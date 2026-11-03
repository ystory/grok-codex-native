import copy
import json
import pytest
from openai_codex.client import CodexClient
from openai_codex.errors import TransportClosedError
from openai_codex.models import Notification, UnknownNotification

from grok_codex_native.client import connect, refuse_request, require_chatgpt
from grok_codex_native.handoff import HandoffFailure, artifact_paths, read_transcript, wait_for_completion
from grok_codex_native.purge import PurgeFailure, purge

THREAD = "019a0000-0000-7000-8000-000000000001"
CHILD = "019a0000-0000-7000-8000-000000000002"
TURN = "019a0000-0000-7000-8000-000000000011"
NEXT = "019a0000-0000-7000-8000-000000000012"


@pytest.fixture(autouse=True)
def trusted_artifact_root(tmp_path, monkeypatch):
    monkeypatch.setenv("GROK_CODEX_ARTIFACTS_ROOT", str(tmp_path))


def thread(native_id=THREAD, parent=None, status="idle"):
    return {"id": native_id, "parentThreadId": parent, "cwd": "/work/repo", "status": {"type": status}, "turns": []}


def turn(native_id=TURN, status="completed", text="complete report"):
    return {
        "id": native_id, "status": status, "error": None, "itemsView": "full",
        "items": [{"type": "agentMessage", "id": "report-" + native_id, "phase": "final_answer", "text": text}],
    }


class NativeFixture(CodexClient):
    def __init__(self):
        super().__init__(approval_handler=refuse_request)
        self.threads = {THREAD: thread()}
        self.turns = {THREAD: [turn()]}
        self.queues = {}
        self.goals = {}
        self.attachments = {}
        self.archived = set()
        self.calls = []
        self.deleted = []
        self.on_request = None
        self.on_wait = None
        self.delete_error = None

    def request(self, method, params, *, response_model):
        self.calls.append((method, copy.deepcopy(params)))
        if self.on_request:
            response = self.on_request(method, params)
            if response is not None:
                return response_model.model_validate(response)
        native_id = params.get("threadId")
        if method == "account/read":
            result = {"account": {"type": "chatgpt", "email": "fixture@example.com"}, "requiresOpenaiAuth": True}
        elif method == "thread/read":
            result = {"thread": self.threads[native_id]}
        elif method == "thread/resume":
            result = {"thread": self.threads[native_id]}
        elif method == "thread/list":
            result = {"data": [value for key, value in self.threads.items() if key != params["ancestorThreadId"] and (key in self.archived) == params["archived"]], "nextCursor": None}
        elif method == "thread/loaded/list":
            result = {"data": list(self.threads), "nextCursor": None}
        elif method == "thread/turns/list":
            result = {"data": copy.deepcopy(self.turns.get(native_id, [])), "nextCursor": None}
        elif method == "thread/items/list":
            selected = next(value for value in self.turns[native_id] if value["id"] == params["turnId"])
            result = {"data": [{"turnId": selected["id"], "item": item} for item in selected["items"]], "nextCursor": None}
        elif method == "thread/queue/list":
            result = {"data": self.queues.get(native_id, []), "nextCursor": None}
        elif method == "thread/goal/get":
            result = {"goal": self.goals.get(native_id)}
        elif method == "thread/attachment/list":
            result = {"data": self.attachments.get(native_id, []), "nextCursor": None}
        elif method == "thread/delete":
            if self.delete_error:
                raise self.delete_error
            self.deleted.extend(self.threads)
            self.attachments.clear()
            result = {}
        else:
            raise AssertionError(method)
        return response_model.model_validate(result)

    def next_goal_notification(self, state):
        if self.on_wait:
            self.on_wait()
        return super().next_goal_notification(state)

    def emit(self, method, params):
        self._router.route_notification(Notification(method, UnknownNotification(params)))


def managed(client, root, native_id=THREAD, worker="worker-a"):
    path = root / worker / native_id
    path.mkdir(parents=True)
    (path / "report.txt").write_text("managed output")
    client.attachments[native_id] = [{
        "id": "attachment", "attachmentType": "grok.artifact", "identityKey": native_id,
        "payload": {"threadId": native_id, "worker": worker, "path": str(path)},
    }]
    return path


def test_constructor_attaches_existing_socket_with_explicit_refusals(monkeypatch, capsys):
    monkeypatch.setenv("GROK_CODEX_SOCKET", "/run/custom/control.sock")
    client = connect()
    assert client.config.launch_args_override == (
        "websocat4", "-t", "-", "ws-c:unix-connect:/run/custom/control.sock"
    )
    for method in ("item/commandExecution/requestApproval", "item/fileChange/requestApproval"):
        response = client._handle_server_request({"id": 9, "method": method, "params": {"threadId": THREAD, "command": "rm fixture"}})
        assert response == {"decision": "decline"}
    records = [json.loads(line) for line in capsys.readouterr().err.splitlines()]
    assert records[0]["serverRequest"]["params"]["command"] == "rm fixture"
    assert records[0]["serverRequest"]["params"]["threadId"] == THREAD


@pytest.mark.parametrize(("method", "expected"), [
    ("item/permissions/requestApproval", {"permissions": {}, "scope": "turn"}),
    ("item/tool/requestUserInput", {"answers": {}}),
    ("mcpServer/elicitation/request", {"action": "decline", "content": None, "_meta": None}),
    ("item/tool/call", {"success": False, "contentItems": []}),
    ("account/chatgptAuthTokens/refresh", {}),
    ("unrecognized/request", {}),
])
def test_unattended_server_requests_never_grant_or_invent_input(method, expected, capsys):
    assert refuse_request(method, {"threadId": THREAD}) == expected
    assert json.loads(capsys.readouterr().err)["serverRequest"]["method"] == method


def test_subscription_check_rejects_api_key_before_resume_or_delete():
    client = NativeFixture()
    client.on_request = lambda method, params: {"account": {"type": "apiKey"}} if method == "account/read" else None
    with pytest.raises(PermissionError):
        require_chatgpt(client)
    with pytest.raises(HandoffFailure):
        wait_for_completion(client, THREAD, turn_id=TURN)
    assert not any(method in {"thread/resume", "thread/delete"} for method, _ in client.calls)


def test_transcript_pages_turns_and_items_without_summary_loss():
    client = NativeFixture()
    client.turns[THREAD] = [turn(text="full " * 10000), turn(NEXT, text="second final")]

    def pages(method, params):
        if method == "thread/turns/list":
            assert params["itemsView"] == "full"
            index = 0 if params["cursor"] is None else 1
            value = copy.deepcopy(client.turns[THREAD][index])
            value["items"] = []
            return {"data": [value], "nextCursor": "turn-page-2" if index == 0 else None}
        if method == "thread/items/list" and params["turnId"] == TURN:
            item = client.turns[THREAD][0]["items"][0]
            if params["cursor"] is None:
                return {"data": [{"turnId": TURN, "item": {"type": "userMessage", "id": "question", "content": [{"type": "text", "text": "original question"}]}}], "nextCursor": "item-page-2"}
            return {"data": [{"turnId": TURN, "item": item}], "nextCursor": None}
        return None

    client.on_request = pages
    result = read_transcript(client, THREAD)
    assert [value["id"] for value in result] == [TURN, NEXT]
    assert result[0]["items"][0]["content"][0]["text"] == "original question"
    assert result[0]["items"][1]["text"] == "full " * 10000
    assert result[1]["items"][0]["text"] == "second final"


def test_completed_snapshot_returns_full_native_result_without_waiting():
    client = NativeFixture()
    result = wait_for_completion(client, THREAD, turn_id=TURN)
    assert result["threadId"] == THREAD
    assert result["turns"][0]["status"] == "completed"
    assert result["turns"][0]["items"][0]["text"] == "complete report"
    assert result["after"] == TURN
    assert not any(method == "thread/resume" for method, _ in client.calls)


def test_completion_during_snapshot_is_buffered_before_resume():
    client = NativeFixture()
    client.turns[THREAD] = [turn(status="inProgress")]
    first = True

    def race(method, params):
        nonlocal first
        if method == "thread/resume":
            assert client._router.has_goal(THREAD)
        if method == "thread/turns/list" and first:
            first = False
            old = copy.deepcopy(client.turns[THREAD])
            client.turns[THREAD][0]["status"] = "completed"
            client.emit("turn/completed", {"threadId": THREAD, "turn": {"id": TURN, "status": "completed", "items": []}})
            return {"data": old, "nextCursor": None}
        return None

    client.on_request = race
    assert wait_for_completion(client, THREAD, turn_id=TURN)["turns"][0]["status"] == "completed"
    assert sum(method == "thread/resume" for method, _ in client.calls) == 1


def test_after_cursor_returns_each_unseen_native_terminal_turn():
    client = NativeFixture()
    client.turns[THREAD] = [turn(), turn(NEXT, status="interrupted", text="native interrupt report")]
    result = wait_for_completion(client, THREAD, after=TURN)
    assert [value["id"] for value in result["turns"]] == [NEXT]
    assert result["turns"][0]["status"] == "interrupted"
    assert result["after"] == NEXT


def test_retry_error_does_not_finish_and_disconnect_preserves_observed_ids(capsys):
    client = NativeFixture()
    client.turns[THREAD] = [turn(status="inProgress")]

    def disconnect():
        client.emit("turn/started", {"threadId": THREAD, "turn": {"id": NEXT}})
        client.emit("error", {"threadId": THREAD, "turnId": NEXT, "willRetry": True, "error": {"message": "native retry"}})
        client._router.fail_all(TransportClosedError("native connection lost"))
        client.on_wait = None

    client.on_wait = disconnect
    with pytest.raises(HandoffFailure) as caught:
        wait_for_completion(client, THREAD, turn_id=TURN)
    assert caught.value.result["threadId"] == THREAD
    assert caught.value.result["turnIds"] == [TURN, NEXT]
    assert "connection lost" in caught.value.result["error"]
    assert json.loads(capsys.readouterr().err)["params"]["willRetry"] is True
    assert sum(method == "thread/resume" for method, _ in client.calls) == 1


@pytest.mark.parametrize("status", ["active", "systemError", "newUnknownStatus"])
def test_purge_refuses_active_or_unknown_status_without_side_effects(tmp_path, status):
    client = NativeFixture()
    path = managed(client, tmp_path)
    client.threads[THREAD]["status"] = {"type": status, "activeFlags": ["waitingOnApproval", "waitingOnUserInput"]}
    with pytest.raises(ValueError):
        purge(client, THREAD, tmp_path, THREAD)
    assert client.deleted == []
    assert (path / "report.txt").read_text() == "managed output"


@pytest.mark.parametrize("condition", ["turn", "queue", "active-goal", "blocked-goal", "unknown-goal"])
def test_purge_refuses_runnable_native_state_even_when_thread_is_idle(tmp_path, condition):
    client = NativeFixture()
    path = managed(client, tmp_path)
    if condition == "turn":
        client.turns[THREAD][0]["status"] = "inProgress"
    elif condition == "queue":
        client.queues[THREAD] = [{"id": "native-queue-id", "input": []}]
    else:
        client.goals[THREAD] = {"threadId": THREAD, "status": condition.removesuffix("-goal")}
    with pytest.raises(ValueError):
        purge(client, THREAD, tmp_path, THREAD)
    assert client.deleted == []
    assert path.exists()


def test_purge_checks_archived_descendant_and_refuses_unreadable_native_state(tmp_path):
    client = NativeFixture()
    client.threads[CHILD] = thread(CHILD, THREAD)
    client.archived.add(CHILD)
    path = managed(client, tmp_path)

    def unavailable(method, params):
        if method == "thread/queue/list" and params["threadId"] == CHILD:
            raise ValueError("native archived queue cannot be read")
        return None

    client.on_request = unavailable
    with pytest.raises(ValueError):
        purge(client, THREAD, tmp_path, THREAD)
    assert client.deleted == []
    assert path.exists()
    queries = [params for method, params in client.calls if method == "thread/list"]
    assert {params["archived"] for params in queries} == {False, True}
    assert all(params["ancestorThreadId"] == THREAD and "subAgentThreadSpawn" in params["sourceKinds"] for params in queries)


def test_live_descendant_missing_from_persisted_list_is_still_refused(tmp_path):
    client = NativeFixture()
    client.threads[CHILD] = thread(CHILD, THREAD, status="active")
    client.on_request = lambda method, params: {"data": [], "nextCursor": None} if method == "thread/list" else None
    path = managed(client, tmp_path)
    with pytest.raises(ValueError):
        purge(client, THREAD, tmp_path, THREAD)
    assert client.deleted == []
    assert path.exists()


def test_missing_descendant_lineage_fails_closed(tmp_path):
    client = NativeFixture()
    client.threads[CHILD] = thread(CHILD)
    with pytest.raises(ValueError, match="lineage"):
        purge(client, THREAD, tmp_path, THREAD)
    assert client.deleted == []


def test_exact_confirmation_is_required_before_any_native_action(tmp_path):
    client = NativeFixture()
    with pytest.raises(ValueError):
        purge(client, THREAD, tmp_path, CHILD)
    assert client.calls == []


def test_success_captures_allowlist_before_delete_and_preserves_source(tmp_path):
    client = NativeFixture()
    client.threads[CHILD] = thread(CHILD, THREAD)
    root = tmp_path
    first = managed(client, root)
    second = managed(client, root, CHILD, worker="worker-b")
    source = tmp_path / "source"
    source.mkdir()
    (source / "uncommitted.txt").write_text("keep work")
    external = tmp_path / "grok-delivered-copy.txt"
    external.write_text("keep delivered copy")
    result = purge(client, THREAD, root, THREAD)
    assert result["nativeDeleteOutcome"] == "succeeded"
    assert set(result["preflightThreadIds"]) == {THREAD, CHILD}
    assert set(result["removedArtifacts"]) == {str(first), str(second)}
    assert not first.exists() and not second.exists()
    assert (source / "uncommitted.txt").read_text() == "keep work"
    assert external.read_text() == "keep delivered copy"
    assert client.attachments == {}


def test_dry_run_preserves_native_history_and_files(tmp_path):
    client = NativeFixture()
    path = managed(client, tmp_path)
    result = purge(client, THREAD, tmp_path, THREAD, dry_run=True)
    assert result["managedArtifacts"] == [str(path)]
    assert result["removedArtifacts"] == []
    assert client.deleted == []
    assert path.exists()


@pytest.mark.parametrize("bad_path", ["outside", "traversal", "symlink", "wrong-owner", "wrong-worker"])
def test_artifact_boundary_refuses_escapes_before_native_deletion(tmp_path, bad_path):
    client = NativeFixture()
    root = tmp_path
    path = managed(client, root)
    source = tmp_path / "source"
    source.mkdir()
    protected = source / "uncommitted.txt"
    protected.write_text("keep")
    attachment = client.attachments[THREAD][0]
    if bad_path == "outside":
        attachment["payload"]["path"] = str(source)
    elif bad_path == "traversal":
        attachment["payload"]["path"] = str(root / ".." / "source" / THREAD)
    elif bad_path == "symlink":
        (path / "source-link").symlink_to(source, target_is_directory=True)
    elif bad_path == "wrong-owner":
        attachment["payload"]["threadId"] = CHILD
    else:
        attachment["payload"]["worker"] = "../source"
    with pytest.raises(ValueError):
        purge(client, THREAD, root, THREAD)
    assert client.deleted == []
    assert protected.read_text() == "keep"


def test_native_delete_failure_never_removes_artifacts_or_replays(tmp_path):
    client = NativeFixture()
    path = managed(client, tmp_path)
    client.delete_error = TransportClosedError("accepted outcome cannot be observed")
    with pytest.raises(PurgeFailure) as caught:
        purge(client, THREAD, tmp_path, THREAD)
    assert caught.value.result["nativeDeleteOutcome"] == "unknown"
    assert path.exists()
    assert sum(method == "thread/delete" for method, _ in client.calls) == 1


def test_cleanup_failure_reports_successful_native_delete_and_keeps_source(tmp_path):
    client = NativeFixture()
    path = managed(client, tmp_path)
    source = tmp_path / "source"
    source.mkdir()
    protected = source / "uncommitted.txt"
    protected.write_text("keep")

    def swap(method, params):
        if method == "thread/delete":
            (path / "late-link").symlink_to(source, target_is_directory=True)
        return None

    client.on_request = swap
    with pytest.raises(PurgeFailure) as caught:
        purge(client, THREAD, tmp_path, THREAD)
    assert caught.value.result["nativeDeleteOutcome"] == "succeeded"
    assert caught.value.result["removedArtifacts"] == []
    assert protected.read_text() == "keep"


def test_handoff_refuses_symlink_artifacts_without_reading_external_files(tmp_path):
    path = tmp_path / "worker-a" / THREAD
    path.mkdir(parents=True)
    external = tmp_path / "secret.txt"
    external.write_text("private")
    (path / "leak").symlink_to(external)
    with pytest.raises(ValueError):
        artifact_paths(path, THREAD)


def test_handoff_keeps_terminal_result_when_artifact_validation_fails(tmp_path):
    client = NativeFixture()
    path = managed(client, tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("must not be delivered")
    (path / "unsafe-link").symlink_to(outside)
    with pytest.raises(HandoffFailure) as caught:
        wait_for_completion(client, THREAD, turn_id=TURN, artifacts=path)
    result = caught.value.result
    assert result["outcomes"] == [{"turnId": TURN, "status": "completed", "error": None}]
    assert result["turns"][0]["items"][0]["text"] == "complete report"
    assert str(path / "unsafe-link") in result["artifactError"]
    assert result["artifacts"] == []


def test_handoff_places_summary_before_large_transcript():
    client = NativeFixture()
    client.turns[THREAD] = [turn(text="LARGE_TRANSCRIPT_MARKER" * 10000)]
    result = wait_for_completion(client, THREAD, turn_id=TURN)
    encoded = json.dumps(result)
    assert encoded.index('"outcomes"') < encoded.index('"turns"')
    assert encoded.index('"artifacts"') < encoded.index('"turns"')
    assert result["outcomes"] == [{"turnId": TURN, "status": "completed", "error": None}]


def test_native_error_data_survives_handoff_without_retry():
    from openai_codex.errors import ServerBusyError

    client = NativeFixture()
    client.turns[THREAD] = [turn(status="inProgress")]
    native_data = {"codexErrorInfo": "server_overloaded", "acceptedThreadId": THREAD}

    def overloaded(method, params):
        if method == "thread/resume":
            raise ServerBusyError(-32001, "native capacity", native_data)
        return None

    client.on_request = overloaded
    with pytest.raises(HandoffFailure) as caught:
        wait_for_completion(client, THREAD, turn_id=TURN)
    assert caught.value.result["turnIds"] == [TURN]
    assert caught.value.result["error"] == {"code": -32001, "message": "native capacity", "data": native_data}
    assert sum(method == "thread/resume" for method, _ in client.calls) == 1


def test_status_change_observed_by_final_preflight_refuses_delete(tmp_path):
    client = NativeFixture()
    path = managed(client, tmp_path)
    def change(method, params):
        if method == "thread/goal/get":
            client.threads[THREAD]["status"] = {"type": "active", "activeFlags": []}
        return None

    client.on_request = change
    with pytest.raises(ValueError, match="activity changed"):
        purge(client, THREAD, tmp_path, THREAD)
    assert client.deleted == []
    assert path.exists()


def test_handoff_cli_returns_native_ids_and_exits_after_completion(monkeypatch, capsys):
    from contextlib import nullcontext
    from grok_codex_native import handoff

    client = NativeFixture()
    monkeypatch.setattr(client, "initialize", lambda: None)
    monkeypatch.setattr(handoff, "connect", lambda: nullcontext(client))
    assert handoff.main(["--thread", THREAD, "--turn", TURN]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["threadId"] == THREAD
    assert result["turns"][0]["id"] == TURN
    assert result["turns"][0]["items"][0]["text"] == "complete report"


def test_handoff_cli_preserves_completion_on_artifact_failure(tmp_path, monkeypatch, capsys):
    from contextlib import nullcontext
    from grok_codex_native import handoff

    client = NativeFixture()
    path = managed(client, tmp_path)
    (path / "unsafe-link").symlink_to(tmp_path / "outside")
    monkeypatch.setattr(client, "initialize", lambda: None)
    monkeypatch.setattr(handoff, "connect", lambda: nullcontext(client))
    assert handoff.main(["--thread", THREAD, "--turn", TURN, "--artifacts", str(path)]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["outcomes"][0]["status"] == "completed"
    assert result["turns"][0]["id"] == TURN
    assert str(path / "unsafe-link") in result["artifactError"]


def test_purge_cli_reports_refusal_and_preserves_history_and_source(tmp_path, monkeypatch, capsys):
    from contextlib import nullcontext
    from grok_codex_native import purge as command

    client = NativeFixture()
    path = managed(client, tmp_path)
    client.queues[THREAD] = [{"id": "native-queue"}]
    monkeypatch.setattr(client, "initialize", lambda: None)
    monkeypatch.setattr(command, "connect", lambda: nullcontext(client))
    assert command.main(["--thread", THREAD, "--artifacts-root", str(tmp_path), "--confirm-thread", THREAD]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["threadId"] == THREAD
    assert "queued" in result["error"]
    assert client.deleted == []
    assert path.exists()


def test_caller_cannot_relabel_a_source_directory_as_artifact_root(tmp_path):
    client = NativeFixture()
    source = tmp_path / "source"
    path = managed(client, source)
    protected = path / "uncommitted.py"
    protected.write_text("keep source")
    with pytest.raises(ValueError, match="trusted"):
        purge(client, THREAD, source, THREAD)
    assert client.calls == []
    assert protected.read_text() == "keep source"


def test_handoff_cannot_export_uuid_named_source_directory_outside_configured_root(tmp_path):
    source = tmp_path / "source" / "worker-a" / THREAD
    source.mkdir(parents=True)
    (source / "secret.txt").write_text("private source")
    with pytest.raises(ValueError, match="configured"):
        artifact_paths(source, THREAD)


def test_broken_handoff_output_cannot_turn_refusal_into_reader_failure(monkeypatch):
    class ClosedOutput:
        def write(self, value):
            raise BrokenPipeError("closed caller")

        def flush(self):
            raise BrokenPipeError("closed caller")

    from grok_codex_native import client as connection

    monkeypatch.setattr(connection.sys, "stderr", ClosedOutput())
    assert refuse_request("item/commandExecution/requestApproval", {"threadId": THREAD}) == {"decision": "decline"}


def test_latest_loaded_status_overrides_earlier_metadata_snapshot(tmp_path):
    client = NativeFixture()
    path = managed(client, tmp_path)
    goal_checked = False

    def native_status(method, params):
        nonlocal goal_checked
        if method == "thread/goal/get":
            goal_checked = True
        if method == "thread/loaded/list" and goal_checked:
            client.threads[THREAD]["status"] = {"type": "active", "activeFlags": []}
        return None

    client.on_request = native_status
    with pytest.raises(ValueError, match="activity changed"):
        purge(client, THREAD, tmp_path, THREAD)
    assert client.deleted == []
    assert path.exists()


def test_after_cursor_survives_sdk_closing_old_goal_route(monkeypatch):
    client = NativeFixture()

    def complete_old_goal(method, params):
        if method == "thread/resume":
            goal = {
                "threadId": THREAD, "objective": "old goal", "status": "complete",
                "timeUsedSeconds": 1, "tokensUsed": 1, "createdAt": 1, "updatedAt": 2,
            }
            client._router.route_notification(client._coerce_notification(
                "thread/goal/updated", {"threadId": THREAD, "goal": goal}
            ))
            client._router.route_notification(client._coerce_notification(
                "turn/completed", {"threadId": THREAD, "turn": turn()}
            ))
        return None

    def future_completion(state):
        if state.is_finished():
            raise AssertionError("SDK already closed this logical route")
        client.turns[THREAD].append(turn(NEXT, text="next native completion"))
        client._router.route_notification(client._coerce_notification(
            "turn/completed", {"threadId": THREAD, "turn": turn(NEXT)}
        ))
        return CodexClient.next_goal_notification(client, state)

    client.on_request = complete_old_goal
    monkeypatch.setattr(client, "next_goal_notification", future_completion)
    result = wait_for_completion(client, THREAD, after=TURN)
    assert result["after"] == NEXT
    assert result["turns"][0]["items"][0]["text"] == "next native completion"
