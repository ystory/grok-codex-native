import copy
import contextlib
import fcntl
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/worker-network.py"
spec = importlib.util.spec_from_file_location("worker_network", SCRIPT)
networking = importlib.util.module_from_spec(spec)
with patch.object(sys, "path", [str(SCRIPT.parent), *sys.path]):
    spec.loader.exec_module(networking)


def managed_network():
    return {
        "Id": "a" * 64, "Name": "grok-codex-worker-test-net", "Driver": "bridge",
        "EnableIPv6": False,
        "Labels": {"io.grok-codex.realm": "grok-codex", "io.grok-codex.role": "worker-network"},
        "Options": {},
    }


def stopped_worker():
    return {"Id": "b" * 64, "Name": "/grok-codex-worker-test",
            "State": {"Status": "created", "Running": False, "Paused": False, "Restarting": False, "Dead": False},
            "Config": {"Labels": {"io.grok-codex.realm": "grok-codex", "io.grok-codex.role": "worker",
                                  "io.grok-codex.environment": "grok-codex-worker-test"}},
            "HostConfig": {"NetworkMode": "grok-codex-worker-test-net", "Privileged": False, "CapAdd": [],
                           "Sysctls": {"net.ipv6.conf.all.disable_ipv6": "1", "net.ipv6.conf.default.disable_ipv6": "1"}},
            "NetworkSettings": {"Networks": {"grok-codex-worker-test-net": {"NetworkID": ""}}}}


class MemoryFirewall:
    def __init__(self):
        self.state = {"policies": {"INPUT": "ACCEPT", "FORWARD": "DROP"}, "chains": {
            "INPUT": [], "FORWARD": [["-j", "DOCKER-USER"], ["-j", "DOCKER-FORWARD"]],
            "DOCKER-USER": [], "DOCKER-FORWARD": [["-i", "docker0", "-j", "ACCEPT"]],
        }}
        self.changes = []

    def snapshot(self):
        return copy.deepcopy(self.state)

    def new_chain(self, name):
        self.changes.append(("new", name))
        self.state["chains"][name] = []

    def append(self, chain, rule):
        self.changes.append(("append", chain, rule))
        self.state["chains"][chain].append(rule)

    def insert(self, chain, rule):
        self.changes.append(("insert", chain, rule))
        self.state["chains"][chain].insert(0, rule)

    def delete(self, chain, rule):
        self.changes.append(("delete", chain, rule))
        self.state["chains"][chain].remove(rule)

    def delete_chain(self, chain):
        self.changes.append(("delete_chain", chain))
        if self.state["chains"][chain]:
            raise AssertionError("Refusing a nonempty chain")
        del self.state["chains"][chain]


class WorkerNetworkTests(unittest.TestCase):
    def plan(self):
        return networking.build_plan(managed_network(), "grok-codex", "enp0s3", True)

    def test_network_ownership_and_bridge_scope_are_required(self):
        for change in ({"Labels": {}}, {"Driver": "host"}, {"EnableIPv6": True},
                       {"Options": {"com.docker.network.bridge.name": "enp0s3"}}):
            with self.subTest(change=change):
                value = managed_network() | change
                with self.assertRaises(ValueError):
                    networking.build_plan(value, "grok-codex", "enp0s3", True)

    def test_forward_rules_allow_only_the_exact_bridge_and_uplink(self):
        plan = self.plan()
        rules = plan["chains"][plan["forward_chain"]]
        self.assertEqual(rules[0][:4], ["-i", "br-aaaaaaaaaaaa", "-o", "enp0s3"])
        self.assertEqual(rules[1][:4], ["-i", "enp0s3", "-o", "br-aaaaaaaaaaaa"])
        self.assertIn("RELATED,ESTABLISHED", rules[1])
        self.assertEqual(len(rules), 2)
        self.assertTrue(all(rule[-1] == "ACCEPT" for rule in rules))
        self.assertNotIn("DOCKER", " ".join(plan["chains"]))

    def test_host_input_allows_only_replies_then_rejects(self):
        rules = self.plan()["chains"][self.plan()["input_chain"]]
        self.assertIn("--ctdir", rules[0])
        self.assertEqual(rules[0][rules[0].index("--ctdir") + 1], "REPLY")
        self.assertEqual(rules[0][-1], "RETURN")
        self.assertIn("REJECT", rules[1])

    def test_live_worker_must_have_ipv6_disabled_without_network_admin(self):
        container = {"Config": {"Labels": {"io.grok-codex.realm": "grok-codex", "io.grok-codex.role": "worker"}},
                     "HostConfig": {"Privileged": False, "CapAdd": [], "Sysctls": {
                         "net.ipv6.conf.all.disable_ipv6": "1", "net.ipv6.conf.default.disable_ipv6": "1"}}}
        networking.validate_live_worker(container, "grok-codex")
        for host_config in ({"Sysctls": {}}, {"CapAdd": ["NET_ADMIN"]}, {"Privileged": True}):
            with self.subTest(host_config=host_config), self.assertRaises(ValueError):
                networking.validate_live_worker(container | {"HostConfig": container["HostConfig"] | host_config}, "grok-codex")

    def test_unknown_legacy_drop_policy_is_not_expanded(self):
        firewall = MemoryFirewall()
        self.assertTrue(networking.forward_compatibility(firewall.snapshot()))
        del firewall.state["chains"]["DOCKER-FORWARD"]
        with self.assertRaises(ValueError):
            networking.forward_compatibility(firewall.snapshot())

    def test_reapply_is_idempotent_and_remove_restores_existing_rules(self):
        firewall = MemoryFirewall()
        before = firewall.snapshot()
        with tempfile.TemporaryDirectory() as directory:
            rules = networking.NetworkRules(Path(directory), firewall)
            rules.apply(self.plan())
            applied = firewall.snapshot()
            changes = len(firewall.changes)
            rules.apply(self.plan())
            self.assertEqual(firewall.snapshot(), applied)
            self.assertEqual(len(firewall.changes), changes)
            rules.remove(self.plan())
            self.assertEqual(firewall.snapshot(), before)
            self.assertTrue(list((Path(directory) / "retired").glob("*.json")))
            changes = len(firewall.changes)
            rules.remove(self.plan())
            self.assertEqual(len(firewall.changes), changes)

    def test_unowned_chain_collision_is_refused_without_changes(self):
        firewall = MemoryFirewall()
        firewall.state["chains"][self.plan()["input_chain"]] = [["-j", "ACCEPT"]]
        before = firewall.snapshot()
        with tempfile.TemporaryDirectory() as directory, self.assertRaises(ValueError):
            networking.NetworkRules(Path(directory), firewall).apply(self.plan())
        self.assertEqual(firewall.snapshot(), before)
        self.assertEqual(firewall.changes, [])

    def test_removing_a_never_configured_network_changes_nothing(self):
        firewall = MemoryFirewall()
        before = firewall.snapshot()
        with tempfile.TemporaryDirectory() as directory:
            networking.NetworkRules(Path(directory), firewall).remove(self.plan())
        self.assertEqual(firewall.snapshot(), before)
        self.assertEqual(firewall.changes, [])

    def test_drift_is_refused_before_removing_or_repairing_any_rule(self):
        for action in ("apply", "remove"):
            with self.subTest(action=action), tempfile.TemporaryDirectory() as directory:
                firewall = MemoryFirewall()
                rules = networking.NetworkRules(Path(directory), firewall)
                rules.apply(self.plan())
                firewall.state["chains"][self.plan()["input_chain"]].insert(0, ["-j", "ACCEPT"])
                before = firewall.snapshot()
                count = len(firewall.changes)
                with self.assertRaisesRegex(ValueError, "drift"):
                    getattr(rules, action)(self.plan())
                self.assertEqual(firewall.snapshot(), before)
                self.assertEqual(len(firewall.changes), count)

    def test_foreign_goto_reference_is_refused_before_any_change(self):
        for action in ("apply", "remove"):
            for prefix in ([], ["-m", "comment", "--comment", "-j"]):
                with self.subTest(action=action, prefix=prefix), tempfile.TemporaryDirectory() as directory:
                    firewall = MemoryFirewall()
                    rules = networking.NetworkRules(Path(directory), firewall)
                    rules.apply(self.plan())
                    firewall.state["chains"]["FOREIGN"] = [[*prefix, "-g", self.plan()["input_chain"]]]
                    before = firewall.snapshot()
                    count = len(firewall.changes)
                    with self.assertRaisesRegex(ValueError, "drift"):
                        getattr(rules, action)(self.plan())
                    self.assertEqual(firewall.snapshot(), before)
                    self.assertEqual(len(firewall.changes), count)

    def test_start_requires_a_stopped_owned_worker_on_exactly_this_network(self):
        candidate = stopped_worker()
        networking.validate_start_worker(candidate, self.plan())
        networking.validate_start_worker(candidate | {"NetworkSettings": {"Networks": {}}}, self.plan())
        for change in ({"Name": "/another-worker"}, {"State": candidate["State"] | {"Status": "running", "Running": True}},
                       {"HostConfig": candidate["HostConfig"] | {"NetworkMode": "host"}},
                       {"NetworkSettings": {"Networks": {"another-network": {}}}},
                       {"NetworkSettings": {"Networks": {managed_network()["Name"]: {"NetworkID": "c" * 64}}}},
                       {"HostConfig": candidate["HostConfig"] | {"Sysctls": {}}}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                networking.validate_start_worker(candidate | change, self.plan())

    def test_start_rejects_all_networks_before_accessing_the_host(self):
        with patch.object(sys, "argv", [str(SCRIPT), "start", "--project", "grok-codex", "--all", "--container", "worker"]), \
                patch.object(networking, "require_root_host") as root_check, \
                contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as result:
                networking.main()
        self.assertEqual(result.exception.code, 2)
        root_check.assert_not_called()

    def test_start_keeps_the_receipt_lock_through_docker_start(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            firewall = MemoryFirewall()
            starts = []

            def command(arguments):
                tail = arguments[len(networking.DOCKER):] if arguments[:len(networking.DOCKER)] == networking.DOCKER else arguments
                if tail[:2] == ["network", "inspect"]:
                    return json.dumps([managed_network()])
                if tail[:1] == ["inspect"]:
                    return json.dumps([stopped_worker()])
                if tail[:1] == ["ps"]:
                    return ""
                if tail[:1] == ["start"]:
                    self.assertIn(self.plan()["input_chain"], firewall.state["chains"])
                    probe = subprocess.run([sys.executable, "-c",
                        "import fcntl,sys; f=open(sys.argv[1],'a'); "
                        "\ntry: fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)"
                        "\nexcept BlockingIOError: print('LOCKED')"
                        "\nelse: raise SystemExit('UNLOCKED')", str(state / "lock")],
                        text=True, capture_output=True, timeout=5)
                    self.assertEqual((probe.returncode, probe.stdout.strip()), (0, "LOCKED"))
                    starts.append(tail[1])
                    return tail[1] + "\n"
                if arguments[:4] == ["ip", "-4", "-j", "route"]:
                    return '[{"dev":"enp0s3"}]'
                raise AssertionError(arguments)

            with patch.object(sys, "argv", [str(SCRIPT), "start", "--project", "grok-codex", "--network",
                                             managed_network()["Name"], "--container", "grok-codex-worker-test"]), \
                    patch.object(networking, "require_root_host"), \
                    patch.object(networking, "private_state", return_value=state), \
                    patch.object(networking, "host_bridge_exists", return_value=True), \
                    patch.object(networking, "Firewall", return_value=firewall), \
                    patch.object(networking, "execute", side_effect=command), \
                    contextlib.redirect_stdout(io.StringIO()):
                networking.main()
            self.assertEqual(starts, ["b" * 64])

    def test_interrupted_prefix_can_be_completed_and_rolled_back(self):
        with tempfile.TemporaryDirectory() as directory:
            firewall = MemoryFirewall()
            rules = networking.NetworkRules(Path(directory), firewall)
            rules.apply(self.plan())
            chain = self.plan()["input_chain"]
            firewall.state["chains"][chain].pop()
            rules.apply(self.plan())
            self.assertEqual(firewall.state["chains"][chain], self.plan()["chains"][chain])
            rules.remove(self.plan())
            self.assertNotIn(chain, firewall.state["chains"])


if __name__ == "__main__":
    unittest.main()
