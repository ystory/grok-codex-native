import argparse
import json
from openai_codex.client import CodexClient, CodexConfig

parser = argparse.ArgumentParser()
parser.add_argument("--socket", required=True)
parser.add_argument("--websocat", default="websocat4")
args = parser.parse_args()
config = CodexConfig(launch_args_override=(args.websocat, "-t", "-", "ws-c:unix-connect:" + args.socket))
for attempt in range(2):
    with CodexClient(config, approval_handler=lambda method, params: {"decision": "decline"}) as client:
        initialized = client.initialize()
        threads = client.thread_list()
        print(json.dumps({"connection": attempt + 1, "initialize": initialized.model_dump(mode="json", by_alias=True), "threadCount": len(threads.data)}), flush=True)
