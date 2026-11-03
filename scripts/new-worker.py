#!/usr/bin/env python3
import argparse
import json
from pathlib import Path
import re
import subprocess
import sys

from _docker import PROJECT, deployment
import _worker as worker


def main():
    parser = argparse.ArgumentParser(description="Create one fresh Docker worker and register its native execution environment.")
    parser.add_argument("name")
    parser.add_argument("--repo")
    parser.add_argument("--ref")
    parser.add_argument("--browser-port", type=int)
    parser.add_argument("--cpus", type=float, default=2)
    parser.add_argument("--memory", default="4g")
    parser.add_argument("--pids-limit", type=int, default=512)
    parser.add_argument("--image", help="An existing image whose immutable ID must remain available for restoration")
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--secrets-dir", type=Path)
    parser.add_argument("--inputs-dir", type=Path)
    args = parser.parse_args()
    if args.ref and not args.repo:
        parser.error("--ref requires --repo.")
    config = deployment()
    with worker.worker_lock(config, args.name):
        _create(config, args)


def _create(config, args):
    image = worker.image_info(args.image or f"{config.project}-worker:0.166.0")
    manifest = worker.make_manifest(config, args, image)
    if image.get("Config", {}).get("Volumes"):
        raise ValueError("Prepared worker images must not declare implicit volumes.")
    paths = worker.create_layout(config, args.name)
    worker.write_manifest(config, args.name, manifest)
    token = worker.write_token(paths)
    worker.execute(worker.network_command(config, paths), capture=True)
    container_id = worker.execute(worker.create_command(manifest, paths, token, image["Id"]), capture=True).strip()
    if not re.fullmatch(r"[0-9a-f]{64}", container_id):
        raise ValueError("Docker did not return a worker ID; inspect accepted definitions before retrying.")
    print(json.dumps({"containerName": paths["identity"], "environmentId": paths["identity"],
                      "containerAccepted": True, "containerId": container_id, "imageId": image["Id"],
                      "sourceRoot": "/work/source", "sourceHostPath": str(paths["source"]),
                      "artifactRoot": f"/var/lib/grok-codex/artifacts/{paths['identity']}",
                      "artifactWorkerRoot": "/work/artifacts", "artifactHostPath": str(paths["artifacts"]),
                      "launchManifest": str(paths["manifest"])}), flush=True)
    worker.execute(["sudo", "-n", sys.executable, str(PROJECT / "scripts/worker-network.py"), "start",
                    "--project", config.project, "--network", paths["network"], "--container", paths["identity"]])
    if args.repo:
        worker.execute([*worker.DOCKER, "exec", paths["identity"], "git", "clone", "--", args.repo, "/work/source"])
        if args.ref:
            worker.execute([*worker.DOCKER, "exec", paths["identity"], "git", "-C", "/work/source", "fetch", "origin", "--", args.ref])
            worker.execute([*worker.DOCKER, "exec", paths["identity"], "git", "-C", "/work/source", "checkout", "--detach", "FETCH_HEAD"])
    else:
        worker.execute([*worker.DOCKER, "exec", paths["identity"], "git", "-C", "/work/source", "init", "--initial-branch=main"])
    worker.execute([sys.executable, str(PROJECT / "scripts/register-worker.py"), args.name])


if __name__ == "__main__":
    try:
        main()
    except (ValueError, FileNotFoundError) as error:
        raise SystemExit(str(error)) from None
    except subprocess.CalledProcessError as error:
        raise SystemExit(f"Docker/native operation exited {error.returncode}. Accepted definitions, launch manifest, and private data were preserved; inspect before retrying.") from None
