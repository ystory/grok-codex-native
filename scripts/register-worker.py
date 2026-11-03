#!/usr/bin/env python3
import argparse
import json
import subprocess

from _docker import compose, deployment, inspect_worker


def main():
    parser = argparse.ArgumentParser(description="Register a currently running Docker worker with the native control service.")
    parser.add_argument("name")
    args = parser.parse_args()
    config = deployment()
    worker = inspect_worker(config, args.name)
    identity = config.worker_name(args.name)
    if not worker["State"]["Running"]:
        raise SystemExit("Start this worker with Docker before native registration.")
    ports = worker["NetworkSettings"]["Ports"]
    endpoint = ports["5000/tcp"]
    if len(endpoint) != 1 or endpoint[0]["HostIp"] != "127.0.0.1":
        raise SystemExit("Refusing an executor endpoint that is not published exclusively on host loopback.")
    payload = {
        "environmentId": identity,
        "execServerUrl": "ws://127.0.0.1:" + endpoint[0]["HostPort"],
        "authBearerToken": (config.data / "runtime" / identity / "exec-token").read_text().strip(),
    }
    subprocess.run(compose(
        "exec", "-T", "control", "python", "/opt/project/deploy/register-environment.py",
    ), input=json.dumps(payload), text=True, check=True)
    browser_ports = {
        port: bindings[0]["HostPort"]
        for port, bindings in ports.items()
        if port != "5000/tcp" and bindings
    }
    print(json.dumps({
        "environmentId": identity,
        "containerName": identity,
        "execServerUrl": payload["execServerUrl"],
        "sourceRoot": "/work/source",
        "sourceHostPath": str(config.data / "workspaces" / identity),
        "artifactRoot": f"/var/lib/grok-codex/artifacts/{identity}",
        "artifactWorkerRoot": "/work/artifacts",
        "artifactHostPath": str(config.data / "artifacts" / identity),
        "browserPorts": browser_ports,
    }))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, FileNotFoundError) as error:
        raise SystemExit(str(error)) from None
    except subprocess.CalledProcessError as error:
        raise SystemExit(f"Native registration exited {error.returncode}. The worker remains created; inspect or re-register it explicitly.") from None
