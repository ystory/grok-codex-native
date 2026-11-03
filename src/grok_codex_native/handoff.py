import argparse
import json
import os
import re
import stat
import sys
from pathlib import Path
from uuid import UUID

from openai_codex.client import CodexClient
from openai_codex.errors import JsonRpcError
from openai_codex.models import UnknownNotification
from pydantic import RootModel

from .client import connect, require_chatgpt

TERMINAL = {"completed", "failed", "interrupted"}


class HandoffFailure(RuntimeError):
    def __init__(self, result: dict):
        super().__init__(result.get("error") or result.get("artifactError"))
        self.result = result


def read_transcript(client: CodexClient, thread_id: str) -> list[dict]:
    turns = []
    cursor = None
    seen = set()
    while True:
        page = client.request(
            "thread/turns/list",
            {"threadId": thread_id, "cursor": cursor, "sortDirection": "asc", "itemsView": "full"},
            response_model=RootModel[dict],
        ).root
        turns.extend(page["data"])
        cursor = page["nextCursor"]
        if cursor is None:
            break
        if cursor in seen:
            raise ValueError("Native turn pagination repeated a cursor")
        seen.add(cursor)
    for turn in turns:
        items = []
        cursor = None
        seen = set()
        while True:
            page = client.request(
                "thread/items/list",
                {"threadId": thread_id, "turnId": turn["id"], "cursor": cursor, "sortDirection": "asc"},
                response_model=RootModel[dict],
            ).root
            for entry in page["data"]:
                if entry["turnId"] != turn["id"]:
                    raise ValueError("Native item page contains a different turn")
                items.append(entry["item"])
            cursor = page["nextCursor"]
            if cursor is None:
                break
            if cursor in seen:
                raise ValueError("Native item pagination repeated a cursor")
            seen.add(cursor)
        turn["items"] = items
        turn["itemsView"] = "full"
    return turns


def artifact_paths(directory: Path, thread_id: str) -> list[str]:
    root = Path(os.environ.get("GROK_CODEX_ARTIFACTS_ROOT", "/var/lib/grok-codex/artifacts"))
    if directory.parent.parent != root or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", directory.parent.name):
        raise ValueError("Artifacts must use the configured root/worker/exact-thread-ID directory")
    if str(UUID(thread_id)) != thread_id or directory.name != thread_id:
        raise ValueError("Artifact directory must be named with the exact native thread ID")
    if not directory.is_absolute() or ".." in directory.parts:
        raise ValueError("Artifact directory must be an absolute path without traversal")
    for path in (*reversed(directory.parents), directory):
        if path.is_symlink():
            raise ValueError(f"Artifact paths must not contain symlinks: {path}")
    if not directory.is_dir():
        raise ValueError(f"Managed artifact directory does not exist: {directory}")
    files = []
    for path in sorted(directory.rglob("*")):
        mode = path.lstat().st_mode
        if stat.S_ISLNK(mode) or not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
            raise ValueError(f"Managed artifact path is not a regular file or directory: {path}")
        if stat.S_ISREG(mode):
            files.append(str(path))
    return files


def wait_for_completion(
    client: CodexClient,
    thread_id: str,
    *,
    turn_id: str | None = None,
    after: str | None = None,
    artifacts: Path | None = None,
) -> dict:
    if turn_id is not None and after is not None:
        raise ValueError("Choose --turn or --after")
    observed = {turn_id} if turn_id else set()
    route = client.register_goal_operation(thread_id)
    try:
        require_chatgpt(client)
        subscribed = False
        while True:
            turns = read_transcript(client, thread_id)
            observed.update(turn["id"] for turn in turns)
            start = 0
            if after is not None:
                anchors = [i for i, turn in enumerate(turns) if turn["id"] == after]
                if not anchors:
                    raise ValueError(f"Native --after turn does not exist in this thread: {after}")
                start = anchors[0] + 1
            selected = [
                turn for turn in turns[start:]
                if turn["status"] in TERMINAL and (turn_id is None or turn["id"] == turn_id)
            ]
            if selected:
                result = {
                    "threadId": thread_id,
                    "after": selected[-1]["id"],
                    "outcomes": [{"turnId": turn["id"], "status": turn["status"], "error": turn.get("error")} for turn in selected],
                    "artifacts": [],
                    "artifactError": None,
                    "turns": selected,
                }
                if artifacts:
                    try:
                        result["artifacts"] = artifact_paths(artifacts, thread_id)
                    except (OSError, ValueError) as exc:
                        result["artifactError"] = str(exc)
                        raise HandoffFailure(result) from exc
                return result
            if not subscribed:
                client.request(
                    "thread/resume", {"threadId": thread_id, "excludeTurns": True},
                    response_model=RootModel[dict],
                )
                subscribed = True
                continue
            if route.is_finished():
                client.unregister_goal_operation(route)
                route = client.register_goal_operation(thread_id)
                continue
            while True:
                notification = client.next_goal_notification(route)
                payload = notification.payload
                params = payload.params if isinstance(payload, UnknownNotification) else payload.model_dump(by_alias=True, mode="json")
                if isinstance(params.get("turnId"), str):
                    observed.add(params["turnId"])
                if isinstance(params.get("turn"), dict) and isinstance(params["turn"].get("id"), str):
                    observed.add(params["turn"]["id"])
                if notification.method == "error":
                    print(json.dumps({"method": notification.method, "params": params}), file=sys.stderr, flush=True)
                if notification.method == "turn/completed":
                    break
    except HandoffFailure:
        raise
    except Exception as exc:
        raise HandoffFailure({
            "threadId": thread_id, "turnIds": sorted(observed), "after": after,
            "error": {"code": exc.code, "message": exc.message, "data": exc.data} if isinstance(exc, JsonRpcError) else str(exc),
        }) from exc
    finally:
        client.unregister_goal_operation(route)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Wait once for native Codex completion and exit for Grok Shell")
    parser.add_argument("--thread", required=True)
    cursor = parser.add_mutually_exclusive_group()
    cursor.add_argument("--turn")
    cursor.add_argument("--after")
    parser.add_argument("--artifacts", type=Path)
    args = parser.parse_args(argv)
    try:
        with connect() as client:
            client.initialize()
            result = wait_for_completion(client, args.thread, turn_id=args.turn, after=args.after, artifacts=args.artifacts)
        print(json.dumps(result), flush=True)
        return 0
    except Exception as exc:
        result = exc.result if isinstance(exc, HandoffFailure) else {"threadId": args.thread, "turnId": args.turn, "after": args.after, "error": str(exc)}
        print(json.dumps(result), flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
