import argparse
import json
import os
import re
import shutil
import sys
from pathlib import Path
from uuid import UUID

from openai_codex.client import CodexClient
from openai_codex.generated.v2_all import ThreadSourceKind
from openai_codex.errors import JsonRpcError
from pydantic import RootModel

from .client import connect, require_chatgpt
from .handoff import TERMINAL, artifact_paths

SOURCE_KINDS = [source.value for source in ThreadSourceKind]


class PurgeFailure(RuntimeError):
    def __init__(self, result: dict):
        super().__init__(result["error"])
        self.result = result


def _pages(client: CodexClient, method: str, params: dict) -> list:
    result = []
    cursor = None
    seen = set()
    while True:
        page = client.request(method, {**params, "cursor": cursor}, response_model=RootModel[dict]).root
        result.extend(page["data"])
        cursor = page["nextCursor"]
        if cursor is None:
            return result
        if cursor in seen:
            raise ValueError(f"Native {method} pagination repeated a cursor")
        seen.add(cursor)


def _subtree(client: CodexClient, thread_id: str) -> dict[str, dict]:
    root = client.request("thread/read", {"threadId": thread_id}, response_model=RootModel[dict]).root["thread"]
    threads = {thread_id: root}
    descendants = []
    for archived in (False, True):
        descendants.extend(_pages(client, "thread/list", {
            "ancestorThreadId": thread_id, "archived": archived,
            "sourceKinds": SOURCE_KINDS, "modelProviders": [],
        }))
    for thread in descendants:
        if thread["id"] == thread_id:
            raise ValueError("Native descendant filter returned the ancestor; refusing unknown subtree")
        threads[thread["id"]] = thread
    for thread in descendants:
        current = thread
        seen = set()
        while current["id"] != thread_id:
            if current["id"] in seen or current.get("parentThreadId") not in threads:
                raise ValueError("Native descendant lineage is incomplete; refusing deletion")
            seen.add(current["id"])
            current = threads[current["parentThreadId"]]
    loaded = {}
    for loaded_id in _pages(client, "thread/loaded/list", {}):
        loaded[loaded_id] = client.request(
            "thread/read", {"threadId": loaded_id}, response_model=RootModel[dict]
        ).root["thread"]
    for thread in list(loaded.values()):
        lineage = []
        seen = set()
        current = thread
        while current["id"] != thread_id:
            if current["id"] in seen or "parentThreadId" not in current:
                raise ValueError("Native loaded thread lineage is unknown; refusing deletion")
            seen.add(current["id"])
            lineage.append(current)
            parent = current["parentThreadId"]
            if parent is None:
                break
            if parent not in loaded:
                loaded[parent] = client.request(
                    "thread/read", {"threadId": parent}, response_model=RootModel[dict]
                ).root["thread"]
            current = loaded[parent]
        else:
            threads.update((item["id"], item) for item in lineage)
    threads.update((native_id, thread) for native_id, thread in loaded.items() if native_id in threads)
    return threads


def _managed_paths(client: CodexClient, threads: dict[str, dict], root: Path) -> list[Path]:
    if not root.is_absolute() or ".." in root.parts or root == Path(root.anchor):
        raise ValueError("Managed artifact root must be an absolute, separate directory")
    for path in (*reversed(root.parents), root):
        if path.is_symlink():
            raise ValueError(f"Managed artifact root contains a symlink: {path}")
    paths = []
    for thread_id in threads:
        for attachment in _pages(client, "thread/attachment/list", {"threadId": thread_id}):
            if attachment["attachmentType"] != "grok.artifact":
                continue
            payload = attachment["payload"]
            if attachment["identityKey"] != thread_id or not isinstance(payload, dict) or payload.get("threadId") != thread_id:
                raise ValueError("Managed artifact attachment has a different thread owner")
            worker = payload.get("worker")
            if not isinstance(worker, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", worker):
                raise ValueError("Managed artifact worker is not a safe directory component")
            if str(UUID(thread_id)) != thread_id:
                raise ValueError("Managed artifact owner must be an exact native UUID")
            path = root / worker / thread_id
            if payload.get("path") != str(path):
                raise ValueError("Managed artifact path must be root/worker/exact-thread-ID")
            if path.exists() or path.is_symlink():
                artifact_paths(path, thread_id)
                paths.append(path)
    return sorted(set(paths))


def _require_stopped(client: CodexClient, thread_id: str) -> None:
    thread = client.request("thread/read", {"threadId": thread_id}, response_model=RootModel[dict]).root["thread"]
    if thread["status"].get("type") not in {"idle", "notLoaded"}:
        raise ValueError(f"Thread {thread_id} is active or its native status is unknown; stop it explicitly first")
    for turn in _pages(client, "thread/turns/list", {"threadId": thread_id, "itemsView": "notLoaded"}):
        if turn["status"] not in TERMINAL:
            raise ValueError(f"Thread {thread_id} has a nonterminal native turn")
    if _pages(client, "thread/queue/list", {"threadId": thread_id}):
        raise ValueError(f"Thread {thread_id} has queued native work; cancel it explicitly first")
    goal = client.request("thread/goal/get", {"threadId": thread_id}, response_model=RootModel[dict]).root["goal"]
    if goal is not None and goal.get("status") not in {"paused", "complete"}:
        raise ValueError(f"Thread {thread_id} has a runnable or unknown goal; pause it explicitly first")


def purge(
    client: CodexClient,
    thread_id: str,
    artifacts_root: Path,
    confirm_thread: str,
    *,
    dry_run: bool = False,
) -> dict:
    if confirm_thread != thread_id or str(UUID(thread_id)) != thread_id:
        raise ValueError("Deletion requires confirmation of the exact native thread ID")
    configured_root = Path(os.environ.get("GROK_CODEX_ARTIFACTS_ROOT", "/var/lib/grok-codex/artifacts"))
    if artifacts_root != configured_root:
        raise ValueError("--artifacts-root must match the trusted GROK_CODEX_ARTIFACTS_ROOT configuration")
    require_chatgpt(client)
    threads = _subtree(client, thread_id)
    paths = _managed_paths(client, threads, artifacts_root)
    for native_id in threads:
        _require_stopped(client, native_id)
    final_threads = _subtree(client, thread_id)
    if set(final_threads) != set(threads):
        raise ValueError("Native descendants changed during preflight; refusing deletion")
    if any(thread["status"].get("type") not in {"idle", "notLoaded"} for thread in final_threads.values()):
        raise ValueError("Native activity changed during preflight; refusing deletion")
    result = {
        "threadId": thread_id, "preflightThreadIds": sorted(threads),
        "managedArtifacts": [str(path) for path in paths], "removedArtifacts": [],
        "dryRun": dry_run,
    }
    if dry_run:
        return result
    try:
        client.request("thread/delete", {"threadId": thread_id}, response_model=RootModel[dict])
    except Exception as exc:
        error = {"code": exc.code, "message": exc.message, "data": exc.data} if isinstance(exc, JsonRpcError) else str(exc)
        raise PurgeFailure({**result, "nativeDeleteOutcome": "unknown", "error": error}) from exc
    result["nativeDeleteOutcome"] = "succeeded"
    try:
        for path in paths:
            artifact_paths(path, path.name)
            shutil.rmtree(path)
            result["removedArtifacts"].append(str(path))
    except Exception as exc:
        raise PurgeFailure({
            **result, "error": str(exc),
            "remainingArtifacts": [str(path) for path in paths if str(path) not in result["removedArtifacts"]],
        }) from exc
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Refuse runnable native threads, then delete confirmed history and managed artifacts")
    parser.add_argument("--thread", required=True)
    parser.add_argument("--artifacts-root", required=True, type=Path)
    parser.add_argument("--confirm-thread", required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        with connect() as client:
            client.initialize()
            result = purge(client, args.thread, args.artifacts_root, args.confirm_thread, dry_run=args.dry_run)
        print(json.dumps(result), flush=True)
        return 0
    except Exception as exc:
        error = {"code": exc.code, "message": exc.message, "data": exc.data} if isinstance(exc, JsonRpcError) else str(exc)
        result = exc.result if isinstance(exc, PurgeFailure) else {"threadId": args.thread, "error": error}
        print(json.dumps(result), flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
