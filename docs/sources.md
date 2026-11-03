# Pinned versions and source references

The implementation and documentation target Codex CLI `0.166.0`, official Python SDK `0.163.0`, and websocat `4.0.0-alpha3`. The Codex source commit is `6ff1e1ca3b30fc8b914720681fcf5fe62e95f856`. Later-version features are not evidence of support in these versions.

## Codex protocol and transport

- The [Codex 0.166.0 release](https://github.com/openai/codex/releases/tag/rust-v0.166.0) establishes the CLI version.
- The [pinned app-server documentation](https://github.com/openai/codex/blob/6ff1e1ca3b30fc8b914720681fcf5fe62e95f856/codex-rs/app-server/README.md) describes native lifecycle, protocol, and stored thread attachments.
- The [thread protocol](https://github.com/openai/codex/blob/6ff1e1ca3b30fc8b914720681fcf5fe62e95f856/codex-rs/app-server-protocol/src/protocol/v2/thread.rs) defines thread start, listing, queue, goal, and full-history parameters.
- The [turn protocol](https://github.com/openai/codex/blob/6ff1e1ca3b30fc8b914720681fcf5fe62e95f856/codex-rs/app-server-protocol/src/protocol/v2/turn.rs) defines native input items, steer preconditions, and interruption.
- The [environment protocol](https://github.com/openai/codex/blob/6ff1e1ca3b30fc8b914720681fcf5fe62e95f856/codex-rs/app-server-protocol/src/protocol/v2/environment.rs) defines dynamic environments, authentication tokens, and status queries.
- The [environment processor](https://github.com/openai/codex/blob/6ff1e1ca3b30fc8b914720681fcf5fe62e95f856/codex-rs/app-server/src/request_processors/environment_processor.rs) establishes in-memory registration.
- The [queue processor](https://github.com/openai/codex/blob/6ff1e1ca3b30fc8b914720681fcf5fe62e95f856/codex-rs/app-server/src/request_processors/thread_queue_processor.rs) and [native queue service](https://github.com/openai/codex/blob/6ff1e1ca3b30fc8b914720681fcf5fe62e95f856/codex-rs/ext/queue/src/service.rs) establish queued input and idle dispatch behavior.
- The [native thread deletion implementation](https://github.com/openai/codex/blob/6ff1e1ca3b30fc8b914720681fcf5fe62e95f856/codex-rs/app-server/src/request_processors/thread_delete.rs) can terminate live threads during deletion. This project's purge preflight refuses runnable state before calling native deletion.
- The [pinned Python client source](https://github.com/openai/codex/blob/6ff1e1ca3b30fc8b914720681fcf5fe62e95f856/sdk/python/src/openai_codex/client.py) informed investigation of SDK requests and server-request handlers. Deployment uses released `openai-codex==0.163.0`, not the source tree's development version.
- [Websocat 4.0.0-alpha3](https://github.com/vi/websocat/releases/tag/v4.0.0-alpha3) provides the standard transport between the native Unix socket and official SDK stdio. The raw byte relay in Codex `app-server proxy` is not the SDK's JSONL transport.

## Grok Bot integration

The design review consulted these official Grok Bot documents on 2026-11-03.

- [Use the computer and apps](https://docs.x.ai/grok-bot/computer-and-apps) describes account-shared computers, filesystems, command-line access, and sharing across Bots. Separate Bot screens do not establish separate security boundaries.
- [Skills and routines](https://docs.x.ai/grok-bot/skills-routines-and-automations) describes the shared private skill library, per-Bot activation, and supported skill-saving UI.
- [Work with files](https://docs.x.ai/grok-bot/computer-and-apps#work-with-files) and [update the hosted computers](https://docs.x.ai/grok-bot/deployment#update-the-hosted-computers) distinguish durable `/workspace` files from replaceable packages and runtime environments.

These pages do not establish detailed Shell parameters or the file-attachment tool schema. Those were observed in testing; see [what was tested](contract.md#what-was-tested). Grok Build CLI paths are not Grok Bot skill installation paths.

## Evidence boundaries

Distinguish source-defined capability, deployment configuration, actual backend execution, Grok registration, and file delivery. The [capabilities page](contract.md) records supported paths and observed limits. Private credentials, captures, and execution records are excluded from public source and the skill body.

The control evidence also distinguishes native terminal state from physical command state: native interruption returned successfully while the worker PID was still alive about 8.4 seconds later, and native full history omitted that command's item. Later PID absence does not identify the exact stop time or root cause. These observations limit completion claims; they do not establish a diagnosed defect inside the pinned `deferred_executor` implementation.

## Development and repository conventions

Repository conventions were reviewed on 2026-11-03. The scope was documentation, contribution instructions, and validation. The execution architecture remains this project's own native Codex deployment.

- [Vercel Labs durable-harness contribution guide](https://github.com/vercel-labs/durable-harness/blob/135dee512c6a7aebe710617426c4947c9d2ad498/CONTRIBUTING.md), from a repository created on 2026-10-16, informed the short development guide and separation of credential-free checks from live checks.
- [Cloudflare cf CI](https://github.com/cloudflare/cf/blob/05b37bc3a4f5aa7c77bf013a239047df18161c24/.github/workflows/ci.yml), from a repository created on 2026-09-21, informed read-only permissions, action SHA pins, bounded jobs, and cancellation of superseded runs.
- [GitHub's Python workflow guide](https://docs.github.com/en/actions/tutorials/build-and-test-code/python) and [workflow syntax](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax) define Python setup, job dependencies, permissions, concurrency, and cleanup conditions. The action pins resolve to checkout v7.0.1 and setup-python v7.1.0.
- [The Ubuntu 24.04 runner inventory](https://github.com/actions/runner-images/blob/main/images/ubuntu/Ubuntu2404-Readme.md) lists Docker and Compose. The inventory can change; CI selects the runner label explicitly.
- Docker's [config](https://docs.docker.com/reference/cli/docker/compose/config/), [build](https://docs.docker.com/reference/cli/docker/compose/build/), [up](https://docs.docker.com/reference/cli/docker/compose/up/), and [run](https://docs.docker.com/reference/cli/docker/compose/run/) references distinguish static configuration, image builds, healthy startup, and a disposable command. CI starts control without login and runs the worker binary. The deployment helper's login guard leaves worker registration and model turns to manual acceptance.
- The [Python Packaging User Guide](https://packaging.python.org/en/latest/tutorials/packaging-projects/) and [pyproject reference](https://packaging.python.org/en/latest/guides/writing-pyproject-toml/) define the build metadata, license declaration, and distribution workflow. CI verifies an installed wheel and builds the deployment archive.

Two hosted failures led to an explicit Bake `network.host` grant and exact checkout-root context overrides for both image targets.
