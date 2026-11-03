import argparse
import json

from grok_codex_native.client import connect, require_chatgpt


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--require-chatgpt", action="store_true")
    args = parser.parse_args()
    with connect() as client:
        initialized = client.initialize()
        account = (
            require_chatgpt(client) if args.require_chatgpt
            else client.account_read().model_dump(mode="json", by_alias=True)
        )
        account_type = (account.get("account") or {}).get("type")
        print(json.dumps({
            "native": initialized.model_dump(mode="json", by_alias=True),
            "accountType": account_type,
        }))


if __name__ == "__main__":
    main()
