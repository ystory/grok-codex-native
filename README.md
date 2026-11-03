# Grok Codex native

Delegate coding tasks from your Grok Bot to Codex on the Bot's own cloud computer, using your ChatGPT account.

Each task runs in its own Docker worker. Your Bot can steer it, queue follow-ups, read its full history, and receive the result and files back in the original conversation.

This is an independent project. It is not affiliated with or endorsed by xAI, OpenAI, or Anysphere. Grok, Codex, and Cursor are trademarks of their respective owners.

## What it covers

The project moves the delegation workflow of Grok Bot's built-in Cloud Agent to Codex. Of the Cloud Agent's 16 capabilities:

- **8 are ported:** model settings, repository skills, completion notifications, watching other tasks, status, task lists, PR lookup, and artifact lists.
- **6 are partial:** starting a task, starting without a repository, follow-ups, full history, stopping and cleanup, and discovering choices.
- **1 is narrowed:** tasks run only in Docker workers on the Bot's cloud computer.
- **1 is not ported:** project mode.

See the [capability matrix](docs/contract.md#capability-matrix) for each difference.

## Limits

- **Sleep pauses work.** In testing, a task resumed after a manual Wake, but it did not run during sleep.
- **Reset and computer updates end running tasks.** Files, history, and login survive, and you continue with a new turn.
- **`interrupted` does not mean stopped.** A process outlived that state in testing.
- **Eight concurrent light tasks were tested, not guaranteed.** Your capacity depends on the workload.
- **Workers share the host kernel.** They are not separate VMs.

See [what was tested](docs/contract.md#what-was-tested) for the measurements.

## Requirements

- A Grok Bot and its cloud computer. The installer needs passwordless `sudo -n` there.
- A ChatGPT account with Codex access and [device code login](https://learn.chatgpt.com/docs/auth) turned on. Enable it in ChatGPT's security settings; a workspace admin controls it for workspace accounts.

Install this on your own cloud computer, for your own use, with your own ChatGPT account. OpenAI's [terms](https://openai.com/policies/row-terms-of-use/) apply to that account, and [all your Bots share the computer's command-line credentials](https://docs.x.ai/grok-bot/computer-and-apps).

## Install

> **Draft:** these steps have not yet been run from a fresh checkout of this repository.

Paste this into your Bot:

> Install grok-codex-native on your cloud computer through Shell. Run `git clone --branch v0.1.0 --depth 1 https://github.com/ystory/grok-codex-native.git /workspace/grok-codex-native`, then run `python3 scripts/install-grok-cloud.py` in that directory as the normal user, without sudo. If it reports `authentication_still_required`, run the device login from the project's README and give me the link and code. Then save `skill/grok-codex-native/SKILL.md` as a private skill, enable it for this Bot, and report the health check result.

To run the same steps yourself in the cloud computer's Shell:

```sh
git clone --branch v0.1.0 --depth 1 https://github.com/ystory/grok-codex-native.git /workspace/grok-codex-native
cd /workspace/grok-codex-native
python3 scripts/install-grok-cloud.py
sg docker -c './scripts/compose.sh exec control codex login --device-auth'
sg docker -c './scripts/healthcheck.sh'
```

Skip the login command if the installer finds an existing login. Keep tokens out of chat. Then [register the skill](docs/operations.md#register-the-shared-skill).

You can also install from a [release archive](https://github.com/ystory/grok-codex-native/releases). [Verify its checksum and manifest](docs/operations.md#build-and-verify-a-release-archive) first, and extract it to the same path.

The project stays in `/workspace/grok-codex-native` and private data in `/workspace/grok-codex-data`, because Grok documents [`/workspace` as the durable location for project files](https://docs.x.ai/grok-bot/computer-and-apps#work-with-files). The installer rebuilds Docker and images after a [computer replacement](docs/operations.md#recover-after-hosted-computer-replacement).

## Use it

Ask your Bot by skill name:

> Use the grok-codex-native skill to [task] in this repository. When it finishes, report the verification results and attach the generated files to this conversation.

The skill creates a worker, starts the task, and waits for completion through a finite Task. It does not change your Bot's global instructions, memory, or default Cloud Agent routing.

## Documentation

- [Capabilities and limits](docs/contract.md)
- [Native task operations](docs/native-use.md): inputs, control, history, watch, lookup, and deletion
- [Operations](docs/operations.md): restart, recovery, upgrades, packaging, and removal
- [Verification](docs/verification.md): runnable checks and manual acceptance steps
- [Pinned sources](docs/sources.md): protocol and product references
- [Glossary](GLOSSARY.md)

## Development and license

See the [development instructions](CONTRIBUTING.md) for local tests and what CI verifies.

Copyright 2026 Yongsul Kim. Licensed under [MIT](LICENSE). Upstream components retain their own licenses.
