import argparse
import base64
import hashlib
import json
from pathlib import Path
import struct
import zlib

from pydantic import RootModel

from grok_codex_native.client import connect, require_chatgpt
from grok_codex_native.handoff import wait_for_completion


def image_fixture():
    shapes = [
        ["01110", "10001", "10001", "01110", "10001", "10001", "01110"],
        ["00010", "00110", "01010", "10010", "11111", "00010", "00010"],
        ["01110", "10001", "10000", "11110", "10001", "10001", "01110"],
        ["01110", "10001", "00001", "00010", "00100", "01000", "11111"],
    ]
    width, height, scale = 390, 145, 15
    pixels = bytearray(b"\xff" * (width * height * 3))
    for digit, shape in enumerate(shapes):
        for y, row in enumerate(shape):
            for x, value in enumerate(row):
                if value == "1":
                    for yy in range(20 + y * scale, 20 + (y + 1) * scale):
                        for xx in range(20 + digit * 90 + x * scale, 20 + digit * 90 + (x + 1) * scale):
                            index = (yy * width + xx) * 3
                            pixels[index:index + 3] = b"\x00\x00\x00"

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    raw = b"".join(b"\x00" + pixels[y * width * 3:(y + 1) * width * 3] for y in range(height))
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


def main():
    parser = argparse.ArgumentParser(description="Verify explicit model settings, remote repo skill, image and selected secret fixture")
    parser.add_argument("--worker", required=True)
    parser.add_argument("--run-models", action="store_true", required=True)
    args = parser.parse_args()
    with connect() as c:
        c.initialize()
        require_chatgpt(c)
        started = c.thread_start({"model": "gpt-6.1-sol", "config": {"model_reasoning_effort": "low"},
                                  "approvalPolicy": "never", "environments": [{"environmentId": args.worker,
                                  "cwd": "/work/source", "runtimeWorkspaceRoots": ["/work/source", "/work/artifacts"]}]})
        tid = started.thread.id
        c.thread_set_name(tid, "Disposable native input verification")
        folder = Path("/var/lib/grok-codex/artifacts") / args.worker / tid
        folder.mkdir(parents=True)
        digest = hashlib.sha256(b"DISPOSABLE_SECRET_FIXTURE_ONLY").hexdigest()
        text = f"""Verify this disposable fixture only. Use the included repository skill.
The delivery directory is /work/artifacts/{tid}/.
Read the four digits in the attached image, and write just the digits to image-code.txt there. Do not infer the image from local files.
Read /work/source/ref-marker.txt and copy it to ref-marker.txt in the delivery directory.
Compute SHA-256 of /run/secrets/DEPLOYMENT_SENTINEL and write its hexadecimal digest to secret-digest.txt there. Do not print the secret itself. Expected hash is {digest}.
Read your created outputs and report actual success or errors. Do no other work."""
        turn = c.turn_start(tid, [
            {"type": "skill", "name": "native-route-probe", "path": "/work/source/.agents/skills/native-route-probe/SKILL.md"},
            {"type": "image", "url": "data:image/png;base64," + base64.b64encode(image_fixture()).decode()},
            {"type": "text", "text": text},
        ], {"sandboxPolicy": {"type": "externalSandbox", "networkAccess": "enabled"}}).turn.id
    with connect() as c:
        c.initialize()
        result = wait_for_completion(c, tid, turn_id=turn, artifacts=folder)
    assert (folder / "image-code.txt").read_text().strip() == "8462", result
    assert (folder / "skill-route.txt").read_text().strip() == "SKILL_F05_OK", result
    assert (folder / "ref-marker.txt").read_text().strip() == "BASE_REF_OK", result
    assert (folder / "secret-digest.txt").read_text().strip() == digest, result
    effective = started.model_dump(mode="json", by_alias=True)
    assert effective["model"] == "gpt-6.1-sol" and effective["reasoningEffort"] == "low", effective
    print(json.dumps({"verified": ["requested model and effort", "remote repository skill", "image input",
                                   "non-default PR ref", "selected secret file"],
                      "threadId": tid, "turnId": turn, "model": effective["model"],
                      "reasoningEffort": effective["reasoningEffort"], "artifacts": result["artifacts"]}), flush=True)


if __name__ == "__main__":
    main()
