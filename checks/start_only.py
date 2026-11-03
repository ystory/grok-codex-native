import argparse
import json
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

from pydantic import RootModel

from grok_codex_native.client import connect
from grok_codex_native.handoff import read_transcript


def main():
    parser = argparse.ArgumentParser(description="Verify detached launch, separate waiters and native after-current dispatch")
    parser.add_argument("--worker", required=True)
    parser.add_argument("--run-models", action="store_true", required=True)
    args = parser.parse_args()
    project = Path(__file__).resolve().parents[1]
    brief = {
        "worker": args.worker, "origin": "disposable-start-only-verification",
        "title": "Disposable detached launch verification",
        "thread": {"model": "gpt-6.1-sol", "config": {"model_reasoning_effort": "low"},
                   "environments": [{"environmentId": args.worker, "cwd": "/work/source",
                                     "runtimeWorkspaceRoots": ["/work/source", "/work/artifacts"]}]},
        "input": [{"type": "text", "text": "Run sleep 10 in the shell, then write exactly FIRST_OK to first.txt in the delivery directory. Wait for the command and finish. Do no other work."}],
    }
    accepted = subprocess.run([sys.executable, str(project / "examples/launch_and_wait.py"), "/dev/stdin", "--no-wait"],
                              input=json.dumps(brief), capture_output=True, text=True, timeout=60)
    print(accepted.stdout, end="", flush=True)
    assert accepted.returncode == 0, accepted.stderr
    launched = json.loads(accepted.stdout.splitlines()[-1])
    thread, turn = launched["threadId"], launched["turnId"]
    folder = Path(launched["artifactRoot"])
    with connect() as client:
        client.initialize()
        snapshot = client.request("thread/read", {"threadId": thread}, response_model=RootModel[dict]).root["thread"]
        assert snapshot["status"]["type"] == "active", snapshot["status"]
        queued = client.request("thread/queue/add", {
            "threadId": thread, "clientUserMessageId": str(uuid4()),
            "input": [{"type": "text", "text": f"Read /work/artifacts/{thread}/first.txt with the shell. Only if its content is exactly FIRST_OK, write exactly QUEUE_OK to /work/artifacts/{thread}/second.txt. Finish. Do no other work."}],
        }, response_model=RootModel[dict]).root
    first = subprocess.run([sys.executable, "-m", "grok_codex_native.handoff", "--thread", thread,
                            "--turn", turn, "--artifacts", str(folder)], capture_output=True, text=True, timeout=180)
    assert first.returncode == 0, (first.stdout, first.stderr)
    second = subprocess.run([sys.executable, "-m", "grok_codex_native.handoff", "--thread", thread,
                             "--after", turn, "--artifacts", str(folder)], capture_output=True, text=True, timeout=180)
    assert second.returncode == 0, (second.stdout, second.stderr)
    result = json.loads(second.stdout)
    assert (folder / "first.txt").read_text() == "FIRST_OK"
    assert (folder / "second.txt").read_text() == "QUEUE_OK"
    assert result["turns"][-1]["status"] == "completed", result
    with connect() as client:
        client.initialize()
        turns = read_transcript(client, thread)
        remaining = client.request("thread/queue/list", {"threadId": thread}, response_model=RootModel[dict]).root
    assert len(turns) == 2 and all(item["status"] == "completed" for item in turns), turns
    assert not remaining["data"], remaining
    print(json.dumps({"verified": ["no-wait returns while active", "separate process waiters", "native automatic after-current dispatch", "ordered actual artifacts"],
                      "threadId": thread, "firstTurn": turn, "queuedSubmission": queued,
                      "secondTurn": result["turns"][-1]["id"], "artifacts": result["artifacts"]}), flush=True)


if __name__ == "__main__":
    main()
