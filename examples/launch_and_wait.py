import argparse
import json
import os
from pathlib import Path
import re
import sys

from pydantic import RootModel

from grok_codex_native.client import connect, require_chatgpt


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("brief", type=Path)
    parser.add_argument("--artifacts-root", type=Path, default=Path("/var/lib/grok-codex/artifacts"))
    parser.add_argument("--no-wait", action="store_true", help="Return accepted native IDs; attach a separate Grok Task waiter")
    args = parser.parse_args()
    brief = json.loads(args.brief.read_text())
    worker = brief["worker"]
    origin = brief["origin"]
    if not isinstance(worker, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}", worker):
        parser.error("worker must be the Docker container identity")
    if not isinstance(origin, str) or not origin.strip():
        parser.error("origin must identify the calling Bot or conversation")
    params = brief["thread"]
    selection = params.get("environments", [])
    if len(selection) != 1 or selection[0].get("environmentId") != worker or worker == "local":
        parser.error("thread must select exactly this worker environment")
    if not {"/work/source", "/work/artifacts"}.issubset(selection[0].get("runtimeWorkspaceRoots", [])):
        parser.error("worker workspace roots must include source and managed artifacts")
    if params.get("approvalPolicy") not in (None, "never") or params.get("sandbox") not in (None, "workspace-write"):
        parser.error("the deployment uses approvalPolicy=never and sandbox=workspace-write")
    if params.get("permissions") is not None:
        parser.error("custom permissions require a separately configured deployment")
    params = {**params, "approvalPolicy": "never", "sandbox": "workspace-write"}
    inputs = brief["input"]
    if not isinstance(inputs, list) or not inputs:
        parser.error("input must contain native Codex input items")

    with connect() as client:
        client.initialize()
        require_chatgpt(client)
        started = client.thread_start(params)
        tid = started.thread.id
        print(json.dumps({"threadId": tid, "environmentId": worker, "model": started.model}), flush=True)
        artifact_path = args.artifacts_root / worker / tid
        artifact_path.mkdir(parents=True, exist_ok=False, mode=0o700)
        client.thread_set_name(tid, brief.get("title") or worker)
        client.request("thread/attachment/add", {
            "threadId": tid, "attachmentType": "grok.origin", "identityKey": origin,
            "payload": {"worker": worker},
        }, response_model=RootModel[dict])
        client.request("thread/attachment/add", {
            "threadId": tid, "attachmentType": "grok.artifact", "identityKey": tid,
            "payload": {"threadId": tid, "worker": worker, "path": str(artifact_path)},
        }, response_model=RootModel[dict])
        task_inputs = inputs + [{"type": "text", "text": f"Place files intended for delivery in /work/artifacts/{tid}/."}]
        turn = client.turn_start(tid, task_inputs, {
            "sandboxPolicy": {"type": "externalSandbox", "networkAccess": "enabled"},
        })
        turn_id = turn.turn.id
        print(json.dumps({"threadId": tid, "turnId": turn_id, "artifactRoot": str(artifact_path)}), flush=True)

    if args.no_wait:
        return
    os.execv(sys.executable, [sys.executable, "-m", "grok_codex_native.handoff", "--thread", tid,
                             "--turn", turn_id, "--artifacts", str(artifact_path)])


if __name__ == "__main__":
    main()
