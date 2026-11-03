#!/usr/bin/env python3
import json
import os
from pathlib import Path
import subprocess
import tempfile


project = Path(__file__).resolve().parent.parent
environment = dict(os.environ, GROK_CODEX_DATA="/tmp/grok-codex-config-check")
config = json.loads(subprocess.check_output([
    "docker", "compose", "-f", str(project / "deploy/compose.yaml"),
    "--profile", "build-worker", "config", "--format", "json",
], env=environment))
control = config["services"]["control"]
worker = config["services"]["worker-template"]
assert control["network_mode"] == "host"
assert worker["profiles"] == ["build-worker"] and worker["entrypoint"] == ["/bin/false"]
assert "network_mode" not in worker
assert {v["target"] for v in control["volumes"]} == {
    "/var/lib/grok-codex/codex", "/var/lib/grok-codex/artifacts", "/run/grok-codex",
}
assert all("docker.sock" not in v["source"] for v in control["volumes"])
assert control["environment"]["OPENAI_API_KEY"] == control["environment"]["CODEX_API_KEY"] == ""
assert control["pids_limit"] > 0 and control["cpus"] > 0
assert worker["build"]["target"] == "worker" and control["build"]["target"] == "control"
lock = json.loads((project / "deploy/package-lock.json").read_text())
for architecture in ("x64", "arm64"):
    native = lock["packages"][f"node_modules/@openai/codex-linux-{architecture}"]
    assert native["version"] == "0.166.0-linux-" + architecture and native["integrity"].startswith("sha512-")
with tempfile.TemporaryDirectory(prefix="grok-codex-python-check-") as cache:
    subprocess.run([
        "python3", "-m", "compileall", "-q", str(project / "deploy"), str(project / "scripts"),
    ], env=dict(os.environ, PYTHONPYCACHEPREFIX=cache), check=True)
print("Compose ownership, isolation configuration, native integrity locks, and Python parsing passed.")
