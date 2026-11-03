import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tomllib


def validate_deployment_identity(value):
    if type(value) is not dict or set(value) != {"schemaVersion", "project", "uid", "gid"}:
        raise ValueError("deployment.json must contain only schemaVersion, project, uid, and gid.")
    if type(value["schemaVersion"]) is not int or value["schemaVersion"] != 1:
        raise ValueError("Unsupported deployment.json schemaVersion.")
    if not isinstance(value["project"], str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,39}", value["project"]):
        raise ValueError("Invalid deployment.json project.")
    if any(type(value[key]) is not int or not 0 < value[key] <= 2147483647 for key in ("uid", "gid")):
        raise ValueError("deployment.json uid and gid must be positive, non-root system IDs.")
    return value


def read_deployment_identity(data):
    path = data / "deployment.json"
    if data.is_symlink() or path.is_symlink():
        raise ValueError("Deployment data and deployment.json must not be symlinks.")
    if not path.exists():
        return None
    if not path.is_file():
        raise ValueError("deployment.json must be an ordinary file.")
    return validate_deployment_identity(json.loads(path.read_text()))


def has_worker_manifests(data):
    path = data / "manifests"
    if path.is_symlink():
        raise ValueError("The managed manifests directory must not be a symlink.")
    return any(path.glob("*.json"))


def require_writable_directory(path):
    while not path.exists():
        if path.is_symlink():
            raise ValueError("Refusing a broken symlink in a deployment directory.")
        path = path.parent
    if not path.is_dir() or not os.access(path, os.W_OK | os.X_OK):
        raise ValueError("Deployment directories must be writable by the current user.")


def reject_symlink_chain(path):
    if any(item.is_symlink() for item in (path, *path.parents)):
        raise ValueError(f"Managed deployment paths must not contain symlinks: {path}")


def validate_data_path(data, project):
    data = data.expanduser()
    if not data.is_absolute():
        raise ValueError("Use an absolute private data path.")
    reject_symlink_chain(data)
    data = data.resolve()
    if not re.fullmatch(r"/[A-Za-z0-9_./-]+", str(data)):
        raise ValueError("Private data paths may contain only letters, digits, slash, dot, underscore, or hyphen.")
    if data == Path("/") or data == project or project in data.parents:
        raise ValueError("Keep private data outside the project checkout.")
    return data


def validate_managed_paths(data, *, ignore_control_run=False):
    reject_symlink_chain(data)
    directories = [data, *(data / name for name in ("codex", "artifacts", "workspaces", "runtime", "manifests"))]
    if not ignore_control_run:
        directories.append(data / "control-run")
    for path in directories:
        reject_symlink_chain(path)
        if path.exists() and not path.is_dir():
            raise ValueError(f"Managed deployment directory is not a directory: {path}")
    for path in (data / "deployment.json", data / "codex/config.toml", data / "codex/environments.toml"):
        reject_symlink_chain(path)
        if path.exists() and not path.is_file():
            raise ValueError(f"Managed deployment configuration is not an ordinary file: {path}")


def preflight(data, project, identity, *, import_source=None):
    validate_deployment_identity(identity)
    data = validate_data_path(data, project)
    validate_managed_paths(data)
    environment = project / "deploy/.env"
    reject_symlink_chain(environment)
    content = f"GROK_CODEX_DATA={data}\nGROK_CODEX_PROJECT={identity['project']}\nGROK_CODEX_UID={identity['uid']}\nGROK_CODEX_GID={identity['gid']}\n"
    if environment.exists() and (not environment.is_file() or environment.read_text() != content):
        raise ValueError("deploy/.env already selects different data or identity. Preserve it and use a separate checkout.")
    saved = read_deployment_identity(data)
    if saved is not None and saved != identity:
        raise ValueError("Requested identity differs from deployment.json. Existing data was preserved.")
    if saved is None and not environment.exists() and has_worker_manifests(data):
        raise ValueError("Worker manifests lack deployment.json; refusing to guess their identity.")
    config_data = import_source.expanduser().absolute() if import_source is not None else data
    if import_source is not None:
        validate_managed_paths(config_data, ignore_control_run=True)
    configs = []
    for name in ("config.toml", "environments.toml"):
        path = config_data / "codex" / name
        if not path.exists():
            path = project / "deploy" / name
        reject_symlink_chain(path)
        if not path.is_file():
            raise ValueError(f"Required deployment configuration is missing: {path}")
        configs.append(tomllib.loads(path.read_text()))
    if configs[0].get("forced_login_method") != "chatgpt" or configs[1].get("include_local") is not False:
        raise ValueError("Existing config must set forced_login_method=chatgpt and include_local=false. Files were preserved.")
    require_writable_directory(data)
    require_writable_directory(environment.parent)
    if environment.exists() and not os.access(environment, os.W_OK):
        raise ValueError("Existing deploy/.env is read-only and was preserved.")
    return data, content, saved


def main():
    parser = argparse.ArgumentParser(description="Prepare project-local Docker assets on a Linux Docker host.")
    parser.add_argument("data", type=Path)
    parser.add_argument("--project", default="grok-codex")
    args = parser.parse_args()
    if sys.platform != "linux":
        parser.error("Deploy on Linux. Docker Desktop host-network behavior is not this deployment contract.")
    project = Path(__file__).resolve().parent.parent
    uid = os.getuid() or 1000
    gid = os.getgid() or 1000
    identity = {"schemaVersion": 1, "project": args.project, "uid": uid, "gid": gid}
    environment = project / "deploy/.env"
    try:
        data, content, saved = preflight(args.data, project, identity)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    if shutil.which("docker") is None:
        parser.error("Docker is missing. Install Docker Engine with Compose before deployment.")
    engine = subprocess.check_output(["docker", "info", "--format", "{{.OSType}}"], text=True).strip()
    if engine != "linux":
        parser.error("The selected Docker engine must run Linux containers.")
    subprocess.run(["docker", "compose", "version"], check=True, capture_output=True)
    for directory in (data, *(data / n for n in ("codex", "artifacts", "workspaces", "runtime", "manifests", "control-run"))):
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if os.geteuid() == 0:
            os.chown(directory, uid, gid)
        directory.chmod(0o700)
    for name in ("config.toml", "environments.toml"):
        destination = data / "codex" / name
        if not destination.exists():
            shutil.copyfile(project / "deploy" / name, destination)
            destination.chmod(0o600)
            if os.geteuid() == 0:
                os.chown(destination, uid, gid)
    config = tomllib.loads((data / "codex/config.toml").read_text())
    environments = tomllib.loads((data / "codex/environments.toml").read_text())
    if config.get("forced_login_method") != "chatgpt" or environments.get("include_local") is not False:
        parser.error("Existing config must set forced_login_method=chatgpt and include_local=false. Files were preserved.")
    if saved is None:
        descriptor = os.open(data / "deployment.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        if os.geteuid() == 0:
            os.fchown(descriptor, uid, gid)
        with os.fdopen(descriptor, "w") as stream:
            json.dump(identity, stream)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
    (data / "deployment.json").chmod(0o600)
    environment.write_text(content)
    environment.chmod(0o600)
    print(f"Prepared {data}. Build with scripts/compose.sh --profile build-worker build.")


if __name__ == "__main__":
    main()
