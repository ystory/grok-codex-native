# Grok Codex native

Delegate coding tasks from Grok Bot to native Codex on a Linux VM. Run each task in a Docker worker, send follow-up instructions, inspect history, and collect files for the original Grok conversation.

Codex owns sessions, history, queued input, and errors. A control container holds the ChatGPT login and native store. Workers provide task-specific execution environments. The project adds deployment scripts, a finite completion waiter, and preflight checks for permanent deletion.

The pinned versions are Codex CLI `0.166.0`, official Python SDK `0.163.0`, and websocat `4.0.0-alpha3`. Native environments and `deferred_executor` are experimental. Docker workers share the host kernel. See [capabilities and limits](docs/contract.md).

The maintainer verified the backend on Linux arm64. Production Grok installation, skill registration, and complete delivery with real attachments remain unverified. See the [verification report](docs/verification-results.md).

## Prerequisites

- Linux `amd64` or `arm64`, Python `3.11` or later, Docker Engine, and the Compose plugin. Actual execution has been verified only on arm64.
- Permission to control Docker, with worker isolation and Linux host networking support.
- Network access for images, packages, Git, ChatGPT login, and Codex services.
- A ChatGPT account with Codex access. GitHub uses separate credentials.

Run commands on the selected VM through Grok's existing computer selection or SSH. Grok computers, files, and command-line credentials are [shared across Bots](https://docs.x.ai/grok-bot/computer-and-apps).

## Install and log in

Keep this directory as a standalone checkout, preferably `~/code/grok-codex-native`. Any absolute installation path works. If using an archive, [verify its checksum and manifest](docs/operations.md#build-and-verify-a-release-archive) first.

From the directory containing `scripts/`, run the following commands. Keep the private data directory outside the checkout.

```sh
cd "$HOME/code/grok-codex-native"
./scripts/prepare.sh "$HOME/.local/share/grok-codex"
./scripts/compose.sh --profile build-worker build
./scripts/compose.sh up -d --wait control
./scripts/compose.sh exec control codex login --device-auth
./scripts/healthcheck.sh
```

Authenticate on the displayed site. Keep tokens out of chat. Preparation preserves existing native configuration and credentials. Health checks verify connectivity and ChatGPT account type. The deployment keeps `include_local = false`.

## Start a task

Create a fresh worker. This example initializes a local Git repository without publishing it.

```sh
python3 scripts/new-worker.py example --browser-port 3000
cp examples/brief.json /tmp/grok-codex-brief.json
```

For an existing repository, add `--repo REPOSITORY_URL --ref REF`. Configure private Git access first. Worker output includes environment IDs, source and artifact paths, and VM loopback browser ports. See [inputs and prepared images](docs/native-use.md).

Edit the copied brief with the task, title, and `origin`, a stable calling Bot or conversation identity. Keep worker and environment IDs consistent. Set a model, settings, or repository skill only when explicitly requested.

```sh
./scripts/compose.sh exec -T control python examples/launch_and_wait.py --no-wait /dev/stdin < /tmp/grok-codex-brief.json
```

Give Grok's existing Task subagent this finite waiter with the returned IDs and artifact path.

```sh
./scripts/compose.sh exec -T control python -m grok_codex_native.handoff --thread THREAD_ID --turn TURN_ID --artifacts /var/lib/grok-codex/artifacts/WORKER/THREAD_ID
```

Keep native IDs, Task ID, and the subagent Shell terminal ID in the original conversation. When Task returns, the parent reads terminal state and errors, then sends actual files through `SendMessage`. Map control paths to host paths or copy files to Grok's computer. A path in text is not an attachment.

Complete the [Grok delivery checks](docs/verification.md#verify-completion-delivery-in-grok) and [skill registration](docs/operations.md#register-the-shared-skill) for your installation. Shell-only background wakeup remains unverified. For direct terminal execution that waits in one process, omit `--no-wait`.

## Further use

- [Native task operations](docs/native-use.md) covers inputs, control, history, watch, lookup, and deletion.
- [Operations](docs/operations.md) covers restart, upgrades, packaging, and removal.
- [Verification](docs/verification.md) provides runnable checks and manual acceptance steps.
- [Pinned sources](docs/sources.md) records protocol and product references.

The project returns native IDs, history files, artifacts, and actual PR URLs. It creates no hosted task page or Codex deep link.

## Development and license

See [development instructions](CONTRIBUTING.md) for local tests and what CI verifies.

Copyright 2026 Yongsul Kim. Licensed under [Apache-2.0](LICENSE). Upstream components retain their own licenses.
