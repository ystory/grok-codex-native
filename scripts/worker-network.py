#!/usr/bin/env python3
"""Reconcile only this deployment's worker bridge rules on the Grok computer."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import time

from _docker import REALM_LABEL, ROLE_LABEL


DOCKER = ["/usr/bin/docker", "--host", "unix:///var/run/docker.sock"]
STATE = Path("/var/lib/grok-codex-bootstrap/worker-networks")


def execute(command):
    return subprocess.run(command, check=True, text=True, capture_output=True).stdout


def build_plan(network, project, uplink, compatibility):
    labels = network.get("Labels") or {}
    identity = network.get("Id", "")
    name = network.get("Name", "")
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,39}", project):
        raise ValueError("Invalid deployment project.")
    if labels.get(REALM_LABEL) != project or labels.get(ROLE_LABEL) != "worker-network":
        raise ValueError("Refusing an unmanaged Docker network.")
    if not re.fullmatch(r"[0-9a-f]{64}", identity) or not re.fullmatch(re.escape(project) + r"-worker-[a-z0-9][a-z0-9-]{0,39}-net", name):
        raise ValueError("Unexpected managed network identity.")
    if network.get("Driver") != "bridge" or network.get("EnableIPv6") is not False or network.get("Internal"):
        raise ValueError("Managed workers require an external IPv4-only Docker bridge.")
    bridge = "br-" + identity[:12]
    if (network.get("Options") or {}).get("com.docker.network.bridge.name", bridge) != bridge:
        raise ValueError("Refusing a bridge name outside the managed Docker network identity.")
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,15}", uplink) or uplink in (bridge, "lo", "docker0") or uplink.startswith("br-"):
        raise ValueError("Unexpected external uplink interface.")
    input_chain, forward_chain = "GCXI" + identity[:16], "GCXF" + identity[:16]
    owner = "grok-codex/" + project + "/" + identity
    comment = ["-m", "comment", "--comment", owner]
    chains = {input_chain: [
        ["-m", "conntrack", "--ctstate", "RELATED,ESTABLISHED", "--ctdir", "REPLY", "-j", "RETURN"],
        ["-j", "REJECT", "--reject-with", "icmp-port-unreachable"],
    ]}
    hooks = [["INPUT", ["-i", bridge, *comment, "-j", input_chain]]]
    if compatibility:
        chains[forward_chain] = [
            ["-i", bridge, "-o", uplink, "-j", "ACCEPT"],
            ["-i", uplink, "-o", bridge, "-m", "conntrack", "--ctstate", "RELATED,ESTABLISHED", "-j", "ACCEPT"],
        ]
        hooks += [["FORWARD", ["-i", bridge, *comment, "-j", forward_chain]],
                  ["FORWARD", ["-o", bridge, *comment, "-j", forward_chain]]]
    return {"project": project, "network": name, "id": identity, "bridge": bridge,
            "uplink": uplink, "input_chain": input_chain, "forward_chain": forward_chain,
            "chains": chains, "hooks": hooks}


def validate_live_worker(container, project):
    labels = container.get("Config", {}).get("Labels") or {}
    config = container.get("HostConfig") or {}
    sysctls = config.get("Sysctls") or {}
    capabilities = {value.upper().removeprefix("CAP_") for value in config.get("CapAdd") or []}
    if labels.get(REALM_LABEL) != project or labels.get(ROLE_LABEL) != "worker":
        raise ValueError("An unmanaged container is attached to the worker network.")
    if config.get("Privileged") or capabilities & {"ALL", "NET_ADMIN", "SYS_ADMIN"}:
        raise ValueError("Refusing a worker able to change its network namespace protections.")
    if any(str(sysctls.get("net.ipv6.conf." + scope + ".disable_ipv6")) != "1" for scope in ("all", "default")):
        raise ValueError("Existing worker lacks IPv6 isolation; stop it and create a fresh worker with the current script.")


def validate_start_worker(container, plan):
    validate_live_worker(container, plan["project"])
    identity = plan["network"].removesuffix("-net")
    labels = container["Config"]["Labels"]
    state = container.get("State") or {}
    if not re.fullmatch(r"[0-9a-f]{64}", container.get("Id", "")) or container.get("Name") != "/" + identity or labels.get("io.grok-codex.environment") != identity:
        raise ValueError("Start requires the exact managed worker identity for this network.")
    if state.get("Status") not in ("created", "exited") or any(state.get(key) for key in ("Running", "Paused", "Restarting", "Dead")):
        raise ValueError("Start requires a stopped worker; existing execution was preserved.")
    if container["HostConfig"].get("NetworkMode") not in (plan["network"], plan["id"]):
        raise ValueError("Worker is not configured for this dedicated network.")
    networks = container.get("NetworkSettings", {}).get("Networks") or {}
    if networks and (set(networks) != {plan["network"]} or networks[plan["network"]].get("NetworkID") not in (None, "", plan["id"])):
        raise ValueError("Unexpected worker network attachment.")


def parse_filter(output):
    state = {"policies": {}, "chains": {}}
    for line in output.splitlines():
        fields = shlex.split(line)
        if not fields:
            continue
        if fields[0] == "-P" and len(fields) == 3:
            state["policies"][fields[1]] = fields[2]
            state["chains"].setdefault(fields[1], [])
        elif fields[0] == "-N" and len(fields) == 2:
            state["chains"].setdefault(fields[1], [])
        elif fields[0] == "-A" and len(fields) >= 4:
            state["chains"].setdefault(fields[1], []).append(fields[2:])
        else:
            raise ValueError("Unrecognized legacy firewall output; existing rules were preserved.")
    return state


def forward_compatibility(state):
    policy = state["policies"].get("FORWARD")
    if policy == "ACCEPT":
        return False
    chains = state["chains"]
    if policy == "DROP" and "DOCKER-FORWARD" in chains and ["-j", "DOCKER-FORWARD"] in chains.get("FORWARD", []):
        return True
    raise ValueError("Unknown legacy FORWARD policy; refusing to expand its access.")


class Firewall:
    def __init__(self):
        self.command = ["/usr/sbin/iptables-legacy", "-w", "5", "-t", "filter"]

    def snapshot(self):
        return parse_filter(execute([*self.command, "-S"]))

    def new_chain(self, chain):
        execute([*self.command, "-N", chain])

    def append(self, chain, rule):
        execute([*self.command, "-A", chain, *rule])

    def insert(self, chain, rule):
        execute([*self.command, "-I", chain, "1", *rule])

    def delete(self, chain, rule):
        execute([*self.command, "-D", chain, *rule])

    def delete_chain(self, chain):
        execute([*self.command, "-X", chain])


def write_receipt(path, value):
    descriptor, temporary = tempfile.mkstemp(prefix=".network-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as stream:
            json.dump(value, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
            os.fchmod(stream.fileno(), 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class NetworkRules:
    def __init__(self, directory, firewall):
        self.directory, self.firewall = directory, firewall
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)

    def check(self, plan, state, *, required=False):
        path = self.directory / (plan["id"] + ".json")
        if path.is_symlink():
            raise ValueError("Refusing a symlink receipt.")
        if not path.exists():
            if required:
                raise ValueError("No owned network receipt exists; nothing was removed.")
            if any(chain in state["chains"] for chain in plan["chains"]):
                raise ValueError("Unowned firewall chain collision; existing rules were preserved.")
            return path
        metadata = path.stat()
        if metadata.st_uid != os.geteuid() or metadata.st_mode & 0o022:
            raise ValueError("Network receipt ownership or permissions changed.")
        if json.loads(path.read_text()) != plan:
            raise ValueError("Network receipt drift; existing configuration was preserved.")
        for chain, expected in plan["chains"].items():
            actual = state["chains"].get(chain, [])
            if actual != expected[:len(actual)]:
                raise ValueError(f"Firewall drift in {chain}; existing rules were preserved.")
        hooks = [(chain, rule) for chain, rule in plan["hooks"]]
        for chain, rules in state["chains"].items():
            for rule in rules:
                targets = {rule[index + 1] for index, value in enumerate(rule[:-1]) if value in ("-j", "-g")}
                if targets.intersection(plan["chains"]) and (chain, rule) not in hooks:
                    raise ValueError("Firewall hook drift; foreign references were preserved.")
        for chain, rule in hooks:
            if state["chains"].get(chain, []).count(rule) > 1:
                raise ValueError("Duplicate firewall hook drift.")
            if chain == "INPUT" and rule in state["chains"].get(chain, []):
                for earlier in state["chains"][chain][:state["chains"][chain].index(rule)]:
                    if not (len(earlier) == 8 and earlier[0] == "-i" and earlier[1] != plan["bridge"]
                            and earlier[2:5] == ["-m", "comment", "--comment"] and earlier[6] == "-j"
                            and re.fullmatch(r"GCXI[0-9a-f]{16}", earlier[7])):
                        raise ValueError("INPUT priority drift; host protection cannot be guaranteed.")
        return path

    def apply(self, plan):
        state = self.firewall.snapshot()
        path = self.check(plan, state)
        if not path.exists():
            write_receipt(path, plan)
        for chain, rules in plan["chains"].items():
            if chain not in state["chains"]:
                self.firewall.new_chain(chain)
            for rule in rules[len(state["chains"].get(chain, [])):]:
                self.firewall.append(chain, rule)
        for chain, rule in plan["hooks"]:
            if rule not in state["chains"].get(chain, []):
                self.firewall.insert(chain, rule)
        self.check(plan, self.firewall.snapshot(), required=True)

    def remove(self, plan):
        state = self.firewall.snapshot()
        path = self.check(plan, state)
        if not path.exists():
            return
        retired = self.directory / "retired"
        if retired.is_symlink() or (retired.exists() and not retired.is_dir()):
            raise ValueError("Refusing a changed receipt archive path.")
        for chain, rule in reversed(plan["hooks"]):
            if rule in state["chains"].get(chain, []):
                self.firewall.delete(chain, rule)
        for chain in reversed(plan["chains"]):
            if chain in state["chains"]:
                for rule in reversed(state["chains"][chain]):
                    self.firewall.delete(chain, rule)
                self.firewall.delete_chain(chain)
        retired.mkdir(mode=0o700, exist_ok=True)
        path.rename(retired / (plan["id"] + "-" + str(time.time_ns()) + ".json"))


def private_state():
    for path in (*reversed(STATE.parents), STATE):
        if path.is_symlink():
            raise ValueError("Refusing a symlink in the root network state path.")
    STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    for path in (STATE.parent, STATE):
        if path.stat().st_uid != 0 or path.stat().st_mode & 0o022:
            raise ValueError("Network state must be root-owned and not writable by other users.")
    return STATE


def require_root_host():
    if sys.platform != "linux" or os.geteuid() != 0:
        raise ValueError("Run this host helper with sudo -n python3 on the Grok cloud computer.")


def host_bridge_exists(bridge):
    return (Path("/sys/class/net") / bridge / "bridge").is_dir()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("apply", "remove", "start"))
    parser.add_argument("--project", required=True)
    parser.add_argument("--container")
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--network")
    selection.add_argument("--all", action="store_true")
    args = parser.parse_args()
    if args.action == "start" and (args.all or not args.network or not args.container):
        parser.error("Start requires one --network and one --container; --all is not accepted.")
    if args.action != "start" and args.container:
        parser.error("--container is only accepted with start.")
    require_root_host()
    directory = private_state()
    descriptor = os.open(directory / "lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "a") as lock:
        metadata = os.fstat(lock.fileno())
        if metadata.st_uid != os.geteuid() or metadata.st_mode & 0o022:
            raise ValueError("Refusing an unsafe network state lock.")
        fcntl.flock(lock, fcntl.LOCK_EX)
        names = [args.network] if args.network else execute([*DOCKER, "network", "ls", "--quiet", "--filter", "label=" + REALM_LABEL + "=" + args.project,
                                                            "--filter", "label=" + ROLE_LABEL + "=worker-network"]).splitlines()
        firewall = Firewall()
        state = firewall.snapshot()
        compatibility = forward_compatibility(state) if args.action != "remove" else False
        route = json.loads(execute(["ip", "-4", "-j", "route", "get", "198.18.0.1"]))
        if len(route) != 1:
            raise ValueError("Ambiguous external uplink route.")
        plans = []
        start_identity = None
        manager = NetworkRules(directory, firewall)
        for name in names:
            network = json.loads(execute([*DOCKER, "network", "inspect", name]))[0]
            plan = build_plan(network, args.project, route[0]["dev"], compatibility)
            receipt = directory / (plan["id"] + ".json")
            if receipt.is_symlink():
                raise ValueError("Refusing a symlink receipt.")
            saved = json.loads(receipt.read_text()) if args.action == "remove" and receipt.exists() else None
            if saved:
                plan = build_plan(network, args.project, saved["uplink"], saved["forward_chain"] in saved["chains"])
            if not host_bridge_exists(plan["bridge"]):
                raise ValueError("Managed bridge is absent from this host network namespace.")
            running = execute([*DOCKER, "ps", "--quiet", "--filter", "network=" + network["Id"]]).splitlines()
            if args.action == "remove" and running:
                raise ValueError("Stop attached workers before removing their network protections.")
            if args.action == "start":
                candidate = json.loads(execute([*DOCKER, "inspect", args.container]))[0]
                validate_start_worker(candidate, plan)
                if running:
                    raise ValueError("The dedicated network already has a running container; preserved it.")
                start_identity = candidate["Id"]
            for identity in running:
                validate_live_worker(json.loads(execute([*DOCKER, "inspect", identity]))[0], args.project)
            manager.check(plan, state)
            plans.append(plan)
        for plan in plans:
            (manager.remove if args.action == "remove" else manager.apply)(plan)
        if start_identity:
            execute([*DOCKER, "start", start_identity])
        print(json.dumps({"action": args.action, "networks": [p["network"] for p in plans],
                          "startedContainer": start_identity, "legacyForwardCompatibility": compatibility}))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as error:
        raise SystemExit(f"Worker network configuration failed: {error}") from error
