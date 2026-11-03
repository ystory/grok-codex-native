import argparse
import json
from pathlib import Path
import subprocess
import sys
from uuid import uuid4

from pydantic import RootModel

from grok_codex_native.client import connect, require_chatgpt
from grok_codex_native.purge import purge


def wait(thread, turn):
    completed = subprocess.run([sys.executable, "-m", "grok_codex_native.handoff", "--thread", thread,
                                "--turn", turn], capture_output=True, text=True, timeout=120)
    assert completed.returncode == 0, (completed.stdout, completed.stderr)
    return json.loads(completed.stdout)


def main():
    parser = argparse.ArgumentParser(description="Verify native cross-call controls, queue, followup and disposable deletion")
    parser.add_argument("--worker", required=True)
    parser.add_argument("--run-models", action="store_true", required=True)
    args = parser.parse_args()
    roots = [{"environmentId": args.worker, "cwd": "/work/source",
              "runtimeWorkspaceRoots": ["/work/source", "/work/artifacts"]}]
    with connect() as c:
        c.initialize()
        require_chatgpt(c)
        thread = c.thread_start({"environments": roots, "approvalPolicy": "never"}).thread.id
        c.thread_set_name(thread, "Disposable lifecycle verification")
        folder = Path("/var/lib/grok-codex/artifacts") / args.worker / thread
        folder.mkdir(parents=True)
        c.request("thread/attachment/add", {"threadId": thread, "attachmentType": "grok.artifact",
                  "identityKey": thread, "payload": {"worker": args.worker, "threadId": thread, "path": str(folder)}},
                  response_model=RootModel[dict])
        turn = c.turn_start(thread, "Run sleep 60 in the shell. Wait until it finishes before answering.",
                            {"sandboxPolicy": {"type": "externalSandbox", "networkAccess": "enabled"}}).turn.id
    with connect() as c:
        c.initialize()
        snapshot = c.request("thread/read", {"threadId": thread}, response_model=RootModel[dict]).root["thread"]
        assert snapshot["status"]["type"] == "active", snapshot["status"]
        c.turn_steer(thread, turn, "After waiting, report LIFECYCLE_STEER_ACCEPTED.")
        queued = c.request("thread/queue/add", {"threadId": thread, "clientUserMessageId": str(uuid4()),
                           "input": [{"type": "text", "text": "Report QUEUED_LIFECYCLE_PROBE."}]}, response_model=RootModel[dict]).root
        print(json.dumps({"threadId": thread, "turnId": turn, "queueAccepted": queued}), flush=True)
        try:
            purge(c, thread, Path("/var/lib/grok-codex/artifacts"), thread, dry_run=True)
        except ValueError:
            pass
        else:
            raise AssertionError("active deletion preflight must refuse")
        c.turn_interrupt(thread, turn)
        c.request("thread/queue/delete", {"threadId": thread, "queuedSubmissionId": queued["queuedSubmission"]["id"]},
                  response_model=RootModel[dict])
    interrupted = wait(thread, turn)
    assert interrupted["turns"][-1]["status"] == "interrupted", interrupted
    with connect() as c:
        c.initialize()
        c.thread_archive(thread)
        c.thread_unarchive(thread)
        unloaded = c.request("thread/read", {"threadId": thread}, response_model=RootModel[dict]).root["thread"]
        assert unloaded["status"]["type"] == "notLoaded", unloaded["status"]
        c.thread_resume(thread, {"excludeTurns": True})
        followup = c.turn_start(thread, f"Use the shell to write exactly FOLLOWUP_OK to /work/artifacts/{thread}/followup.txt. Do no other work.",
                                {"environments": roots,
                                 "sandboxPolicy": {"type": "externalSandbox", "networkAccess": "enabled"}}).turn.id
    finished = wait(thread, followup)
    assert finished["turns"][-1]["status"] == "completed", finished
    assert (folder / "followup.txt").read_text().strip() == "FOLLOWUP_OK"
    with connect() as c:
        c.initialize()
        preflight = purge(c, thread, Path("/var/lib/grok-codex/artifacts"), thread, dry_run=True)
        deleted = purge(c, thread, Path("/var/lib/grok-codex/artifacts"), thread)
        assert deleted["nativeDeleteOutcome"] == "succeeded", deleted
        assert not folder.exists()
    print(json.dumps({"verified": ["cross-call status", "steer", "native queue admission/cancellation", "interrupt",
                                   "archive/unarchive", "unloaded followup with explicit environment",
                                   "runnable deletion refusal", "stopped disposable deletion"],
                      "threadId": thread, "interruptedTurn": turn, "followupTurn": followup,
                      "preflight": preflight, "deleted": deleted}), flush=True)


if __name__ == "__main__":
    main()
