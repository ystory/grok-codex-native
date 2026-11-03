#!/usr/bin/env python3
"""Install Docker on Grok's own Debian cloud computer; never contact another host."""
import argparse
import base64
import grp
import hashlib
import json
import os
from pathlib import Path
import platform
import pwd
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time


PACKAGES = ("docker-ce", "docker-ce-cli", "containerd.io", "docker-buildx-plugin", "docker-compose-plugin", "iproute2")
DOCKER = ["docker", "--host", "unix:///var/run/docker.sock"]
DEBIAN_SOURCES = Path("/etc/apt/sources.list.d/debian.sources")
KEY = Path("/etc/apt/keyrings/grok-codex-docker.asc")
REPOSITORY = Path("/etc/apt/sources.list.d/grok-codex-docker.sources")


def digest(content):
    return hashlib.sha256(content).hexdigest() if content is not None else None


def contents(path):
    if path.is_symlink():
        raise RuntimeError(f"Refusing symlink: {path}")
    return path.read_bytes() if path.exists() else None


def atomic_write(path, content, mode):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".grok-codex-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
            os.fchmod(stream.fileno(), mode)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class Journal:
    def __init__(self, directory):
        self.directory = directory
        self.path = directory / "receipt.json"
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.data = json.loads(self.path.read_text()) if self.path.exists() else {"configs": {}}

    def save(self):
        atomic_write(self.path, (json.dumps(self.data, indent=2) + "\n").encode(), 0o600)

    def check_configs(self):
        for name, entry in self.data["configs"].items():
            current = digest(contents(Path(name)))
            if current not in (entry["before_sha256"], entry["after_sha256"]):
                raise RuntimeError(f"Configuration changed outside this installer: {name}")

    def write_config(self, path, value):
        current = contents(path)
        entry = self.data["configs"].get(str(path))
        if entry is None:
            metadata = path.stat() if current is not None else None
            entry = {
                "before": base64.b64encode(current).decode() if current is not None else None,
                "before_sha256": digest(current), "after_sha256": digest(value),
                "mode": stat.S_IMODE(metadata.st_mode) if metadata else 0o644,
                "uid": metadata.st_uid if metadata else os.geteuid(),
                "gid": metadata.st_gid if metadata else os.getegid(),
            }
            self.data["configs"][str(path)] = entry
            self.save()
        if digest(current) not in (entry["before_sha256"], entry["after_sha256"]):
            raise RuntimeError(f"Configuration changed outside this installer: {path}")
        if digest(value) != entry["after_sha256"]:
            raise RuntimeError(f"Managed configuration changed; rollback before replacing it: {path}")
        if current != value:
            atomic_write(path, value, 0o644)
            if os.geteuid() == 0:
                os.chown(path, entry["uid"], entry["gid"])

    def restore_configs(self):
        self.check_configs()
        for name, entry in self.data["configs"].items():
            path = Path(name)
            current = contents(path)
            if digest(current) == entry["before_sha256"]:
                continue
            if entry["before"] is None:
                archive = self.directory / "retired-configs" / entry["after_sha256"]
                atomic_write(archive, current, 0o600)
                path.unlink()
            else:
                atomic_write(path, base64.b64decode(entry["before"]), entry["mode"])
                if os.geteuid() == 0:
                    os.chown(path, entry["uid"], entry["gid"])


def debian_mirrors(source):
    replacements = {
        f"{scheme}://deb.debian.org/{suffix}": target
        for scheme in ("http", "https")
        for suffix, target in (
            ("debian", "https://ftp.debian.org/debian"),
            ("debian-security", "https://security.debian.org/debian-security"),
        )
    }
    return re.sub(r"(?m)^(URIs:[ \t]*)([^\n]+)",
                  lambda match: match[1] + " ".join(replacements.get(uri, uri) for uri in match[2].split()), source)


def run(command, *, capture=False, check=True):
    print("+ " + " ".join(map(str, command)), flush=True)
    return subprocess.run(command, text=True, capture_output=capture, check=check)


def host():
    values = dict(line.split("=", 1) for line in Path("/etc/os-release").read_text().splitlines() if "=" in line)
    architecture = {"x86_64": "amd64", "aarch64": "arm64"}.get(platform.machine())
    if sys.platform != "linux" or values.get("ID", "").strip('"') != "debian" or values.get("VERSION_ID", "").strip('"') != "13" or not architecture:
        raise RuntimeError("This bootstrap supports the observed Debian 13 amd64/arm64 Grok cloud computer only.")
    return architecture


def verify():
    result = run(DOCKER + ["info", "--format", "{{json .}}"], capture=True)
    info = json.loads(result.stdout)
    if info["OSType"] != "linux":
        raise RuntimeError("Docker must execute Linux containers.")
    compose = run(DOCKER + ["compose", "version", "--short"], capture=True).stdout.strip()
    buildx = run(DOCKER + ["buildx", "version"], capture=True).stdout.strip()
    print(json.dumps({"dockerVersion": info["ServerVersion"], "composeVersion": compose,
                      "buildxVersion": buildx, "socket": "unix:///var/run/docker.sock"}))


def packages():
    output = run(["dpkg-query", "-W", "-f=${db:Status-Abbrev}\\t${binary:Package}\\t${Version}\\n"], capture=True).stdout
    return installed_packages(output)


def installed_packages(output):
    rows = (line.split("\t", 2) for line in output.splitlines())
    return {name: version for status, name, version in rows if status.startswith("ii")}


def daemon_running():
    return shutil.which("docker") is not None and run(DOCKER + ["info"], capture=True, check=False).returncode == 0


def process_start(pid):
    return Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()[19]


def start_daemon(journal):
    if daemon_running():
        if "daemon" not in journal.data:
            pidfile = Path("/var/run/docker.pid")
            if not pidfile.exists():
                raise RuntimeError("Docker became available without a traceable service PID; inspect it before continuing.")
            pid = int(pidfile.read_text())
            if Path(f"/proc/{pid}/exe").resolve().name != "dockerd":
                raise RuntimeError("Docker's service PID does not identify dockerd; preserved it.")
            command = Path(f"/proc/{pid}/cmdline").read_bytes().rstrip(b"\0").decode().split("\0")
            journal.data["daemon"] = {"pid": pid, "start": process_start(pid), "command": command}
            journal.save()
        return
    daemon = journal.data.get("daemon")
    if daemon and Path(f"/proc/{daemon['pid']}").exists():
        raise RuntimeError(f"Recorded Docker PID is still present but not ready; inspect {journal.directory / 'dockerd.log'}.")
    if Path("/run/systemd/system").exists():
        raise RuntimeError("Existing systemd service management is outside this non-systemd Grok bootstrap.")
    command = ["dockerd", "--host=unix:///var/run/docker.sock", "--bridge=none",
               "--data-root=/var/lib/grok-codex-docker", "--exec-root=/run/grok-codex-docker",
               f"--pidfile={journal.directory / 'dockerd.pid'}"]
    log = journal.directory / "dockerd.log"
    with log.open("ab") as stream:
        os.chmod(log, 0o600)
        process = subprocess.Popen(command, stdout=stream, stderr=subprocess.STDOUT, start_new_session=True)
    journal.data["daemon"] = {"pid": process.pid, "start": process_start(process.pid), "command": command}
    journal.save()
    for _ in range(60):
        if daemon_running():
            return
        if process.poll() is not None:
            break
        time.sleep(1)
    raise RuntimeError(f"Docker did not become ready; inspect {log}. Defaults were not weakened.")


def validate_docker_user(journal, name):
    if not name:
        return
    try:
        user = pwd.getpwnam(name)
    except KeyError as error:
        raise RuntimeError(f"--docker-user must name an existing user: {name}") from error
    selected = journal.data.get("docker_user") or journal.data.get("added_docker_user")
    if selected and selected != name:
        raise RuntimeError(f"This receipt already selects Docker user {selected}; refusing {name}.")
    if journal.data.get("docker_user") != name:
        journal.data["docker_user"] = name
        journal.save()
    return user


def docker_user(journal, name):
    user = validate_docker_user(journal, name)
    if user is None:
        return
    group = grp.getgrnam("docker")
    if user.pw_gid != group.gr_gid and name not in group.gr_mem:
        journal.data["added_docker_user"] = name
        journal.save()
        run(["usermod", "--append", "--groups", "docker", name])
    print(f"Use a fresh login shell for {name}, or sg docker -c 'COMMAND', to obtain Docker group membership.")


def install(journal, architecture, username):
    validate_docker_user(journal, username)
    journal.check_configs()
    if daemon_running():
        if "packages_before" in journal.data:
            start_daemon(journal)
        docker_user(journal, username)
        verify()
        journal.data["status"] = "installed"
        journal.save()
        return
    if shutil.which("docker") and "packages_before" not in journal.data:
        raise RuntimeError("Existing Docker is not reachable. Refusing to replace an unmanaged installation.")
    if "packages_before" not in journal.data:
        before = packages()
        conflicts = sorted(set(before) & {"docker.io", "docker-compose", "podman-docker", "containerd", "runc"})
        if conflicts:
            raise RuntimeError(f"Conflicting existing packages were preserved: {conflicts}")
        journal.data["packages_before"] = before
        journal.save()
    if DEBIAN_SOURCES.exists():
        journal.write_config(DEBIAN_SOURCES, debian_mirrors(DEBIAN_SOURCES.read_text()).encode())
    key = run(["curl", "--fail", "--silent", "--show-error", "--location", "--connect-timeout", "15", "--max-time", "120", "https://download.docker.com/linux/debian/gpg"], capture=True).stdout
    if not key.startswith("-----BEGIN PGP PUBLIC KEY BLOCK-----"):
        raise RuntimeError("Docker's official signing-key response was invalid.")
    journal.write_config(KEY, key.encode())
    repository = f"Types: deb\nURIs: https://download.docker.com/linux/debian\nSuites: trixie\nComponents: stable\nArchitectures: {architecture}\nSigned-By: {KEY}\n"
    journal.write_config(REPOSITORY, repository.encode())
    current = packages()
    if not all(package in current for package in PACKAGES):
        run(["apt-get", "-o", "APT::Update::Error-Mode=any", "update"])
        try:
            run(["apt-get", "install", "--yes", "--no-install-recommends", *PACKAGES])
        finally:
            journal.data["packages_after"] = packages()
            journal.save()
    start_daemon(journal)
    docker_user(journal, username)
    verify()
    journal.data["status"] = "installed"
    journal.save()


def rollback(journal):
    journal.check_configs()
    daemon = journal.data.get("daemon")
    if daemon and Path(f"/proc/{daemon['pid']}").exists():
        command = Path(f"/proc/{daemon['pid']}/cmdline").read_bytes().rstrip(b"\0").decode().split("\0")
        if process_start(daemon["pid"]) != daemon["start"] or command != daemon["command"]:
            raise RuntimeError("Recorded Docker PID now belongs to another process; preserved it.")
        if run(DOCKER + ["ps", "--quiet"], capture=True).stdout.strip():
            raise RuntimeError("Stop Docker workloads explicitly before rolling back the managed daemon.")
        os.kill(daemon["pid"], signal.SIGTERM)
        for _ in range(30):
            if not Path(f"/proc/{daemon['pid']}").exists():
                break
            time.sleep(1)
        else:
            raise RuntimeError("Docker has not exited; no forced termination was performed.")
    if name := journal.data.get("added_docker_user"):
        if name in grp.getgrnam("docker").gr_mem:
            run(["gpasswd", "--delete", name, "docker"])
    journal.restore_configs()
    journal.data["status"] = "rolled_back"
    journal.save()
    before, after = journal.data.get("packages_before", {}), journal.data.get("packages_after", {})
    print(json.dumps({"status": "configuration_and_owned_daemon_restored",
                      "packagesRetained": {name: version for name, version in after.items() if before.get(name) != version},
                      "note": "Packages, Docker data, project files, and credentials are never purged."}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("probe", "install", "verify", "rollback"))
    parser.add_argument("--state", type=Path, default=Path("/var/lib/grok-codex-bootstrap"))
    parser.add_argument("--docker-user", help="Explicitly grant this existing user Docker-group access (root-equivalent).")
    args = parser.parse_args()
    architecture = host()
    if args.action == "probe":
        print(json.dumps({"os": "debian13", "architecture": architecture,
                          "pid1": Path("/proc/1/comm").read_text().strip(),
                          "dockerCli": shutil.which("docker"), "root": os.geteuid() == 0}))
        return
    if args.action == "verify":
        verify()
        return
    if os.geteuid() != 0:
        parser.error("Run install/rollback with sudo -n python3; probe does not need root.")
    if not args.state.is_absolute() or any(path.is_symlink() for path in (args.state, *args.state.parents)):
        parser.error("Use an absolute, non-symlink state directory owned by root.")
    for path in (args.state, *args.state.parents):
        if path.exists() and (path.stat().st_uid != 0 or path.stat().st_mode & 0o022):
            parser.error("The state directory and its ancestors must be root-owned and not writable by group/others.")
    if args.action == "rollback" and not (args.state / "receipt.json").is_file():
        parser.error("No managed installation receipt exists; nothing was changed.")
    journal = Journal(args.state)
    install(journal, architecture, args.docker_user) if args.action == "install" else rollback(journal)


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, subprocess.CalledProcessError, KeyError) as error:
        raise SystemExit(f"Bootstrap failed: {error}") from error
