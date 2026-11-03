import argparse
import json
from pathlib import Path
import subprocess
import sys
import urllib.request
from uuid import uuid4

from pydantic import RootModel

from grok_codex_native.client import connect, require_chatgpt
from grok_codex_native.handoff import read_transcript


def main():
    parser = argparse.ArgumentParser(description="Exercise disposable native tasks on two provisioned workers using the existing subscription")
    parser.add_argument("--worker-a", required=True)
    parser.add_argument("--worker-b", required=True)
    parser.add_argument("--port-a", required=True, type=int)
    parser.add_argument("--port-b", required=True, type=int)
    parser.add_argument("--run-models", action="store_true", required=True)
    parser.add_argument("--timeout", type=int, default=240)
    args = parser.parse_args()
    if args.worker_a == args.worker_b or args.port_a == args.port_b:
        parser.error("two distinct workers and published ports are required")
    marker = "native-smoke-" + uuid4().hex
    processes = []
    for worker, version in ((args.worker_a, "3.10"), (args.worker_b, "3.11")):
        text = f"""This is a disposable deployment verification. Do only the following.
Create /work/source/.venv with python3 -m venv, install idna=={version} inside it, and verify its actual installed version.
Write /work/source/source-proof.txt with exactly {marker}-{version}.
In the delivery directory supplied by the next instruction, write proof.txt with exactly {marker}-{version}, and dependency.txt with the installed idna version.
Create /work/source/server.py using Python http.server so GET returns exactly {marker}-{version}. Listen on 0.0.0.0:3000.
Start it detached using setsid -f python3 /work/source/server.py with stdin closed and stdout/stderr redirected to a file, so it survives the shell command.
Verify http://127.0.0.1:3000 locally. Generate a screenshot proof.png in the delivery directory with the installed headless chromium. Docker is the browser's outer sandbox, use --no-sandbox inside this isolated worker.
Do not change authentication, Docker settings, or any other work."""
        brief = {
            "worker": worker, "origin": "deployment-smoke", "title": "Disposable deployment verification",
            "thread": {"environments": [{"environmentId": worker, "cwd": "/work/source",
                                          "runtimeWorkspaceRoots": ["/work/source", "/work/artifacts"]}]},
            "input": [{"type": "text", "text": text}],
        }
        proc = subprocess.Popen([sys.executable, "examples/launch_and_wait.py", "/dev/stdin"],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        proc.stdin.write(json.dumps(brief))
        proc.stdin.close()
        proc.stdin = None
        processes.append((worker, version, proc))
    results = []
    for worker, version, proc in processes:
        stdout, stderr = proc.communicate(timeout=args.timeout)
        if proc.returncode:
            raise RuntimeError(json.dumps({"worker": worker, "exitCode": proc.returncode, "stdout": stdout, "stderr": stderr}))
        records = [json.loads(line) for line in stdout.splitlines()]
        finished = records[-1]
        assert finished["turns"][-1]["status"] == "completed", finished
        tid = finished["threadId"]
        root = Path("/var/lib/grok-codex/artifacts") / worker / tid
        assert (root / "proof.txt").read_text().strip() == marker + "-" + version
        assert (root / "dependency.txt").read_text().strip() == version
        assert (root / "proof.png").read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
        with connect() as client:
            client.initialize()
            require_chatgpt(client)
            turns = read_transcript(client, tid)
            assert any(item["type"] == "commandExecution" for turn in turns for item in turn["items"])
            assert all(turn["itemsView"] == "full" for turn in turns)
        results.append({"worker": worker, "threadId": tid, "turnId": finished["after"],
                        "dependencyVersion": version, "artifacts": finished["artifacts"]})
    for port, version in ((args.port_a, "3.10"), (args.port_b, "3.11")):
        with urllib.request.urlopen(f"http://127.0.0.1:{port}", timeout=10) as response:
            assert response.read().decode().strip() == marker + "-" + version
    print(json.dumps({"verified": ["native launch", "finite handoff", "full transcript", "two private dependencies",
                                   "two simultaneous internal port 3000 listeners", "published browser targets", "PNG artifacts"],
                      "tasks": results}), flush=True)


if __name__ == "__main__":
    main()
