import json
import sys

from grok_codex_native.client import connect, require_chatgpt
from pydantic import RootModel

payload = json.load(sys.stdin)
with connect() as client:
    client.initialize()
    require_chatgpt(client)
    client.request("environment/add", payload, response_model=RootModel[dict])
    info = client.request(
        "environment/info", {"environmentId": payload["environmentId"]},
        response_model=RootModel[dict],
    )
    print(json.dumps({"environmentId": payload["environmentId"], "nativeEnvironmentInfo": info.root}))
