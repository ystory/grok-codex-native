import argparse
import base64
import http.client
import json

from openai_codex.errors import InvalidParamsError
from pydantic import RootModel

from grok_codex_native.client import connect, require_chatgpt


def main():
    parser = argparse.ArgumentParser(description="Verify native large messages and unauthenticated executor rejection without model calls")
    parser.add_argument("--exec-port", required=True, type=int)
    parser.add_argument("--worker", required=True)
    args = parser.parse_args()
    expected = "native-대형-\U0001f680" + "x" * 65450
    with connect() as c:
        c.initialize()
        require_chatgpt(c)
        thread = c.thread_start({"environments": [{"environmentId": args.worker, "cwd": "/work/source"}]}).thread.id
        c.thread_set_name(thread, "Disposable large transport verification")
        identities = [f"protocol-probe-{index}" for index in range(3)]
        for identity in identities:
            c.request("thread/attachment/add", {"threadId": thread, "attachmentType": "grok.protocol.probe",
                      "identityKey": identity, "payload": {"text": expected}}, response_model=RootModel[dict])
        result = c.request("thread/attachment/list", {"threadId": thread}, response_model=RootModel[dict]).root
        assert len(result["data"]) == 3 and result["nextCursor"] is None
        assert all(item["payload"]["text"] == expected for item in result["data"])
        try:
            c.request("thread/attachmentOwner/list", {"attachmentType": "probe", "identityKey": "x" * 180000}, response_model=RootModel[dict])
        except InvalidParamsError as error:
            assert error.code == -32602
        else:
            raise AssertionError("native oversized identity must be rejected")
        c.account_read()
        for identity in identities:
            c.request("thread/attachment/remove", {"threadId": thread, "attachmentType": "grok.protocol.probe",
                      "identityKey": identity}, response_model=RootModel[dict])
    rejected = []
    for token in (None, "deliberately-invalid-probe"):
        headers = {"Upgrade": "websocket", "Connection": "Upgrade", "Sec-WebSocket-Version": "13",
                   "Sec-WebSocket-Key": base64.b64encode(b"native-probe-key").decode()}
        if token:
            headers["Authorization"] = "Bearer " + token
        connection = http.client.HTTPConnection("127.0.0.1", args.exec_port, timeout=5)
        try:
            connection.request("GET", "/", headers=headers)
            response = connection.getresponse()
            assert response.status in (401, 403), (response.status, response.read()[:200].decode())
            rejected.append(response.status)
        finally:
            connection.close()
    print(json.dumps({"verified": ["large valid native request", "large exact native response", "native RPC error", "subsequent request", "executor authentication"],
                      "responseBytes": len(json.dumps(result).encode()), "rejectedHttpStatuses": rejected}), flush=True)


if __name__ == "__main__":
    main()
