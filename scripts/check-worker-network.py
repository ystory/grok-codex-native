#!/usr/bin/env python3
"""Check public HTTPS and managed-worker network boundaries without model calls."""
import argparse
import base64
import http.client
import json
from pathlib import Path
import subprocess
import urllib.request

from _docker import deployment, inspect_worker


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("worker_a")
    parser.add_argument("worker_b")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    if args.worker_a == args.worker_b:
        parser.error("Select two different managed workers.")
    config = deployment()
    workers = []
    for name in (args.worker_a, args.worker_b):
        info = inspect_worker(config, name)
        if not info["State"]["Running"]:
            parser.error(f"Worker is not running: {name}")
        networks = info["NetworkSettings"]["Networks"]
        expected = config.worker_name(name) + "-net"
        if set(networks) != {expected}:
            parser.error(f"Worker must use exactly its dedicated network: {name}")
        endpoint = info["NetworkSettings"]["Ports"]["5000/tcp"]
        if len(endpoint) != 1 or endpoint[0]["HostIp"] != "127.0.0.1":
            parser.error(f"Executor must be published only on host loopback: {name}")
        workers.append((config.worker_name(name), networks[expected], int(endpoint[0]["HostPort"])))
    if workers[0][1]["NetworkID"] == workers[1][1]["NetworkID"]:
        parser.error("The two workers must use different networks.")

    with urllib.request.urlopen("http://127.0.0.1:2375/version", timeout=4) as response:
        platform_version = json.load(response)["Version"]
    results = [{"check": "host_docker_api_present", "passed": True, "version": platform_version}]
    for identity, _, port in workers:
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=4)
        try:
            connection.request("GET", "/", headers={
                "Upgrade": "websocket", "Connection": "Upgrade", "Sec-WebSocket-Version": "13",
                "Sec-WebSocket-Key": base64.b64encode(b"native-probe-key").decode(),
            })
            status = connection.getresponse().status
            results.append({"worker": identity, "check": "executor_present_and_authenticated",
                            "passed": status in (401, 403), "httpStatus": status})
        finally:
            connection.close()
    for index, (identity, network, _) in enumerate(workers):
        targets = (
            ("public_https", "https://pypi.org/simple/idna/", True),
            ("host_docker_api_blocked", f"http://{network['Gateway']}:2375/version", False),
            ("peer_executor_blocked", f"http://{workers[1 - index][1]['IPAddress']}:5000/", False),
        )
        for check, url, reachable in targets:
            result = subprocess.run(
                ["docker", "exec", identity, "curl", "--noproxy", "*", "--silent", "--show-error",
                 "--connect-timeout", "3", "--max-time", "6", "--output", "/dev/null",
                 "--write-out", "%{http_code}", url],
                text=True, capture_output=True, timeout=12,
            )
            passed = (result.returncode == 0 and result.stdout == "200") if reachable else result.returncode in (7, 28)
            results.append({"worker": identity, "check": check, "passed": passed,
                            "exitCode": result.returncode, "httpStatus": result.stdout,
                            "error": result.stderr.strip()})
    report = {"passed": all(item["passed"] for item in results), "checks": results}
    output = json.dumps(report, indent=2) + "\n"
    if args.report:
        with args.report.open("x") as stream:
            stream.write(output)
        args.report.chmod(0o600)
    print(output, end="")
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
