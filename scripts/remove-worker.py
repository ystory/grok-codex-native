#!/usr/bin/env python3
import argparse
import json
import subprocess

from _docker import REALM_LABEL, ROLE_LABEL, deployment, inspect_worker


def main():
    parser = argparse.ArgumentParser(description="Remove a stopped worker and its private Docker network while preserving all source and exports.")
    parser.add_argument("name")
    args = parser.parse_args()
    config = deployment()
    worker = inspect_worker(config, args.name)
    identity = config.worker_name(args.name)
    if worker["State"]["Running"] or worker["State"]["Restarting"] or worker["State"]["Paused"]:
        raise SystemExit("Stop native work and explicitly docker stop this worker before removing it.")
    networks = worker["NetworkSettings"]["Networks"]
    if set(networks) != {identity + "-net"}:
        raise SystemExit("Unexpected worker networks. Inspect Docker before removal.")
    network = json.loads(subprocess.check_output(["docker", "network", "inspect", identity + "-net"]))[0]
    labels = network.get("Labels") or {}
    if labels.get(REALM_LABEL) != config.project or labels.get(ROLE_LABEL) != "worker-network":
        raise SystemExit("Refusing an unmanaged Docker network.")
    subprocess.run(["docker", "rm", identity], check=True)
    subprocess.run(["docker", "network", "rm", identity + "-net"], check=True)
    print(json.dumps({
        "removedContainer": identity,
        "preservedSource": str(config.data / "workspaces" / identity),
        "preservedArtifacts": str(config.data / "artifacts" / identity),
        "preservedRuntime": str(config.data / "runtime" / identity),
    }))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, FileNotFoundError) as error:
        raise SystemExit(str(error)) from None
