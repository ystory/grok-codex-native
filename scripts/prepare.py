import argparse
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tomllib


def main():
    parser = argparse.ArgumentParser(description="Prepare project-local Docker assets on a Linux Docker host.")
    parser.add_argument("data", type=Path)
    parser.add_argument("--project", default="grok-codex")
    args = parser.parse_args()
    if sys.platform != "linux":
        parser.error("Deploy on Linux. Docker Desktop host-network behavior is not this deployment contract.")
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,39}", args.project):
        parser.error("Project must use 1 to 40 lowercase letters, digits, or hyphens.")
    project = Path(__file__).resolve().parent.parent
    data = args.data.resolve()
    if not args.data.is_absolute() or not re.fullmatch(r"/[A-Za-z0-9_./-]+", str(data)):
        parser.error("Use an absolute data path containing letters, digits, slash, dot, underscore, or hyphen.")
    if data == Path("/") or data == project or project in data.parents:
        parser.error("Keep private data outside the project checkout.")
    if shutil.which("docker") is None:
        parser.error("Docker is missing. Install Docker Engine with Compose before deployment.")
    engine = subprocess.check_output(["docker", "info", "--format", "{{.OSType}}"], text=True).strip()
    if engine != "linux":
        parser.error("The selected Docker engine must run Linux containers.")
    subprocess.run(["docker", "compose", "version"], check=True, capture_output=True)
    uid = os.getuid() or 1000
    gid = os.getgid() or 1000
    for directory in (data, *(data / n for n in ("codex", "artifacts", "workspaces", "runtime", "control-run"))):
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
    environment = project / "deploy/.env"
    content = f"GROK_CODEX_DATA={data}\nGROK_CODEX_PROJECT={args.project}\nGROK_CODEX_UID={uid}\nGROK_CODEX_GID={gid}\n"
    if environment.exists() and environment.read_text() != content:
        parser.error("deploy/.env already selects different data or identity. Preserve it and use a separate checkout.")
    environment.write_text(content)
    environment.chmod(0o600)
    print(f"Prepared {data}. Build with scripts/compose.sh --profile build-worker build.")


if __name__ == "__main__":
    main()
