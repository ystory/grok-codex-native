"""Typed worker launch data shared by creation and explicit restoration."""
from contextlib import ExitStack, contextmanager
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess
import tempfile

from _docker import REALM_LABEL, ROLE_LABEL


DOCKER = ["docker", "--host", "unix:///var/run/docker.sock"]
FIELDS = {"schemaVersion", "project", "name", "worker", "uid", "gid", "image", "imageKind", "imageId",
          "cpus", "memory", "pidsLimit", "browserPort", "envFile", "inputsDir", "secretsDir"}
SYSCTLS = {"net.ipv6.conf.all.disable_ipv6": "1", "net.ipv6.conf.default.disable_ipv6": "1"}
RESERVED_ENV = {"OPENAI_API_KEY", "CODEX_API_KEY", "CODEX_HOME", "CODEX_ACCESS_TOKEN", "HOME"}


def execute(command, *, capture=False):
    result = subprocess.run(list(map(str, command)), check=True, text=True,
                            stdout=subprocess.PIPE if capture else None,
                            env=os.environ | {"DOCKER_HOST": "unix:///var/run/docker.sock"})
    return result.stdout or ""


def layout(config, name):
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,39}", config.project):
        raise ValueError("Invalid deployment project.")
    identity = config.worker_name(name)
    runtime = config.data / "runtime" / identity
    return {"identity": identity, "network": identity + "-net", "source": config.data / "workspaces" / identity,
            "artifacts": config.data / "artifacts" / identity, "runtime": runtime, "home": runtime / "home",
            "token": runtime / "exec-token", "manifest": config.data / "manifests" / (identity + ".json")}


def private_directory(path, config, *, create=False):
    if config.uid != os.getuid() or not re.fullmatch(r"/[A-Za-z0-9_./-]+", str(config.data)) or config.data == Path("/") or config.data.resolve(strict=True) != config.data:
        raise ValueError("Use the deployment owner and its existing, non-symlink private data root.")
    for current in (config.data, *(config.data / Path(*path.relative_to(config.data).parts[:index])
                                   for index in range(1, len(path.relative_to(config.data).parts) + 1))):
        if current.is_symlink():
            raise ValueError("Managed worker paths must not contain symlinks.")
        if not current.exists() and create:
            current.mkdir(mode=0o700)
        if not current.is_dir() or current.stat().st_uid != config.uid or stat.S_IMODE(current.stat().st_mode) & 0o077:
            raise ValueError("Managed worker directories must exist, belong to the deployment owner, and be private.")


def private_file(path, uid):
    if path.is_symlink() or not path.is_file():
        raise ValueError("Missing or symlinked private worker file.")
    metadata = path.stat()
    if metadata.st_uid != uid or stat.S_IMODE(metadata.st_mode) != 0o600:
        raise ValueError("Private worker files must belong to the deployment owner with mode 0600.")


@contextmanager
def worker_lock(config, name, *, repair_permissions=False):
    path = layout(config, name)["manifest"].with_suffix(".lock")
    if repair_permissions:
        restore_permission_targets(config, name)
    else:
        private_directory(path.parent, config, create=True)
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "r+") as stream:
        metadata = os.fstat(stream.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1 or metadata.st_uid != config.uid or (not repair_permissions and stat.S_IMODE(metadata.st_mode) != 0o600):
            raise ValueError("Worker lock must be a private regular file owned by the deployment user.")
        fcntl.flock(stream, fcntl.LOCK_EX)
        if repair_permissions:
            repair_worker_permissions(config, name, stream.fileno())
        yield


def memory_bytes(value):
    if not isinstance(value, str) or not re.fullmatch(r"[1-9][0-9]*[bBkKmMgG]?", value):
        raise ValueError("Worker memory must be a positive byte count or b/k/m/g limit.")
    unit = value[-1].lower() if value[-1].isalpha() else "b"
    return int(value[:-1] if value[-1].isalpha() else value) * {"b": 1, "k": 1024, "m": 1024 ** 2, "g": 1024 ** 3}[unit]


def external_path(value, config, *, directory):
    if value is None:
        return None
    if not isinstance(value, str) or "," in value or "://" in value:
        raise ValueError("Worker input paths must be absolute filesystem paths without commas.")
    path = Path(value)
    if not path.is_absolute() or path.resolve(strict=True) != path or path.is_symlink():
        raise ValueError("Worker input paths must exist and must not be symlinked.")
    if path == config.data or config.data in path.parents or path in config.data.parents:
        raise ValueError("Worker input paths must be outside managed private data.")
    if not (path.is_dir() if directory else path.is_file()):
        raise ValueError("Worker input path has the wrong filesystem type.")
    return str(path)


def env_values(filename):
    values = {}
    if filename:
        for line in Path(filename).read_text().splitlines():
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            key, separator, value = line.partition("=")
            if not separator or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key) or key in values or key in RESERVED_ENV:
                raise ValueError("Worker env files require unique KEY=VALUE entries without reserved model/home variables.")
            values[key] = value
    return values


def validate_manifest(value, config, name):
    paths = layout(config, name)
    if not isinstance(value, dict) or set(value) != FIELDS or type(value.get("schemaVersion")) is not int or value["schemaVersion"] != 1:
        raise ValueError("Unsupported or unrecognized worker launch manifest fields.")
    if any(value[key] != expected for key, expected in (("project", config.project), ("name", name),
           ("worker", paths["identity"]), ("uid", config.uid), ("gid", config.gid))):
        raise ValueError("Worker manifest identity/UID/GID does not match this deployment.")
    if any(type(value[key]) is not int for key in ("uid", "gid", "pidsLimit")) or value["pidsLimit"] < 1:
        raise ValueError("Worker UID/GID/PID fields must be integers with a positive PID limit.")
    if type(value["cpus"]) not in (int, float) or not math.isfinite(value["cpus"]) or value["cpus"] <= 0:
        raise ValueError("Worker CPU limit must be finite and positive.")
    memory_bytes(value["memory"])
    port = value["browserPort"]
    if port is not None and (type(port) is not int or not 1 <= port <= 65535 or port == 5000):
        raise ValueError("Invalid worker browser port.")
    if value["imageKind"] not in ("managed", "custom") or not isinstance(value["image"], str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/@:-]{0,511}", value["image"]):
        raise ValueError("Invalid worker image reference or policy.")
    if value["imageKind"] == "managed" and value["image"] != f"{config.project}-worker:0.166.0":
        raise ValueError("Managed workers require the pinned deployment image tag.")
    if not isinstance(value["imageId"], str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", value["imageId"]):
        raise ValueError("Worker manifest lacks immutable image provenance.")
    for key in ("envFile", "inputsDir", "secretsDir"):
        external_path(value[key], config, directory=key != "envFile")
    env_values(value["envFile"])
    return value


def make_manifest(config, args, image):
    validate_image(image)
    value = {"schemaVersion": 1, "project": config.project, "name": args.name, "worker": config.worker_name(args.name),
             "uid": config.uid, "gid": config.gid, "image": args.image or f"{config.project}-worker:0.166.0",
             "imageKind": "custom" if args.image else "managed", "imageId": image["Id"],
             "cpus": args.cpus, "memory": args.memory, "pidsLimit": args.pids_limit, "browserPort": args.browser_port}
    for key, attribute in (("envFile", "env_file"), ("inputsDir", "inputs_dir"), ("secretsDir", "secrets_dir")):
        path = getattr(args, attribute)
        value[key] = str(Path(path).resolve(strict=True)) if path is not None else None
    return validate_manifest(value, config, args.name)


def atomic_write(path, content, *, exclusive=False):
    descriptor, temporary = tempfile.mkstemp(prefix=".worker-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
            os.fchmod(stream.fileno(), 0o600)
        if exclusive:
            os.link(temporary, path)
        else:
            os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def write_manifest(config, name, value):
    paths = layout(config, name)
    private_directory(paths["manifest"].parent, config, create=True)
    atomic_write(paths["manifest"], json.dumps(validate_manifest(value, config, name), indent=2) + "\n", exclusive=True)


def read_manifest(config, name):
    path = layout(config, name)["manifest"]
    if not path.exists() and not path.is_symlink():
        raise ValueError("No launch manifest exists; legacy worker data is never guessed or reset.")
    private_directory(path.parent, config)
    private_file(path, config.uid)
    return manifest_value(path, config, name)


def manifest_value(path, config, name):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate worker manifest field.")
            result[key] = value
        return result
    return validate_manifest(json.loads(path.read_text(), object_pairs_hook=unique), config, name)


def restore_permission_targets(config, name):
    private_directory(config.data, config)
    paths = layout(config, name)
    if not paths["manifest"].exists() and not paths["manifest"].is_symlink():
        raise ValueError("No launch manifest exists; legacy worker data is never guessed or reset.")
    directories = set()
    for end in (paths["source"], paths["artifacts"], paths["runtime"], paths["home"], paths["home"] / ".codex", paths["manifest"].parent):
        parts = end.relative_to(config.data).parts
        directories.update(config.data / Path(*parts[:index]) for index in range(1, len(parts) + 1))
    targets = [(path, True) for path in sorted(directories, key=lambda p: (len(p.parts), str(p)))]
    targets += [(path, False) for path in (paths["manifest"], paths["token"], paths["manifest"].with_suffix(".lock"))
                if path.exists() or path.is_symlink()]
    checked = []
    for path, directory in targets:
        if path.is_symlink():
            raise ValueError("Refusing a symlink in restored worker paths.")
        metadata = path.stat()
        correct_type = stat.S_ISDIR(metadata.st_mode) if directory else stat.S_ISREG(metadata.st_mode)
        if not correct_type or metadata.st_uid != config.uid or (not directory and metadata.st_nlink != 1):
            raise ValueError("Restored worker paths have foreign ownership, type, or links; permissions were preserved.")
        checked.append((path, directory, metadata))
    manifest_value(paths["manifest"], config, name)
    if paths["token"].exists():
        token_value(paths["token"])
    return checked


def repair_worker_permissions(config, name, lock_descriptor):
    targets = restore_permission_targets(config, name)
    lock_path = layout(config, name)["manifest"].with_suffix(".lock")
    with ExitStack() as stack:
        fixes = []
        for path, directory, expected in targets:
            if path == lock_path:
                descriptor = lock_descriptor
            else:
                descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | (os.O_DIRECTORY if directory else 0))
                stack.callback(os.close, descriptor)
            actual = os.fstat(descriptor)
            correct_type = stat.S_ISDIR(actual.st_mode) if directory else stat.S_ISREG(actual.st_mode)
            if not correct_type or actual.st_uid != config.uid or (actual.st_dev, actual.st_ino) != (expected.st_dev, expected.st_ino) or (not directory and actual.st_nlink != 1):
                raise ValueError("Restored worker paths changed before permission repair; no permissions were changed.")
            fixes.append((descriptor, 0o700 if directory else 0o600))
        for descriptor, mode in fixes:
            os.fchmod(descriptor, mode)


def create_layout(config, name):
    paths = layout(config, name)
    if any(paths[key].exists() or paths[key].is_symlink() for key in ("source", "artifacts", "runtime", "manifest")):
        raise ValueError("Worker identity already has private data; use explicit restore, never overwrite it.")
    for key in ("source", "artifacts", "runtime", "home"):
        private_directory(paths[key], config, create=True)
    private_directory(paths["home"] / ".codex", config, create=True)
    return paths


def validate_layout(config, name):
    paths = layout(config, name)
    for key in ("source", "artifacts", "runtime", "home"):
        private_directory(paths[key], config)
    private_directory(paths["home"] / ".codex", config)
    if paths["token"].exists() or paths["token"].is_symlink():
        private_file(paths["token"], config.uid)
    return paths


def write_token(paths):
    if paths["token"].exists() or paths["token"].is_symlink():
        private_file(paths["token"], os.getuid())
    token = secrets.token_urlsafe(48)
    atomic_write(paths["token"], token + "\n")
    return token


def read_token(paths, uid):
    private_file(paths["token"], uid)
    return token_value(paths["token"])


def token_value(path):
    token = path.read_text().strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{64}", token):
        raise ValueError("Invalid worker capability token; existing definitions were preserved.")
    return token


def image_info(reference):
    return json.loads(execute([*DOCKER, "image", "inspect", reference], capture=True))[0]


def validate_image(image):
    declared = image.get("Config") or {}
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", image.get("Id", "")) or declared.get("Volumes") or any(not isinstance(declared.get(key), str) for key in ("User", "WorkingDir")):
        raise ValueError("Worker image requires immutable provenance, explicit inherited user/workdir metadata, and no implicit volumes.")
    return image


def resolve_image(manifest, *, existing=None):
    reference = manifest["imageId"] if manifest["imageKind"] == "custom" else (existing["Image"] if existing else manifest["image"])
    image = validate_image(image_info(reference))
    if manifest["imageKind"] == "custom" and image["Id"] != manifest["imageId"]:
        raise ValueError("The original custom image ID is unavailable; a changed image was not substituted.")
    return image


def inspect_optional(kind, name):
    command = [*DOCKER, "ps", "--all", "--no-trunc"] if kind == "container" else [*DOCKER, "network", "ls", "--no-trunc"]
    records = [json.loads(line) for line in execute([*command, "--format", "{{json .}}"], capture=True).splitlines()]
    matches = [record for record in records if record.get("Names" if kind == "container" else "Name") == name]
    if len(matches) > 1:
        raise ValueError("Ambiguous existing Docker identity; preserved all definitions.")
    return json.loads(execute([*DOCKER, kind, "inspect", matches[0]["ID"]], capture=True))[0] if matches else None


def network_command(config, paths):
    return [*DOCKER, "network", "create", "--driver", "bridge", "--label", f"{REALM_LABEL}={config.project}",
            "--label", f"{ROLE_LABEL}=worker-network", paths["network"]]


def executor_command(token):
    return ["codex", "exec-server", "--listen", "ws://0.0.0.0:5000", "--ws-auth", "capability-token",
            "--ws-token-sha256", hashlib.sha256(token.encode()).hexdigest()]


def mounts(manifest, paths):
    result = [(paths["source"], "/work/source", True), (paths["artifacts"], "/work/artifacts", True), (paths["home"], "/home/node", True)]
    for key, target in (("inputsDir", "/run/grok-codex/inputs"), ("secretsDir", "/run/secrets")):
        if manifest[key]:
            result.append((Path(manifest[key]), target, False))
    return result


def create_command(manifest, paths, token, image_id):
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id) or (manifest["imageKind"] == "custom" and image_id != manifest["imageId"]):
        raise ValueError("Worker creation requires the selected immutable image ID.")
    command = [*DOCKER, "create", "--name", paths["identity"], "--init", "--restart", "no", "--network", paths["network"],
               "--label", f"{REALM_LABEL}={manifest['project']}", "--label", f"{ROLE_LABEL}=worker",
               "--label", f"io.grok-codex.environment={paths['identity']}",
               "--label", f"io.grok-codex.image-reference={manifest['image']}", "--cpus", str(manifest["cpus"]),
               "--memory", manifest["memory"], "--pids-limit", str(manifest["pidsLimit"]), "--shm-size", "512m"]
    for key, value in SYSCTLS.items():
        command += ["--sysctl", key + "=" + value]
    for port in (5000, manifest["browserPort"]):
        if port is not None:
            command += ["--publish", f"127.0.0.1::{port}/tcp"]
    if manifest["envFile"]:
        command += ["--env-file", manifest["envFile"]]
    for source, target, writable in mounts(manifest, paths):
        command += ["--mount", f"type=bind,src={source},dst={target}" + ("" if writable else ",readonly")]
    return [*command, image_id, *executor_command(token)]


def validate_network(network, config, paths):
    labels = network.get("Labels") or {}
    occupants = (network.get("Containers") or {}).values()
    if network.get("Name") != paths["network"] or not re.fullmatch(r"[0-9a-f]{64}", network.get("Id", "")) or network.get("Driver") != "bridge" or network.get("EnableIPv6") is not False or labels.get(REALM_LABEL) != config.project or labels.get(ROLE_LABEL) != "worker-network" or any(value.get("Name") != paths["identity"] for value in occupants):
        raise ValueError("Existing Docker network is foreign or has changed; preserved it.")


def validate_container(container, config, manifest, paths, image, token):
    validate_image(image)
    declared, host = container.get("Config") or {}, container.get("HostConfig") or {}
    labels = declared.get("Labels") or {}
    actual_mounts = sorted((m.get("Type"), m.get("Source"), m.get("Destination"), m.get("RW")) for m in container.get("Mounts") or [])
    expected_mounts = sorted(("bind", str(source), target, writable) for source, target, writable in mounts(manifest, paths))
    expected_env = dict(item.split("=", 1) for item in image.get("Config", {}).get("Env") or []) | env_values(manifest["envFile"])
    restart = host.get("RestartPolicy")
    restart_safe = isinstance(restart, dict) and set(restart) == {"Name", "MaximumRetryCount"} and restart["Name"] == "no" and type(restart["MaximumRetryCount"]) is int and restart["MaximumRetryCount"] == 0
    checks = [container.get("Name") == "/" + paths["identity"], labels.get(REALM_LABEL) == config.project,
              labels.get(ROLE_LABEL) == "worker", labels.get("io.grok-codex.environment") == paths["identity"],
              labels.get("io.grok-codex.image-reference") == manifest["image"],
              declared.get("Image") == image["Id"], container.get("Image") == image["Id"],
              declared.get("User") == image["Config"]["User"], declared.get("WorkingDir") == image["Config"]["WorkingDir"],
              declared.get("Cmd") == executor_command(token), declared.get("Entrypoint") == image.get("Config", {}).get("Entrypoint"),
              dict(item.split("=", 1) for item in declared.get("Env") or []) == expected_env,
              host.get("NetworkMode") == paths["network"], host.get("Init") is True,
              host.get("NanoCpus") == int(manifest["cpus"] * 1_000_000_000), host.get("Memory") == memory_bytes(manifest["memory"]),
              host.get("PidsLimit") == manifest["pidsLimit"], host.get("ShmSize") == 512 * 1024 ** 2,
              host.get("Sysctls") == SYSCTLS, not host.get("Privileged"), not host.get("PublishAllPorts"),
              restart_safe,
              not any(host.get(key) for key in ("CapAdd", "CapDrop", "Devices", "DeviceRequests", "PidMode", "UTSMode", "SecurityOpt")),
              host.get("IpcMode") in (None, "", "private"), actual_mounts == expected_mounts,
              all(m.get("Propagation") in (None, "", "rprivate") for m in container.get("Mounts") or [])]
    bindings = host.get("PortBindings") or {}
    expected_ports = {"5000/tcp"} | ({str(manifest["browserPort"]) + "/tcp"} if manifest["browserPort"] else set())
    checks.append(set(bindings) == expected_ports and all(len(rows) == 1 and rows[0].get("HostIp") == "127.0.0.1" for rows in bindings.values()))
    networks = container.get("NetworkSettings", {}).get("Networks") or {}
    checks.append(not networks or set(networks) == {paths["network"]})
    if not all(checks):
        raise ValueError("Existing worker differs from its launch manifest; definitions and private data were preserved.")
