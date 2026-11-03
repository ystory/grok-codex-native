"""Run seven light acceptance tasks and one local HTTP/Chromium task on eight existing workers."""
import argparse
import errno
import importlib.util
import json
from pathlib import Path
import shlex


def web_steps(index, run_id):
    body = f"{run_id}-{index:02d}-http"
    return f"""This worker additionally exercises a temporary local server and browser.
Create http_server.py in the task source directory. Use Python ThreadingHTTPServer bound to 127.0.0.1 with port 0, and serve exactly {body!r} as text/plain for GET. Write its actual PID and assigned port to server-start.json in the delivery directory before serving.
Start only this server detached with setsid -f python3, stdin closed and stdout/stderr redirected to files. Retain its actual PID from server-start.json so you can stop only your own server.
Before the 90-second hold, request the real loopback URL, verify HTTP 200 and the exact body, and save that body as server-response-before.txt in the delivery directory.
Then run chromium as its own native shell command, with --headless --no-sandbox --disable-gpu --disable-dev-shm-usage --window-size=800,600 --screenshot=DELIVERY/browser.png and the actual loopback URL. Wait for it to finish and retain its actual command and exit code. It must exit 0; otherwise stop your server and report failure.
Keep the HTTP server running throughout the hold. In the hold Python process described below, sleep 45 seconds, perform another actual HTTP GET and verify the same 200/body, save server-response-during.txt and httpDuringEpoch, then sleep another 45 seconds. These two sleeps are the required 90-second hold, not a shorter replacement.
After recording endEpoch/endMonotonic, terminate only the PID of the server you created. Verify a new connection to its former port is refused. Record the underlying connection error errno, not just an outer urllib error name. Do not leave the server running.
Write web-proof.json in the delivery directory containing serverHost='127.0.0.1', serverPort, beforeStatus=200, duringStatus=200, browserCommand (the real argument list), browserExitCode, browserFinishedEpoch, httpDuringEpoch, serverStoppedEpoch, afterStopConnectionRefused (computed from the real failed connection), and afterStopErrno. Copy http_server.py to the delivery directory too.
Write the normal proof.json and marker.txt required below only after the real hold and your server cleanup are finished."""


def mixed_factory(base, heavy_worker):
    def make(worker, index, run_id, origin):
        if worker != heavy_worker:
            return base.brief_for(worker, index, run_id, origin)
        brief = base.brief_for(worker, index, run_id, origin, local_web_steps=web_steps(index, run_id))
        brief["acceptanceWorkload"] = {"kind": "local-http-chromium", "responseBody": f"{run_id}-{index:02d}-http"}
        return brief
    return make


def is_browser_command(command):
    try:
        arguments = shlex.split(command)
        if len(arguments) == 3 and arguments[:2] == ["/bin/bash", "-lc"]:
            arguments = shlex.split(arguments[2])
    except ValueError:
        return False
    return bool(arguments) and Path(arguments[0]).name in ("chromium", "chromium-browser") \
        and "--headless" in arguments[1:] \
        and any(argument.startswith("--screenshot=") for argument in arguments[1:])


def mixed_checks(entry, root, turn):
    brief = json.loads(Path(entry["brief"]).read_text())
    workload = brief.get("acceptanceWorkload")
    if workload is None:
        return {}
    web = json.loads((root / "web-proof.json").read_text())
    proof = json.loads((root / "proof.json").read_text())
    browser_command = web.get("browserCommand", [])
    browser_items = [item for item in turn["items"] if item.get("type") == "commandExecution"
                     and item.get("exitCode") == 0 and is_browser_command(item.get("command", ""))]
    return {
        "webLoopbackServer": web.get("serverHost") == "127.0.0.1" and isinstance(web.get("serverPort"), int) and 0 < web["serverPort"] < 65536,
        "webHttpBefore": web.get("beforeStatus") == 200 and (root / "server-response-before.txt").read_text() == workload["responseBody"],
        "webHttpDuring": web.get("duringStatus") == 200 and (root / "server-response-during.txt").read_text() == workload["responseBody"],
        "webBrowserExit": web.get("browserExitCode") == 0 and bool(browser_items),
        "webBrowserArguments": isinstance(browser_command, list) and "--headless" in browser_command
                               and any(str(part).startswith("--screenshot=") for part in browser_command),
        "webPng": (root / "browser.png").read_bytes().startswith(b"\x89PNG\r\n\x1a\n"),
        "webBeforeHold": web["browserFinishedEpoch"] <= proof["startEpoch"],
        "webDuringHold": proof["startEpoch"] + 44 <= web["httpDuringEpoch"] <= proof["endEpoch"] - 44,
        "webStoppedAfterHold": web["serverStoppedEpoch"] >= proof["endEpoch"],
        "webServerStopped": web.get("afterStopConnectionRefused") is True and web.get("afterStopErrno") == errno.ECONNREFUSED,
        "webServerSource": (root / "http_server.py").is_file(),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-path", type=Path, required=True, help="Updated acceptance_eight.py copied into the control scratch directory")
    commands = parser.add_subparsers(dest="command", required=True)
    start = commands.add_parser("launch")
    start.add_argument("--workers", nargs="+", required=True)
    start.add_argument("--heavy-worker", required=True)
    start.add_argument("--origin", required=True)
    start.add_argument("--project-root", type=Path, default=Path("/opt/project"))
    start.add_argument("--report", type=Path, required=True)
    start.add_argument("--run-models", action="store_true", required=True)
    for command in ("inspect", "collect"):
        sub = commands.add_parser(command)
        sub.add_argument("--report", type=Path, required=True)
        sub.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    specification = importlib.util.spec_from_file_location("acceptance_eight_validation", args.base_path)
    base = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(base)
    if args.command == "launch":
        if args.heavy_worker not in args.workers:
            parser.error("heavy-worker must name one of the eight selected environments")
        return base.launch(args, brief_factory=mixed_factory(base, args.heavy_worker))
    state = json.loads(args.report.read_text())
    web_workers = [entry for entry in state["workers"]
                   if json.loads(Path(entry["brief"]).read_text()).get("acceptanceWorkload", {}).get("kind") == "local-http-chromium"]
    if len(web_workers) != 1:
        parser.error("The saved launch must contain exactly one local HTTP/Chromium worker")
    return base.inspect(args) if args.command == "inspect" else base.collect(args, extra_checks=mixed_checks)


if __name__ == "__main__":
    raise SystemExit(main())
