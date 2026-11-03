#!/usr/bin/env python3
"""Prepare this Grok cloud computer; leave ChatGPT login to the user."""
import argparse
import json
import os
from pathlib import Path
import pwd
import shlex
import shutil
import stat
import subprocess
import sys

import _docker
import prepare


WORKSPACE = Path("/workspace")
DOCKER_SOCKET = Path("/var/run/docker.sock")


def docker_group(command):
    return ["sg", "docker", "-c", shlex.join(map(str, command))]


def import_private_data(source, target, project):
    source, target = source.expanduser().absolute(), target.expanduser().absolute()
    workspace, project = WORKSPACE.resolve(), project.resolve()
    environment = project / "deploy/.env"
    if environment.exists() or environment.is_symlink() or workspace not in project.parents:
        raise RuntimeError("Import requires a fresh checkout below /workspace with no deploy/.env.")
    if DOCKER_SOCKET.exists() or DOCKER_SOCKET.is_symlink():
        raise RuntimeError("Private data import requires a cold computer without a Docker socket.")
    if any(path.is_symlink() for path in (source, *source.parents, target, *target.parents)):
        raise RuntimeError("Import source and target parents must not be symlinks.")
    source, target = source.resolve(), target.resolve()
    target = prepare.validate_data_path(target, project)
    prepare.validate_managed_paths(source, ignore_control_run=True)
    if not source.is_dir() or target.exists():
        raise RuntimeError("Import requires an existing source directory and an absent target.")
    if workspace not in target.parents or project == source or project in source.parents or source == target or source in target.parents:
        raise RuntimeError("Import target must be below /workspace and outside the checkout and source.")

    def fail(error):
        raise error

    for directory, directories, files in os.walk(source, followlinks=False, onerror=fail):
        if Path(directory) == source:
            directories[:] = [name for name in directories if name != "control-run"]
            files = [name for name in files if name != "control-run"]
        for name in directories + files:
            mode = (Path(directory) / name).lstat().st_mode
            if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode) or stat.S_ISLNK(mode)):
                raise RuntimeError("Import refused a special file outside replaceable control-run.")
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    target.mkdir(mode=0o700)
    try:
        for entry in source.iterdir():
            if entry.name == "control-run":
                continue
            destination = target / entry.name
            if entry.is_symlink():
                destination.symlink_to(os.readlink(entry))
            elif entry.is_dir():
                shutil.copytree(entry, destination, symlinks=True)
            else:
                shutil.copy2(entry, destination, follow_symlinks=False)
        (target / "control-run").mkdir(mode=0o700)
        target.chmod(0o700)
    except OSError as error:
        raise RuntimeError(f"Import stopped; original source and private partial target were preserved: {target}") from error


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, help="Existing deployment data, or /workspace/grok-codex-data for a fresh installation.")
    parser.add_argument("--import-data", type=Path, help="Explicitly copy cold private data into a fresh Workspace deployment, preserving its source.")
    args = parser.parse_args()
    if sys.platform != "linux" or os.geteuid() == 0:
        parser.error("Run as the normal Grok cloud user on Linux, without sudo.")
    project = Path(__file__).resolve().parent.parent
    prepare.reject_symlink_chain(project / "deploy/.env")
    identity = pwd.getpwuid(os.getuid())
    username, gid = identity.pw_name, identity.pw_gid
    existing = None
    if (project / "deploy/.env").exists():
        existing = _docker.deployment()
        if existing.uid != os.getuid():
            raise RuntimeError(f"Existing deployment UID {existing.uid} differs from the current user; preserved it.")
        gid = existing.gid
    data = args.data if args.data is not None else existing.data if existing else WORKSPACE / "grok-codex-data"
    data = prepare.validate_data_path(data, project)
    prepare.validate_managed_paths(data)
    if existing is not None and data.expanduser().resolve() != existing.data.resolve():
        raise RuntimeError("Existing deploy/.env selects different data; preserve it and use a fresh checkout.")
    saved = prepare.read_deployment_identity(data.expanduser().resolve())
    if args.import_data is not None:
        source = args.import_data.expanduser().absolute()
        if any(path.is_symlink() for path in (source, *source.parents)):
            raise RuntimeError("Import source and parents must not be symlinks.")
        prepare.validate_managed_paths(source, ignore_control_run=True)
        imported = prepare.read_deployment_identity(source)
        if imported is None and prepare.has_worker_manifests(source):
            raise RuntimeError("Imported worker manifests lack deployment.json; refusing to guess their identity.")
        if saved is not None and imported is not None and saved != imported:
            raise RuntimeError("Import source and target deployment identities differ; preserved both.")
        saved = imported or saved
    if saved is None and existing is None and prepare.has_worker_manifests(data):
        raise RuntimeError("Worker manifests lack deployment.json; refusing to guess their identity.")
    if saved is not None and saved["uid"] != os.getuid():
        raise RuntimeError("Saved deployment UID differs from the current user; preserved it.")
    project_name = existing.project if existing else saved["project"] if saved else "grok-codex"
    gid = existing.gid if existing else saved["gid"] if saved else gid
    requested = prepare.validate_deployment_identity({"schemaVersion": 1, "project": project_name, "uid": os.getuid(), "gid": gid})
    if saved is not None and requested != saved:
        raise RuntimeError("deploy/.env and deployment.json identities differ; preserved both.")
    data, _, _ = prepare.preflight(data, project, requested, import_source=args.import_data)
    if args.import_data is not None:
        import_private_data(args.import_data, data, project)
        print(json.dumps({"privateDataCopied": True, "originalSourcePreserved": True}))
    data = data.expanduser().resolve()
    bootstrap = project / "scripts/bootstrap-grok-cloud.py"
    compose = project / "scripts/compose.sh"

    def run(command, *, capture=False):
        command = list(map(str, command))
        print("+ " + shlex.join(command), flush=True)
        return subprocess.run(command, cwd=project, check=True, text=True, stdout=subprocess.PIPE if capture else None)

    run(["python3", bootstrap, "probe"])
    run(["sudo", "-n", "python3", bootstrap, "install", "--docker-user", username])
    run(["sudo", "-n", "-u", username, "-g", f"#{gid}", "--", project / "scripts/prepare.sh", data, "--project", project_name])
    run(["sudo", "-n", "python3", project / "scripts/worker-network.py", "apply", "--project", _docker.deployment().project, "--all"])
    containers = run(docker_group([compose, "ps", "--all", "--quiet", "control"]), capture=True).stdout.split()
    if len(containers) > 1:
        raise RuntimeError("Multiple existing control containers were preserved; review the operations procedure.")
    if containers:
        state = json.loads(run(docker_group(["docker", "inspect", "--format", "{{json .State}}", containers[0]]), capture=True).stdout)
        if state.get("Status") != "running" or not state.get("Running") or any(state.get(flag) for flag in ("Paused", "Restarting", "Dead")):
            raise RuntimeError(f"Existing control ({state.get('Status')}) was preserved; review the operations procedure before starting or rebuilding it.")
        status = "runtime_preserved"
    else:
        run(docker_group([compose, "--profile", "build-worker", "build"]))
        run(docker_group([compose, "up", "-d", "--wait", "control"]))
        status = "runtime_prepared"
    result = run(docker_group([compose, "exec", "-T", "control", "python", "/opt/project/deploy/healthcheck.py"]), capture=True)
    print(result.stdout, end="")
    account_type = json.loads(result.stdout).get("accountType")
    print(json.dumps({"status": status, "accountType": account_type,
                      "authentication": "chatgpt_login_present" if account_type == "chatgpt" else "authentication_still_required"}))
    print("Next, if ChatGPT login is missing: " + shlex.join(docker_group([compose, "exec", "control", "codex", "login", "--device-auth"])))
    print("Then verify authenticated access: " + shlex.join(docker_group([project / "scripts/healthcheck.sh"])))


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, ValueError, KeyError, subprocess.CalledProcessError) as error:
        raise SystemExit(f"Installation failed: {error}") from error
