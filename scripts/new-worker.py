#!/usr/bin/env python3
import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys

from _docker import PROJECT, REALM_LABEL, ROLE_LABEL, deployment


def main():
    parser = argparse.ArgumentParser(description="Create one fresh Docker worker and register its native execution environment.")
    parser.add_argument("name")
    parser.add_argument("--repo")
    parser.add_argument("--ref")
    parser.add_argument("--browser-port", type=int)
    parser.add_argument("--cpus", type=float, default=2)
    parser.add_argument("--memory", default="4g")
    parser.add_argument("--pids-limit", type=int, default=512)
    parser.add_argument("--image", help="An existing Docker image providing the pinned native exec-server")
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--secrets-dir", type=Path)
    parser.add_argument("--inputs-dir", type=Path)
    args = parser.parse_args()
    if args.ref and not args.repo:
        parser.error("--ref requires --repo.")
    if args.browser_port is not None and not 1 <= args.browser_port <= 65535:
        parser.error("--browser-port must be a container TCP port from 1 to 65535.")
    if args.browser_port == 5000:
        parser.error("Container port 5000 belongs to the authenticated native executor.")
    if args.cpus <= 0 or args.pids_limit < 1:
        parser.error("CPU and PID limits must be positive.")
    config = deployment()
    identity = config.worker_name(args.name)
    image = args.image or f"{config.project}-worker:0.166.0"
    network = identity + "-net"
    source = config.data / "workspaces" / identity
    artifacts = config.data / "artifacts" / identity
    runtime = config.data / "runtime" / identity
    if any(path.exists() for path in (source, artifacts, runtime)):
        parser.error("This identity already has private data. Choose a fresh name; preserved source is never reused for another task.")
    if args.env_file:
        args.env_file = args.env_file.resolve(strict=True)
        reserved = {"OPENAI_API_KEY", "CODEX_API_KEY", "CODEX_HOME", "CODEX_ACCESS_TOKEN", "HOME"}
        for line in args.env_file.read_text().splitlines():
            if line.strip() and not line.lstrip().startswith("#") and line.split("=", 1)[0].strip() in reserved:
                parser.error("Worker env file must not override native model credentials or home paths.")
    for attribute in ("secrets_dir", "inputs_dir"):
        value = getattr(args, attribute)
        if value:
            value = value.resolve(strict=True)
            if not value.is_dir():
                parser.error(f"--{attribute.replace('_', '-')} must be a directory.")
            if "," in str(value):
                parser.error("Bind mount paths cannot contain commas.")
            if value == config.data or config.data in value.parents or value in config.data.parents:
                parser.error("Worker inputs and secrets must be outside managed native state, runtime tokens, and peer data.")
            setattr(args, attribute, value)
    subprocess.run(["docker", "image", "inspect", image], check=True, stdout=subprocess.DEVNULL)
    for directory in (source, artifacts, runtime, runtime / "home", runtime / "home/.codex"):
        directory.mkdir(mode=0o700, parents=True)
        if os.geteuid() == 0:
            os.chown(directory, config.uid, config.gid)
    token = secrets.token_urlsafe(48)
    token_path = runtime / "exec-token"
    token_path.write_text(token + "\n")
    token_path.chmod(0o600)
    subprocess.run([
        "docker", "network", "create", "--driver", "bridge",
        "--label", f"{REALM_LABEL}={config.project}",
        "--label", f"{ROLE_LABEL}=worker-network", network,
    ], check=True, stdout=subprocess.DEVNULL)
    command = [
        "docker", "create", "--name", identity, "--init", "--network", network,
        "--label", f"{REALM_LABEL}={config.project}",
        "--label", f"{ROLE_LABEL}=worker",
        "--label", f"io.grok-codex.environment={identity}",
        "--cpus", str(args.cpus), "--memory", args.memory,
        "--pids-limit", str(args.pids_limit), "--shm-size", "512m",
        "--publish", "127.0.0.1::5000/tcp",
        "--mount", f"type=bind,src={source},dst=/work/source",
        "--mount", f"type=bind,src={artifacts},dst=/work/artifacts",
        "--mount", f"type=bind,src={runtime / 'home'},dst=/home/node",
    ]
    if args.browser_port:
        command += ["--publish", f"127.0.0.1::{args.browser_port}/tcp"]
    if args.env_file:
        command += ["--env-file", str(args.env_file)]
    for path, target in ((args.inputs_dir, "/run/grok-codex/inputs"), (args.secrets_dir, "/run/secrets")):
        if path:
            command += ["--mount", f"type=bind,src={path},dst={target},readonly"]
    command += [
        image, "codex", "exec-server",
        "--listen", "ws://0.0.0.0:5000", "--ws-auth", "capability-token",
        "--ws-token-sha256", hashlib.sha256(token.encode()).hexdigest(),
    ]
    subprocess.run(command, check=True, stdout=subprocess.DEVNULL)
    print(json.dumps({
        "containerName": identity,
        "environmentId": identity,
        "containerAccepted": True,
        "image": image,
        "sourceRoot": "/work/source",
        "sourceHostPath": str(source),
        "artifactRoot": f"/var/lib/grok-codex/artifacts/{identity}",
        "artifactWorkerRoot": "/work/artifacts",
        "artifactHostPath": str(artifacts),
    }), flush=True)
    subprocess.run(["docker", "start", identity], check=True, stdout=subprocess.DEVNULL)
    if args.repo:
        subprocess.run(["docker", "exec", identity, "git", "clone", "--", args.repo, "/work/source"], check=True)
        if args.ref:
            subprocess.run(["docker", "exec", identity, "git", "-C", "/work/source", "fetch", "origin", "--", args.ref], check=True)
            subprocess.run(["docker", "exec", identity, "git", "-C", "/work/source", "checkout", "--detach", "FETCH_HEAD"], check=True)
    else:
        subprocess.run(["docker", "exec", identity, "git", "-C", "/work/source", "init", "--initial-branch=main"], check=True)
    subprocess.run([sys.executable, str(PROJECT / "scripts/register-worker.py"), args.name], check=True)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, FileNotFoundError) as error:
        raise SystemExit(str(error)) from None
    except subprocess.CalledProcessError as error:
        raise SystemExit(f"Docker/native operation exited {error.returncode}. Any accepted worker and its private data are preserved. Inspect Docker before retrying with a fresh identity.") from None
