from dataclasses import dataclass
import json
from pathlib import Path
import re
import subprocess


PROJECT = Path(__file__).resolve().parent.parent
REALM_LABEL = "io.grok-codex.realm"
ROLE_LABEL = "io.grok-codex.role"


@dataclass(frozen=True)
class Deployment:
    data: Path
    project: str
    uid: int
    gid: int

    def worker_name(self, name: str) -> str:
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,39}", name):
            raise ValueError("Worker name must use 1 to 40 lowercase letters, digits, or hyphens.")
        return f"{self.project}-worker-{name}"


def deployment() -> Deployment:
    values = dict(line.split("=", 1) for line in (PROJECT / "deploy/.env").read_text().splitlines())
    return Deployment(
        data=Path(values["GROK_CODEX_DATA"]),
        project=values["GROK_CODEX_PROJECT"],
        uid=int(values["GROK_CODEX_UID"]),
        gid=int(values["GROK_CODEX_GID"]),
    )


def inspect_worker(config: Deployment, name: str) -> dict:
    worker = json.loads(subprocess.check_output(["docker", "inspect", config.worker_name(name)]))[0]
    labels = worker["Config"].get("Labels") or {}
    if labels.get(REALM_LABEL) != config.project or labels.get(ROLE_LABEL) != "worker":
        raise ValueError("Refusing a container outside this deployment's managed workers.")
    return worker


def compose(*args: str) -> list[str]:
    return [str(PROJECT / "scripts/compose.sh"), *args]
