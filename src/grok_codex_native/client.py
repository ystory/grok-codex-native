import json
import os
import sys
from pathlib import Path

from openai_codex.client import CodexClient, CodexConfig
from openai_codex.models import JsonObject
from pydantic import RootModel


def refuse_request(method: str, params: JsonObject | None) -> JsonObject:
    responses: dict[str, JsonObject] = {
        "item/commandExecution/requestApproval": {"decision": "decline"},
        "item/fileChange/requestApproval": {"decision": "decline"},
        "item/permissions/requestApproval": {"permissions": {}, "scope": "turn"},
        "item/tool/requestUserInput": {"answers": {}},
        "mcpServer/elicitation/request": {"action": "decline", "content": None, "_meta": None},
        "item/tool/call": {"success": False, "contentItems": []},
    }
    response = responses.get(method, {})
    try:
        print(
            json.dumps({"serverRequest": {"method": method, "params": params}, "response": response}),
            file=sys.stderr,
            flush=True,
        )
    except (OSError, ValueError):
        pass
    return response


def connect(socket: str | Path | None = None) -> CodexClient:
    socket_path = str(socket or os.environ.get("GROK_CODEX_SOCKET", "/run/grok-codex/control.sock"))
    if not Path(socket_path).is_absolute() or any(c in socket_path for c in "\x00\r\n"):
        raise ValueError("GROK_CODEX_SOCKET must be an absolute Unix socket path")
    return CodexClient(
        CodexConfig(
            launch_args_override=(
                "websocat4", "-t", "-",
                f"ws-c:unix-connect:{socket_path}",
            ),
            client_name="grok_codex_native",
            client_title="Grok native Codex",
        ),
        approval_handler=refuse_request,
    )


def require_chatgpt(client: CodexClient) -> dict:
    account = client.request(
        "account/read", {"refreshToken": False}, response_model=RootModel[dict]
    ).root
    if not isinstance(account.get("account"), dict) or account["account"].get("type") != "chatgpt":
        raise PermissionError("Native Codex must be logged in with a ChatGPT subscription")
    return account
