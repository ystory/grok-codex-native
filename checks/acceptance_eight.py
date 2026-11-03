"""Launch, inspect, and collect eight disposable native tasks on existing workers."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
import time
from uuid import uuid4

from grok_codex_native.client import connect, require_chatgpt
from grok_codex_native.handoff import read_transcript, wait_for_completion


HOLD_SECONDS = 90


def now():
    return {"utc": datetime.now(timezone.utc).isoformat(), "epoch": time.time()}


def save(path, value, *, new=False):
    with path.open("x" if new else "w") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    path.chmod(0o600)


def brief_for(worker, index, run_id, origin, *, local_web_steps=None):
    marker = f"{run_id}-{index:02d}"
    restrictions = "Do not spawn subagents, access the network, install packages, or change authentication."
    final_restriction = "Do not change any other work or run a server."
    web_section = ""
    if local_web_steps is not None:
        restrictions = "Do not spawn subagents, access external networks, install packages, or change authentication. Only the specified loopback HTTP test is allowed."
        final_restriction = "Do not change any other work or run services beyond the specified temporary loopback server."
        web_section = "\n" + local_web_steps
    text = f"""This is one disposable concurrency acceptance task. Do only this small task.
Use python3, not python. {restrictions}
Work only in /work/source/{run_id} and the delivery directory supplied by the next instruction.
Create solution.py with a function score(values) which returns the sum of each value multiplied by its 1-based position, plus {index}.
Create test_solution.py using Python unittest with exactly three tests: score([]) == {index}; score([2, 3]) == {8 + index}; score([-1, 0, 4]) == {11 + index}.
Run python3 -m unittest -v and save its actual output to unit-tests.txt in the delivery directory. Stop and report an error if it fails.
Copy solution.py and test_solution.py to the delivery directory.{web_section}
Then run one Python process which records startEpoch=time.time() and startMonotonic=time.monotonic(), writes started.json in the delivery directory, actually sleeps {HOLD_SECONDS} seconds, and records endEpoch/endMonotonic.
That process must write proof.json containing marker={marker!r}, workerIndex={index}, caseResults=[{index}, {8 + index}, {11 + index}], startEpoch, endEpoch, startMonotonic, endMonotonic; compute caseResults by calling the real score function.
After the process completes, write marker.txt with exactly {marker!r} plus one newline.
Wait for actual process completion before ending your turn. Do not shorten the delay, fabricate times, or leave the process running after reporting completion.
{final_restriction}"""
    return {
        "worker": worker,
        "origin": origin,
        "title": f"Eight-worker acceptance {index:02d}",
        "thread": {"environments": [{"environmentId": worker, "cwd": "/work/source",
                                       "runtimeWorkspaceRoots": ["/work/source", "/work/artifacts"]}]},
        "input": [{"type": "text", "text": text}],
    }


def snapshot(state):
    result = {"started": now(), "observationMode": "sequential status reads; collected interval overlap is the concurrency proof", "workers": []}
    with connect() as client:
        client.initialize()
        require_chatgpt(client)
        for entry in state["workers"]:
            row = {"worker": entry["worker"], "threadId": entry.get("threadId"),
                   "turnId": entry.get("turnId"), "observed": now()}
            if row["threadId"]:
                try:
                    thread = client.thread_read(row["threadId"]).model_dump(by_alias=True, mode="json")["thread"]
                    turns = read_transcript(client, row["threadId"])
                    selected = next((turn for turn in turns if turn["id"] == row["turnId"]), None)
                    row["threadStatus"] = thread["status"]
                    row["turn"] = {key: selected.get(key) for key in
                                   ("id", "status", "startedAt", "completedAt", "durationMs", "error")} if selected else None
                    row["active"] = thread["status"].get("type") == "active" and selected is not None and selected["status"] == "inProgress"
                except Exception as error:
                    row["error"] = str(error)
                    row["active"] = False
            else:
                row["active"] = False
            result["workers"].append(row)
    result["finished"] = now()
    result["activeCount"] = sum(row["active"] for row in result["workers"])
    result["allEightActiveObserved"] = len(result["workers"]) == 8 and result["activeCount"] == 8
    return result


def launch(args, *, brief_factory=brief_for):
    if len(args.workers) != 8 or len(set(args.workers)) != 8:
        raise ValueError("Exactly eight distinct registered worker environment IDs are required")
    helper = args.project_root / "examples/launch_and_wait.py"
    if not helper.is_file():
        raise FileNotFoundError(helper)
    args.report.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    state = {"schemaVersion": 1, "runId": "acceptance-eight-" + uuid4().hex[:12],
             "started": now(), "holdSeconds": HOLD_SECONDS, "modelSettings": "deployment defaults",
             "status": "launching", "workers": []}
    save(args.report, state, new=True)
    for index, worker in enumerate(args.workers, 1):
        brief = brief_factory(worker, index, state["runId"], args.origin)
        brief_path = args.report.parent / f"{state['runId']}-brief-{index:02d}.json"
        save(brief_path, brief, new=True)
        entry = {"worker": worker, "index": index, "brief": str(brief_path), "records": [], "launched": now()}
        state["workers"].append(entry)
        save(args.report, state)
        error_path = args.report.parent / f"{state['runId']}-launch-{index:02d}.stderr"
        entry["stderr"] = str(error_path)
        with error_path.open("x") as stderr:
            process = subprocess.Popen([sys.executable, str(helper), "--no-wait", str(brief_path)],
                                       cwd=args.project_root, stdout=subprocess.PIPE, stderr=stderr, text=True)
            for line in process.stdout:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    entry.setdefault("nonJsonOutput", []).append(line.rstrip())
                else:
                    entry["records"].append(record)
                    for key in ("threadId", "turnId", "environmentId", "model", "artifactRoot"):
                        if key in record:
                            entry[key] = record[key]
                    print(json.dumps({"worker": worker, **record}), flush=True)
                save(args.report, state)
            entry["launchExitCode"] = process.wait()
        error_path.chmod(0o600)
        save(args.report, state)
        if entry["launchExitCode"] != 0 or not entry.get("turnId"):
            state["status"] = "launch_failed"
            save(args.report, state)
            print(json.dumps({"status": state["status"], "report": str(args.report), "worker": worker,
                              "stderr": str(error_path)}), flush=True)
            return 1
    state["status"] = "launched"
    state["launchFinished"] = now()
    save(args.report, state)
    state["postLaunchSnapshot"] = snapshot(state)
    save(args.report, state)
    print(json.dumps({"status": state["status"], "report": str(args.report),
                      "activeCount": state["postLaunchSnapshot"]["activeCount"]}), flush=True)
    return 0


def inspect(args):
    state = json.loads(args.report.read_text())
    result = {"runId": state["runId"], **snapshot(state)}
    save(args.out, result, new=True)
    print(json.dumps(result), flush=True)
    return 0


def collect(args, *, extra_checks=None):
    state = json.loads(args.report.read_text())
    result = {"runId": state["runId"], "started": now(), "workers": [], "passed": False}
    save(args.out, result, new=True)
    for entry in state["workers"]:
        row = {"worker": entry["worker"], "index": entry["index"], "threadId": entry.get("threadId"),
               "turnId": entry.get("turnId"), "passed": False}
        result["workers"].append(row)
        try:
            if not row["turnId"]:
                raise ValueError("Launch did not return an accepted turn ID")
            with connect() as client:
                client.initialize()
                require_chatgpt(client)
                finished = wait_for_completion(client, row["threadId"], turn_id=row["turnId"],
                                               artifacts=Path(entry["artifactRoot"]))
            row["native"] = finished
            turn = next(turn for turn in finished["turns"] if turn["id"] == row["turnId"])
            if turn["status"] != "completed" or turn.get("error") is not None:
                raise ValueError("The native turn did not complete successfully")
            root = Path(entry["artifactRoot"])
            proof = json.loads((root / "proof.json").read_text())
            row["proof"] = proof
            index = row["index"]
            marker = f"{state['runId']}-{index:02d}"
            checks = {
                "marker": proof.get("marker") == marker and (root / "marker.txt").read_text() == marker + "\n",
                "workerIndex": proof.get("workerIndex") == index,
                "caseResults": proof.get("caseResults") == [index, 8 + index, 11 + index],
                "elapsedEpoch": proof["endEpoch"] - proof["startEpoch"] >= HOLD_SECONDS - 1,
                "elapsedMonotonic": proof["endMonotonic"] - proof["startMonotonic"] >= HOLD_SECONDS - 1,
                "unittest": "Ran 3 tests" in (root / "unit-tests.txt").read_text() and "\nOK" in (root / "unit-tests.txt").read_text(),
                "sourceArtifacts": all((root / name).is_file() for name in ("solution.py", "test_solution.py")),
                "longCommand": any(item.get("type") == "commandExecution" and item.get("exitCode") == 0
                                   and (item.get("durationMs") or 0) >= (HOLD_SECONDS - 1) * 1000 for item in turn["items"]),
                "nativeTiming": all(isinstance(turn.get(key), (int, float)) for key in ("startedAt", "completedAt")),
            }
            if extra_checks is not None:
                checks.update(extra_checks(entry, root, turn))
            row["checks"] = checks
            row["nativeInterval"] = {key: turn.get(key) for key in ("startedAt", "completedAt", "durationMs")}
            row["tokenMetadata"] = {key: turn[key] for key in ("tokenUsage", "usage") if key in turn}
            row["passed"] = all(checks.values())
        except Exception as error:
            row["error"] = str(error)
            if hasattr(error, "result"):
                row["native"] = error.result
        save(args.out, result)
        print(json.dumps({key: row[key] for key in ("worker", "threadId", "turnId", "passed")}
                         | {"error": row.get("error")}), flush=True)
    if len(result["workers"]) == 8 and all(row["passed"] for row in result["workers"]):
        rows = result["workers"]
        native_start = max(row["nativeInterval"]["startedAt"] for row in rows)
        native_end = min(row["nativeInterval"]["completedAt"] for row in rows)
        command_start = max(row["proof"]["startEpoch"] for row in rows)
        command_end = min(row["proof"]["endEpoch"] for row in rows)
        result["overlap"] = {"nativeStartEpoch": native_start, "nativeEndEpoch": native_end,
                             "nativeSeconds": native_end - native_start,
                             "workerCommandStartEpoch": command_start, "workerCommandEndEpoch": command_end,
                             "workerCommandSeconds": command_end - command_start}
        result["passed"] = native_end > native_start and command_end > command_start
    result["finished"] = now()
    save(args.out, result)
    print(json.dumps({"passed": result["passed"], "report": str(args.out), "overlap": result.get("overlap"),
                      "tokenMetadataNote": "Only native fields present in the read result are recorded; no billing estimate."}), flush=True)
    return 0 if result["passed"] else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    start = commands.add_parser("launch", help="Start exactly eight real Codex tasks and return without waiting")
    start.add_argument("--workers", nargs="+", required=True)
    start.add_argument("--origin", required=True)
    start.add_argument("--project-root", type=Path, default=Path("/opt/project"))
    start.add_argument("--report", type=Path, required=True)
    start.add_argument("--run-models", action="store_true", required=True)
    for command in ("inspect", "collect"):
        sub = commands.add_parser(command, help="Read state" if command == "inspect" else "Wait and verify native outcomes and artifacts")
        sub.add_argument("--report", type=Path, required=True)
        sub.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    return {"launch": launch, "inspect": inspect, "collect": collect}[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
