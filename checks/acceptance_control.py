"""Exercise steer, native queue, interrupt, follow-up, and retained lifecycle state."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import socket
import subprocess
import sys
import time
from uuid import uuid4

from openai_codex.generated.v2_all import ThreadSourceKind
from pydantic import RootModel

from grok_codex_native.client import connect, require_chatgpt
from grok_codex_native.handoff import read_transcript, wait_for_completion


def stamp():
    return {"utc": datetime.now(timezone.utc).isoformat(), "epoch": time.time()}


def write(path, value, *, new=False):
    with path.open("x" if new else "w") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    path.chmod(0o600)


def require(condition, message):
    if not condition:
        raise ValueError(message)


def rpc(client, method, **params):
    return client.request(method, params, response_model=RootModel[dict]).root


def queue(client, thread):
    rows, cursor = [], None
    while True:
        page = rpc(client, "thread/queue/list", threadId=thread, **({"cursor": cursor} if cursor else {}))
        rows.extend(page["data"])
        cursor = page.get("nextCursor")
        if cursor is None:
            return rows


def listed(client, thread, archived):
    cursor = None
    while True:
        page = client.thread_list({"archived": archived, "sourceKinds": [s.value for s in ThreadSourceKind],
                                   "cursor": cursor}).model_dump(by_alias=True, mode="json")
        for item in page["data"]:
            if item["id"] == thread:
                return item
        cursor = page["nextCursor"]
        if cursor is None:
            return None


def completed(thread, *, turn=None, after=None, folder):
    with connect() as client:
        client.initialize()
        return wait_for_completion(client, thread, turn_id=turn, after=after, artifacts=folder)


def observe_command(client, thread, turn_id, started_file, finished_file):
    deadline = time.monotonic() + 60
    sample = None
    while time.monotonic() < deadline:
        before = stamp()
        thread_state = client.thread_read(thread).model_dump(by_alias=True, mode="json")["thread"]
        after = stamp()
        sample = {"turnId": turn_id, "rpcStarted": before, "rpcFinished": after,
                  "rpcSeconds": after["epoch"] - before["epoch"], "threadStatus": thread_state["status"],
                  "started": json.loads(started_file.read_text()) if started_file.is_file() else None,
                  "finishedFileExists": finished_file.is_file(), "observed": stamp()}
        if sample["finishedFileExists"]:
            raise ValueError("Worker command finished before active observation: " + json.dumps(sample))
        if sample["started"] is not None and thread_state["status"]["type"] == "active":
            return sample
        time.sleep(0.25)
    raise TimeoutError("No active thread and start artifact within 60 seconds: " + json.dumps(sample))


def run(args):
    require(args.report_dir.is_absolute(), "report-dir must be absolute inside control")
    args.report_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
    report = args.report_dir / "report.json"
    state = {"runId": "acceptance-control-" + uuid4().hex[:12], "started": stamp(), "worker": args.worker,
             "expectedHostname": args.expected_hostname, "controlHostname": socket.gethostname(),
             "modelSettings": "deployment defaults", "status": "starting", "events": [], "passed": False}
    write(report, state, new=True)

    def save(stage, value=None):
        state["status"] = stage
        state["events"].append({"stage": stage, "at": stamp(), "value": value})
        write(report, state)
        print(json.dumps({"stage": stage, "threadId": state.get("threadId"), "value": value}), flush=True)

    roots = [{"environmentId": args.worker, "cwd": "/work/source",
              "runtimeWorkspaceRoots": ["/work/source", "/work/artifacts"]}]
    params = {"environments": roots, "sandboxPolicy": {"type": "externalSandbox", "networkAccess": "enabled"}}
    task_dir = "/work/source/" + state["runId"]
    brief = {"worker": args.worker, "origin": args.origin, "title": "Disposable control acceptance",
             "thread": {"environments": roots}, "input": [{"type": "text", "text": f"""This is a disposable control acceptance task.
Use python3 only, no subagents, network, installs, authentication changes, or other work.
Create {task_dir}. Run one foreground Python3 command which first calls os.chdir({task_dir!r}) inside Python, then writes a-started.json with marker={state['runId']!r}, hostname=socket.gethostname(), cwd=os.getcwd(), pid=os.getpid(), startEpoch=time.time(); actually sleeps 20 seconds; writes a-finished.json with the same marker and pid plus endEpoch=time.time(); then writes initial.txt with exactly {state['runId']!r} plus one newline.
Use absolute artifact paths under the /work/artifacts/THREAD_ID directory supplied in the next instruction. That directory is only the output location: never chdir into it. The Python command must stay in {task_dir}.
Wait for actual process completion. Do not leave it detached. Follow any later steer instruction before completing this turn."""}]}
    brief_path = args.report_dir / "brief.json"
    write(brief_path, brief, new=True)
    thread, folder = None, None
    try:
        stderr_path = args.report_dir / "launch.stderr"
        with stderr_path.open("x") as stderr:
            process = subprocess.Popen([sys.executable, "/opt/project/examples/launch_and_wait.py", "--no-wait", str(brief_path)],
                                       cwd="/opt/project", stdout=subprocess.PIPE, stderr=stderr, text=True)
            for line in process.stdout:
                record = json.loads(line)
                state.setdefault("launchRecords", []).append(record)
                for key in ("threadId", "turnId", "environmentId", "artifactRoot", "model"):
                    if key in record:
                        state[key] = record[key]
                write(report, state)
            code = process.wait()
        stderr_path.chmod(0o600)
        state["launchExitCode"] = code
        require(code == 0 and state.get("turnId"), "Launch failed; preserve accepted IDs and launch.stderr")
        thread, first = state["threadId"], state["turnId"]
        folder = Path(state["artifactRoot"])
        state["turnIds"] = {"a": first}
        save("a_accepted", {"turnId": first})
        with connect() as client:
            client.initialize()
            require_chatgpt(client)
            state["aActive"] = observe_command(client, thread, first, folder / "a-started.json", folder / "a-finished.json")
            save("a_running")
            state["steer"] = client.turn_steer(thread, first, f"After the running command finishes, use python3, call os.chdir({task_dir!r}) inside Python, and write exactly {state['runId']!r} plus one newline to the absolute path /work/artifacts/{thread}/steered.txt before ending this turn.").model_dump(by_alias=True, mode="json")
            require(client.thread_read(thread).model_dump(by_alias=True, mode="json")["thread"]["status"]["type"] == "active", "Queue only behind an active turn")
            cancelled = rpc(client, "thread/queue/add", threadId=thread, clientUserMessageId=str(uuid4()),
                            input=[{"type": "text", "text": f"Use python3, call os.chdir({task_dir!r}) inside Python, and write CANCELLED_MUST_NOT_RUN to the absolute path /work/artifacts/{thread}/cancelled.txt. Do no other work."}])
            state["cancelledQueue"] = cancelled
            write(report, state)
            state["queueDelete"] = rpc(client, "thread/queue/delete", threadId=thread,
                                        queuedSubmissionId=cancelled["queuedSubmission"]["id"])
            queued = rpc(client, "thread/queue/add", threadId=thread, clientUserMessageId=str(uuid4()), input=[{"type": "text", "text": f"Use one Python3 command which calls os.chdir({task_dir!r}) before constructing proof metadata. Read the absolute paths /work/artifacts/{thread}/initial.txt and /work/artifacts/{thread}/steered.txt and require both equal {state['runId']!r} plus newline. Only then write the absolute path /work/artifacts/{thread}/queued-proof.json with marker={state['runId']!r}, hostname=socket.gethostname(), cwd=os.getcwd(), initialMatched=true, steerMatched=true. Never chdir into the artifact directory. Do no other work."}])
            state["acceptedQueue"] = queued
            state["queueBeforeCompletion"] = queue(client, thread)
            save("steer_and_queue_accepted")
        state["aResult"] = completed(thread, turn=first, folder=folder)
        require(state["aResult"]["outcomes"][0]["status"] == "completed", "Steered first turn did not complete")
        state["aFinished"] = json.loads((folder / "a-finished.json").read_text())
        started = state["aActive"]["started"]
        require(started["startEpoch"] <= state["aActive"]["observed"]["epoch"] < state["aFinished"]["endEpoch"],
                "Control observation was outside the actual worker command interval")
        require(state["aFinished"].get("marker") == state["runId"] and state["aFinished"].get("pid") == started["pid"],
                "First command start/finish proofs do not match")
        require(any(i.get("type") == "commandExecution" and i.get("exitCode") == 0 and (i.get("durationMs") or 0) >= 19000
                    for i in state["aResult"]["turns"][0]["items"]), "First turn has no actual successful 20-second command")
        state["bResult"] = completed(thread, after=first, folder=folder)
        state["turnIds"]["b"] = state["bResult"]["after"]
        require(all(r["status"] == "completed" for r in state["bResult"]["outcomes"]), "Queued follow-up failed")
        require((folder / "initial.txt").read_text() == state["runId"] + "\n", "Initial marker mismatch")
        require((folder / "steered.txt").read_text() == state["runId"] + "\n", "Steer marker mismatch")
        require(not (folder / "cancelled.txt").exists(), "Cancelled queue item executed")
        queued_proof = json.loads((folder / "queued-proof.json").read_text())
        require(queued_proof.get("marker") == state["runId"] and queued_proof.get("initialMatched") is True
                and queued_proof.get("steerMatched") is True, "Queued execution proof mismatch")
        save("a_and_queued_b_completed", state["turnIds"])
        with connect() as client:
            client.initialize()
            require(not queue(client, thread), "Queue remains after automatic dispatch")
            state["cPreStart"] = {"status": client.thread_read(thread).model_dump(by_alias=True, mode="json")["thread"]["status"], "resume": None}
            write(report, state)
            if state["cPreStart"]["status"]["type"] == "notLoaded":
                state["cPreStart"]["resume"] = client.thread_resume(thread, {"excludeTurns": True}).model_dump(by_alias=True, mode="json")
            save("c_environment_ready", state["cPreStart"])
            interrupted = client.turn_start(thread, f"Run one foreground Python3 command which first calls os.chdir({task_dir!r}) inside Python, then writes the absolute path /work/artifacts/{thread}/interrupt-started.json with marker={state['runId']!r}, hostname=socket.gethostname(), cwd=os.getcwd(), pid=os.getpid(), startEpoch=time.time(); sleeps 120 seconds; then writes the absolute path /work/artifacts/{thread}/interrupt-finished.txt. Never chdir into the artifact directory. Wait for actual completion. Do no other work.", params).turn.id
            state["turnIds"]["c"] = interrupted
            save("c_accepted", {"turnId": interrupted})
            state["cActive"] = observe_command(client, thread, interrupted, folder / "interrupt-started.json", folder / "interrupt-finished.txt")
            state["interruptResponse"] = client.turn_interrupt(thread, interrupted).model_dump(by_alias=True, mode="json")
            save("c_interrupt_requested")
        state["cResult"] = completed(thread, turn=interrupted, folder=folder)
        require(state["cResult"]["outcomes"][0]["status"] == "interrupted", "Interrupt did not produce interrupted terminal state")
        state["cCommandEvidence"] = [{key: item.get(key) for key in ("id", "status", "exitCode", "durationMs", "startedAtMs", "completedAtMs")}
                                     for turn in state["cResult"]["turns"] for item in turn["items"] if item.get("type") == "commandExecution"]
        state["interruptedCommandHistoryPresent"] = bool(state["cCommandEvidence"])
        state["interruptedCommandHistoryLimitation"] = None if state["cCommandEvidence"] else "Native interrupted-turn history omitted commandExecution items; stop verification uses the observed worker PID, interrupted terminal state, replacement PID check, and absent completion marker."
        require(not (folder / "interrupt-finished.txt").exists(), "Interrupted command reached its completion marker")
        with connect() as client:
            client.initialize()
            require(not queue(client, thread), "Queue is not empty before replacement")
            state["dPreStart"] = {"status": client.thread_read(thread).model_dump(by_alias=True, mode="json")["thread"]["status"], "resume": None}
            write(report, state)
            if state["dPreStart"]["status"]["type"] == "notLoaded":
                state["dPreStart"]["resume"] = client.thread_resume(thread, {"excludeTurns": True}).model_dump(by_alias=True, mode="json")
            save("d_environment_ready", state["dPreStart"])
            replacement = client.turn_start(thread, f"Use one Python3 command which first calls os.chdir({task_dir!r}) before constructing proof metadata. Read only your own absolute path /work/artifacts/{thread}/interrupt-started.json. Check the saved pid by reading /proc/PID/stat if it exists; record its raw single-letter third stat field, or JSON null if absent. Do not kill anything. Write the absolute path /work/artifacts/{thread}/replacement-proof.json with marker={state['runId']!r}, hostname=socket.gethostname(), cwd=os.getcwd(), interruptedProcessState, and replacement=true. Never chdir into the artifact directory. Do no other work.", params).turn.id
            state["turnIds"]["d"] = replacement
            save("d_accepted", {"turnId": replacement})
        state["dResult"] = completed(thread, turn=replacement, folder=folder)
        require(state["dResult"]["outcomes"][0]["status"] == "completed", "Replacement did not complete")
        proof = json.loads((folder / "replacement-proof.json").read_text())
        require(proof.get("replacement") is True and proof.get("marker") == state["runId"], "Replacement proof mismatch")
        require(proof.get("interruptedProcessState") in (None, "Z"), "Interrupted worker process is still running")
        require(not (folder / "interrupt-finished.txt").exists(), "Interrupted command completed before replacement proof finished")
        state["executionProofs"] = {"a": state["aActive"]["started"], "b": queued_proof,
                                     "c": state["cActive"]["started"], "d": proof}
        for name, evidence in state["executionProofs"].items():
            require(evidence.get("hostname") == args.expected_hostname and evidence.get("cwd") == task_dir,
                    f"Explicit worker execution proof failed for {name}")
        with connect() as client:
            client.initialize()
            state["queueAtEnd"] = queue(client, thread)
            state["goalAtEnd"] = rpc(client, "thread/goal/get", threadId=thread)
            require(not state["queueAtEnd"], "Queue remains before lifecycle operations")
            goal = state["goalAtEnd"]["goal"]
            require(goal is None or goal["status"] in ("paused", "complete"), "Runnable or unknown goal preserved")
            turns = read_transcript(client, thread)
            require([t["id"] for t in turns] == [state["turnIds"][x] for x in "abcd"], "Unexpected native turn sequence")
            write(args.report_dir / "history-before-lifecycle.json", {"threadId": thread, "turns": turns}, new=True)
            renamed = "Completed disposable control acceptance " + state["runId"]
            client.thread_set_name(thread, renamed)
            client.thread_archive(thread)
            state["archivedListing"] = listed(client, thread, True)
            require(state["archivedListing"] is not None, "Archived task missing from archived listing")
            client.thread_unarchive(thread)
            state["restoredListing"] = listed(client, thread, False)
            require(state["restoredListing"] is not None, "Restored task missing from live listing")
            state["threadAtEnd"] = client.thread_read(thread).model_dump(by_alias=True, mode="json")["thread"]
            require(state["threadAtEnd"]["name"] == renamed, "Task rename was not retained")
            after = read_transcript(client, thread)
            require(after == turns, "Full turn history changed during archive/restore")
            write(args.report_dir / "history-after-lifecycle.json", {"threadId": thread, "turns": after}, new=True)
        state["passed"] = True
        state["finished"] = stamp()
        save("completed", {"turnIds": state["turnIds"], "artifacts": str(folder), "historyPreserved": True})
        return 0
    except Exception as error:
        state["error"] = {"type": type(error).__name__, "message": str(error)}
        if hasattr(error, "result"):
            state["nativeErrorResult"] = error.result
        if state.get("threadId"):
            try:
                with connect() as client:
                    client.initialize()
                    write(args.report_dir / "history-at-error.json",
                          {"threadId": state["threadId"], "turns": read_transcript(client, state["threadId"])}, new=True)
                    state["queueAtError"] = queue(client, state["threadId"])
            except Exception as capture_error:
                state["historyCaptureError"] = str(capture_error)
        state["finished"] = stamp()
        save("failed", state["error"])
        return 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", required=True)
    parser.add_argument("--expected-hostname", required=True, help="Exact safe docker inspect .Config.Hostname value")
    parser.add_argument("--origin", required=True)
    parser.add_argument("--report-dir", type=Path, required=True)
    parser.add_argument("--run-models", action="store_true", required=True)
    return run(parser.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
