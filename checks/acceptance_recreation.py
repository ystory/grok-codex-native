"""Start a disposable clock probe and capture its state without resuming it."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import stat
import subprocess
import sys
import time
from uuid import uuid4

from pydantic import RootModel

from grok_codex_native.client import connect
from grok_codex_native.handoff import read_transcript


def stamp():
    return {"utc": datetime.now(timezone.utc).isoformat(), "epoch": time.time()}


def save(path, value, *, new=False):
    with path.open("x" if new else "w") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    path.chmod(0o600)


def probe_program(run_id, duration, interval):
    return f'''import json, os, socket, sys, time
from datetime import datetime, timezone
from pathlib import Path
folder = Path(sys.argv[1])
marker = {run_id!r}
remaining, interval, tick = {duration!r}, {interval!r}, 0
def row():
    memory = dict(line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines())
    return {{"tick": tick, "marker": marker, "utc": datetime.now(timezone.utc).isoformat(),
            "epoch": time.time(), "monotonic": time.monotonic(),
            "boottime": time.clock_gettime(time.CLOCK_BOOTTIME) if hasattr(time, "CLOCK_BOOTTIME") else None,
            "bootId": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
            "hostname": socket.gethostname(), "pid": os.getpid(), "cwd": os.getcwd(),
            "memAvailableKiB": int(memory["MemAvailable"].split()[0]), "loadAverage": list(os.getloadavg())}}
with (folder / "timeline.jsonl").open("x", buffering=1) as output:
    started = row()
    (folder / "started.json").write_text(json.dumps(started) + "\\n")
    output.write(json.dumps(started) + "\\n"); output.flush()
    while remaining > 0:
        delay = min(interval, remaining)
        time.sleep(delay)
        remaining -= delay
        tick += 1
        output.write(json.dumps(row()) + "\\n"); output.flush()
(folder / "proof.json").write_text(json.dumps({{"marker": marker, "lastTick": tick, "sleepRequestedSeconds": {duration!r}}}) + "\\n")
(folder / "marker.txt").write_text(marker + "\\n")
'''


def launch(args):
    args.report.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    state = {"schemaVersion": 1, "runId": "acceptance-recreation-" + uuid4().hex[:12],
             "started": stamp(), "worker": args.worker, "duration": args.duration,
             "interval": args.interval, "modelSettings": "deployment defaults", "status": "launching"}
    save(args.report, state, new=True)
    program = probe_program(state["runId"], args.duration, args.interval)
    brief = {"worker": args.worker, "origin": args.origin, "title": "Disposable recreation/sleep clock probe",
             "thread": {"environments": [{"environmentId": args.worker, "cwd": "/work/source",
                                           "runtimeWorkspaceRoots": ["/work/source", "/work/artifacts"]}]},
             "input": [{"type": "text", "text": f"""This is one disposable computer recreation/sleep observation task.
Use python3 only. Do not spawn subagents, use a network, install packages, change authentication, or touch other work.
Create /work/source/{state['runId']}/probe.py with the exact program below and execute it in the foreground.
Pass the delivery directory from the next instruction as its first argument. Wait for actual completion before ending the turn.
The fixed sleep requests must all execute; do not replace them with a wall-clock deadline, shorten them, fabricate records, or restart the probe.
Do not leave it detached. Copy probe.py to the delivery directory. If interrupted, preserve existing files and report the actual error.
{program}"""}]}
    brief_path = args.report.with_name(args.report.stem + ".brief.json")
    save(brief_path, brief, new=True)
    state["brief"] = str(brief_path)
    stderr_path = args.report.with_name(args.report.stem + ".stderr")
    state["stderr"] = str(stderr_path)
    with stderr_path.open("x") as stderr:
        process = subprocess.Popen([sys.executable, "/opt/project/examples/launch_and_wait.py", "--no-wait", str(brief_path)],
                                   cwd="/opt/project", stdout=subprocess.PIPE, stderr=stderr, text=True)
        for line in process.stdout:
            record = json.loads(line)
            state.setdefault("records", []).append(record)
            for key in ("threadId", "turnId", "environmentId", "model", "artifactRoot"):
                if key in record:
                    state[key] = record[key]
            save(args.report, state)
            print(json.dumps(record), flush=True)
        state["launchExitCode"] = process.wait()
    stderr_path.chmod(0o600)
    state["status"] = "launched" if state["launchExitCode"] == 0 and state.get("turnId") else "launch_failed"
    state["returned"] = stamp()
    save(args.report, state)
    print(json.dumps({"status": state["status"], "report": str(args.report)}), flush=True)
    return 0 if state["status"] == "launched" else 1


def queue_rows(client, thread):
    rows, cursor, seen = [], None, set()
    while True:
        page = client.request("thread/queue/list", {"threadId": thread, "cursor": cursor}, response_model=RootModel[dict]).root
        rows.extend(page["data"])
        cursor = page.get("nextCursor")
        if cursor is None:
            return rows
        if cursor in seen:
            raise ValueError("Native queue pagination repeated a cursor")
        seen.add(cursor)


def artifacts(directory):
    result = {}
    for name in ("started.json", "timeline.jsonl", "marker.txt", "proof.json"):
        path = directory / name
        try:
            metadata = path.lstat()
            entry = {"exists": True, "size": metadata.st_size, "mode": oct(stat.S_IMODE(metadata.st_mode)),
                     "mtimeEpoch": metadata.st_mtime, "regularFile": stat.S_ISREG(metadata.st_mode)}
            result[name] = entry
            if not entry["regularFile"]:
                continue
            data = path.read_bytes()
            entry["sha256"] = hashlib.sha256(data).hexdigest()
            if name == "timeline.jsonl":
                rows, errors = [], []
                for index, line in enumerate(data.decode().splitlines(), 1):
                    try:
                        rows.append(json.loads(line))
                    except (UnicodeError, json.JSONDecodeError) as error:
                        errors.append({"line": index, "error": str(error)})
                entry.update({"rows": rows, "parseErrors": errors, "rowCount": len(rows),
                              "first": rows[0] if rows else None, "last": rows[-1] if rows else None,
                              "gaps": [{"fromTick": a.get("tick"), "toTick": b.get("tick"),
                                        **{key: b[key] - a[key] for key in ("epoch", "monotonic", "boottime")
                                           if isinstance(a.get(key), (int, float)) and isinstance(b.get(key), (int, float))}}
                                       for a, b in zip(rows, rows[1:])]})
            else:
                entry["content"] = data.decode() if name == "marker.txt" else json.loads(data)
        except FileNotFoundError:
            result[name] = {"exists": False}
        except Exception as error:
            result.setdefault(name, {})["readError"] = {"type": type(error).__name__, "message": str(error)}
    return result


def capture(args):
    state = json.loads(args.report.read_text())
    result = {"runId": state["runId"], "threadId": state.get("threadId"), "turnId": state.get("turnId"),
              "started": stamp(), "readOnly": True, "errors": {}}
    args.out.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    save(args.out, result, new=True)
    try:
        with connect() as client:
            client.initialize()
            operations = {"thread": lambda: client.thread_read(state["threadId"]).model_dump(by_alias=True, mode="json"),
                          "turns": lambda: read_transcript(client, state["threadId"]),
                          "queue": lambda: queue_rows(client, state["threadId"]),
                          "goal": lambda: client.request("thread/goal/get", {"threadId": state["threadId"]}, response_model=RootModel[dict]).root}
            for name, operation in operations.items():
                try:
                    result[name] = operation()
                except Exception as error:
                    result["errors"][name] = {"type": type(error).__name__, "message": str(error)}
                save(args.out, result)
    except Exception as error:
        result["errors"]["connection"] = {"type": type(error).__name__, "message": str(error)}
    if state.get("artifactRoot"):
        result["artifacts"] = artifacts(Path(state["artifactRoot"]))
    result["finished"] = stamp()
    save(args.out, result)
    timeline = result.get("artifacts", {}).get("timeline.jsonl", {})
    print(json.dumps({"out": str(args.out), "threadId": result["threadId"], "turnId": result["turnId"],
                      "lastTick": (timeline.get("last") or {}).get("tick"), "errors": result["errors"]}), flush=True)
    return 0 if not result["errors"] else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    start = sub.add_parser("launch")
    start.add_argument("--worker", required=True)
    start.add_argument("--origin", required=True)
    start.add_argument("--report", type=Path, required=True)
    start.add_argument("--duration", type=float, default=600)
    start.add_argument("--interval", type=float, default=2)
    start.add_argument("--run-models", action="store_true", required=True)
    read = sub.add_parser("capture")
    read.add_argument("--report", type=Path, required=True)
    read.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "launch" and (args.duration <= 0 or args.interval <= 0):
        parser.error("duration and interval must be positive")
    return launch(args) if args.command == "launch" else capture(args)


if __name__ == "__main__":
    raise SystemExit(main())
