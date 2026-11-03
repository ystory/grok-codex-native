#!/usr/bin/env python3
"""Restore one recorded worker's execution environment without resuming a turn."""
import argparse
import json
import re
import subprocess
import sys

from _docker import PROJECT, deployment
import _worker as worker


def restore(config, name):
    with worker.worker_lock(config, name, repair_permissions=True):
        return _restore(config, name)


def _restore(config, name):
    manifest = worker.read_manifest(config, name)
    paths = worker.validate_layout(config, name)
    container = worker.inspect_optional("container", paths["identity"])
    network = worker.inspect_optional("network", paths["network"])
    if network:
        worker.validate_network(network, config, paths)
    image = worker.resolve_image(manifest, existing=container)
    if container:
        if not network:
            raise ValueError("Existing worker has no matching network definition; preserved it for inspection.")
        token = worker.read_token(paths, config.uid)
        worker.validate_container(container, config, manifest, paths, image, token)
        attached = container.get("NetworkSettings", {}).get("Networks", {}).get(paths["network"], {})
        if attached.get("NetworkID") not in (None, "", network["Id"]):
            raise ValueError("Existing worker references another network ID; preserved it.")
        state = container.get("State") or {}
        if any(state.get(key) for key in ("Paused", "Restarting", "Dead")):
            raise ValueError("Existing worker is paused, restarting, or dead; preserved it.")
        if state.get("Running") and state.get("Status") == "running":
            action, status = "apply", "running_preserved"
        elif not state.get("Running") and state.get("Status") in ("created", "exited"):
            action, status = "start", "stopped_started"
        else:
            raise ValueError("Unknown worker state; preserved it.")
        container_id = container["Id"]
    else:
        token = worker.write_token(paths)
        if not network:
            worker.execute(worker.network_command(config, paths), capture=True)
        container_id = worker.execute(worker.create_command(manifest, paths, token, image["Id"]), capture=True).strip()
        if not re.fullmatch(r"[0-9a-f]{64}", container_id):
            raise ValueError("Docker did not return a worker ID; inspect accepted definitions before retrying.")
        action, status = "start", "definitions_restored"
    command = ["sudo", "-n", sys.executable, str(PROJECT / "scripts/worker-network.py"),
               action, "--project", config.project, "--network", paths["network"]]
    if action == "start":
        command += ["--container", container_id]
    worker.execute(command)
    worker.execute([sys.executable, str(PROJECT / "scripts/register-worker.py"), name])
    return {"worker": paths["identity"], "state": status, "containerId": container_id,
            "imageId": image["Id"], "originalImageId": manifest["imageId"],
            "managedImageRebuilt": manifest["imageKind"] == "managed" and image["Id"] != manifest["imageId"],
            "sourceHostPath": str(paths["source"]), "artifactHostPath": str(paths["artifacts"]),
            "nativeTurnResumed": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("name")
    args = parser.parse_args()
    print(json.dumps(restore(deployment(), args.name)))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as error:
        raise SystemExit(f"Worker restoration failed: {error}") from error
