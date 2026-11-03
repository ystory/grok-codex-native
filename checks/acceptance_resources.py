"""Record host and named-container resource observations without changing runtime state."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import time


LOW_AVAILABLE_BYTES = 512 * 1024 ** 2
INSPECT_FORMAT = '{"name":{{json .Name}},"status":{{json .State.Status}},"oomKilled":{{json .State.OOMKilled}},"restartCount":{{json .RestartCount}}}'


def stamp():
    return {"utc": datetime.now(timezone.utc).isoformat(), "epoch": time.time()}


def docker_records(arguments):
    command = ["docker", *arguments]
    try:
        result = subprocess.run(command, text=True, capture_output=True, timeout=15)
    except subprocess.TimeoutExpired:
        return {"records": [], "error": "Docker observation exceeded 15 seconds"}
    if result.returncode:
        return {"records": [], "exitCode": result.returncode, "error": result.stderr.strip()}
    try:
        return {"records": [json.loads(line) for line in result.stdout.splitlines() if line.strip()], "exitCode": 0}
    except json.JSONDecodeError as error:
        return {"records": [], "exitCode": 0, "error": str(error), "output": result.stdout}


def inspection(containers):
    return {**stamp(), **docker_records(["inspect", "--format", INSPECT_FORMAT, *containers])}


def sample(containers, index):
    started = time.monotonic()
    observed = stamp()
    memory = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        key, value = line.split(":", 1)
        if key in ("MemTotal", "MemAvailable", "SwapTotal", "SwapFree"):
            memory[key + "Bytes"] = int(value.split()[0]) * 1024
    disk = shutil.disk_usage("/workspace")
    stats = docker_records(["stats", "--no-stream", "--format", "{{json .}}", *containers])
    return {
        "kind": "sample", "index": index, **observed,
        "memory": memory,
        "loadAverage": list(os.getloadavg()),
        "workspaceDisk": {"totalBytes": disk.total, "usedBytes": disk.used, "freeBytes": disk.free},
        "dockerStats": stats,
        "finished": stamp(), "samplingSeconds": time.monotonic() - started,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--containers", nargs="+", required=True, help="Exact worker and control container names to observe")
    parser.add_argument("--seconds", type=float, default=240)
    parser.add_argument("--interval", type=float, default=3)
    parser.add_argument("--out", type=Path, required=True, help="New JSONL evidence file; summary uses the same stem")
    args = parser.parse_args()
    if args.seconds <= 0 or args.interval <= 0:
        parser.error("seconds and interval must be positive")
    if len(set(args.containers)) != len(args.containers):
        parser.error("container names must be distinct")
    args.out.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    summary_path = args.out.with_suffix(".summary.json")
    if summary_path.exists():
        parser.error(f"Summary already exists: {summary_path}")
    minimum_available = None
    minimum_disk = None
    samples = 0
    docker_errors = 0
    threshold_crossed = False
    interrupted = False
    started = stamp()
    before = inspection(args.containers)
    monotonic_start = time.monotonic()
    deadline = monotonic_start + args.seconds
    with args.out.open("x") as stream:
        args.out.chmod(0o600)

        def record(value):
            stream.write(json.dumps(value, separators=(",", ":")) + "\n")
            stream.flush()

        record({"kind": "baseline", **started, "containers": args.containers,
                "requestedSeconds": args.seconds, "intervalSeconds": args.interval, "inspection": before})
        try:
            while True:
                sample_started = time.monotonic()
                row = sample(args.containers, samples)
                record(row)
                samples += 1
                available = row["memory"]["MemAvailableBytes"]
                minimum_available = available if minimum_available is None else min(minimum_available, available)
                disk_free = row["workspaceDisk"]["freeBytes"]
                minimum_disk = disk_free if minimum_disk is None else min(minimum_disk, disk_free)
                threshold_crossed |= available < LOW_AVAILABLE_BYTES
                docker_errors += "error" in row["dockerStats"]
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                time.sleep(max(0, min(args.interval - (time.monotonic() - sample_started), remaining)))
        except KeyboardInterrupt:
            interrupted = True
        after = inspection(args.containers)
        summary = {
            "kind": "summary", "started": started, "finished": stamp(),
            "observedSeconds": time.monotonic() - monotonic_start,
            "containers": args.containers, "samples": samples,
            "minimumAvailableMemoryBytes": minimum_available, "minimumWorkspaceFreeBytes": minimum_disk,
            "lowAvailableThresholdBytes": LOW_AVAILABLE_BYTES, "thresholdCrossed": threshold_crossed,
            "dockerObservationErrors": docker_errors, "interrupted": interrupted,
            "before": before, "after": after,
            "automaticActions": [], "evidence": str(args.out),
        }
        record(summary)
    with summary_path.open("x") as stream:
        json.dump(summary, stream, indent=2)
        stream.write("\n")
    summary_path.chmod(0o600)
    print(json.dumps({"evidence": str(args.out), "summary": str(summary_path), "samples": samples,
                      "minimumAvailableMemoryBytes": minimum_available, "thresholdCrossed": threshold_crossed,
                      "dockerObservationErrors": docker_errors, "interrupted": interrupted}), flush=True)
    return 130 if interrupted else 1 if docker_errors or "error" in before or "error" in after else 0


if __name__ == "__main__":
    raise SystemExit(main())
