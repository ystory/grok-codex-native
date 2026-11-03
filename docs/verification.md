# Verify a target-VM deployment

Keep [historical maintainer results](verification-results.md) separate from checks on your installation. Use disposable tasks and fresh worker names. Keep sensitive logs and files in a private data directory. Record the command, CLI and SDK versions, worker and native IDs, expected result, and actual result. Investigate failures before marking the affected capability complete.

## Verify local behavior tests

From the project directory, use a virtual environment with the package dependencies and pytest installed.

```sh
python3 -m venv /tmp/grok-codex-test-venv
/tmp/grok-codex-test-venv/bin/python -m pip install -e '.[dev]'
/tmp/grok-codex-test-venv/bin/python -m pytest
```

These tests check policy and packaging behavior. They do not prove real native model execution, Docker isolation, or Grok delivery. Deployment dependencies remain pinned by the supplied lockfiles.

## Verify deployment and authentication

```sh
python3 scripts/check-deployment.py
./scripts/compose.sh exec -T control codex --version
./scripts/compose.sh exec -T control python -m pip show openai-codex
./scripts/healthcheck.sh
```

Confirm CLI `0.166.0`, SDK `0.163.0`, native initialization, and account type `chatgpt`. Check repeated standard transport connections.

```sh
./scripts/compose.sh exec -T control python checks/native_transport.py --socket /run/grok-codex/control.sock
```

After creating a worker, compare `register-worker.py` output with `environment/status`. Registration is separate from actual command execution.

## Run the supplied execution checks

The examples use the default project name `grok-codex`. For a custom project name, use actual container IDs from creation output. Each check name must be new in this deployment. If it exists, choose another name and update every subsequent command. Model checks consume ChatGPT subscription limits.

Create two workers and pass their published ports to the checks.

```sh
python3 scripts/new-worker.py verify-a --browser-port 3000
python3 scripts/new-worker.py verify-b --browser-port 3000
VERIFY_PORT_A=$(docker inspect --format '{{(index (index .NetworkSettings.Ports "3000/tcp") 0).HostPort}}' grok-codex-worker-verify-a)
VERIFY_PORT_B=$(docker inspect --format '{{(index (index .NetworkSettings.Ports "3000/tcp") 0).HostPort}}' grok-codex-worker-verify-b)
./scripts/compose.sh exec -T control python checks/smoke.py --worker-a grok-codex-worker-verify-a --worker-b grok-codex-worker-verify-b --port-a "$VERIFY_PORT_A" --port-b "$VERIFY_PORT_B" --run-models
./scripts/compose.sh exec -T control python checks/lifecycle.py --worker grok-codex-worker-verify-a --run-models
./scripts/compose.sh exec -T control python checks/start_only.py --worker grok-codex-worker-verify-a --run-models
```

`smoke.py` checks actual dependencies, files, PNGs, and HTTP responses. `lifecycle.py` checks status, steer, queue registration and cancellation, interruption, follow-up turns, and disposable deletion. `start_only.py` checks immediate return, separate completion waiters, and automatic queued execution through actual file order.

Check executor authentication and large native protocol messages on the same worker. This check does not call a model.

```sh
VERIFY_EXEC_PORT=$(docker inspect --format '{{(index (index .NetworkSettings.Ports "5000/tcp") 0).HostPort}}' grok-codex-worker-verify-a)
./scripts/compose.sh exec -T control python checks/protocol.py --worker grok-codex-worker-verify-a --exec-port "$VERIFY_EXEC_PORT"
```

Create a local fixture for a repository ref, skill, image, and fake secret. Keep the fixture outside managed data.

```sh
VERIFY_ROOT=$(mktemp -d /tmp/grok-codex-verify.XXXXXX)
python3 checks/fixture_repo.py "$VERIFY_ROOT/fixture"
python3 scripts/new-worker.py verify-input --repo file:///run/grok-codex/inputs/fixture.git --ref refs/pull/123/head --inputs-dir "$VERIFY_ROOT/fixture/inputs" --secrets-dir "$VERIFY_ROOT/fixture/secrets"
./scripts/compose.sh exec -T control python checks/native_inputs.py --worker grok-codex-worker-verify-input --run-models
```

The fixture secret is a public test string. Do not substitute real credentials. Check the reported model, effort, native IDs, and actual artifacts. Complete the manual acceptance checks below for behavior these scripts do not cover.

## Verify native execution and isolation

1. Create two fresh workers with `--browser-port 3000`. Mount a sentinel input file and fake secret in only one worker.
2. Start a native Codex turn in each worker. Have each turn write and reread a different sentinel in `/work/source`. Confirm `apply_patch`, repository `AGENTS.md`, requested skill input, and input-file reads execute in that worker.
3. Install different dependency versions in separate worker virtual environments or projects. Confirm installation in one worker does not change the other's version.
4. Start servers with different responses on `0.0.0.0:3000` in both workers. Check each response through its distinct VM loopback `browserPorts` mapping and your actual browser tool.
5. Check required external Git and package access. Confirm mounted secrets are available only where required and absent from the other worker. Do not log secret values.
6. Check that workers lack the control ChatGPT credential directory and Docker socket. Inspect access to host source and the native store. Confirm missing and incorrect exec WebSocket tokens are rejected. Redact token values from logs.
7. Keep Docker's default seccomp and AppArmor policies enabled. Verify actual file and command execution in a native `externalSandbox` turn. Confirm host files, control credentials, and the Docker socket are not exposed.
8. Run an image task and confirm the model reads its content. If using `localImage`, inspect path resolution directly. A file's presence in a container is not proof of model access.

Docker exec inspection checks file placement. Native Codex reads and writes prove the execution path.

## Verify lifecycle and control

Start execution and completion waiting in Shell A. In Shell B, call `thread_read`, `turn_steer`, native queue methods, and `turn_interrupt`. Confirm B can inspect and control without `thread/resume`, and A receives the terminal result. Control must survive the first SDK connection closing.

Test a fresh turn, queued after-current input, and an interrupt followed by new input. Leave an after-current queue item in place and confirm it automatically starts after the first turn ends. Compare actual native IDs and execution order. Preserve `willRetry` and final errors. After disconnecting, read the thread to establish acceptance and execution state.

Queue only behind an active turn. For an unloaded thread, inspect and explicitly cancel leftover queued input before resume. This includes a waiter that may resume the thread. Then start intended input with an explicit worker in `turn/start.environments`. The field does not belong to `thread/resume`. Do not treat queued execution without a worker after restart as success.

Verify exact native decline responses to server requests. A disconnected client after a handler exception or fallback to SDK default approval fails the check. Interactive approval relay between conversations is not implemented.

## Verify history and lists

Create multiple turns with large outputs. Export full history and confirm every turn, command item, and final output appears through the last page. Test origin-based task lookup, all-task listing, archive, restore, rename, and PR attachment reverse lookup with actual native IDs.

Use history larger than the current Grok Shell output limit. Confirm `outcomes`, `after`, and `artifacts` appear before the large history, and the full export file can be read. A test symlink artifact must produce exit code 1 and `artifactError` while preserving native completion state and history.

Create regular artifact files and compare their paths and content with the returned list. Verify rejection of symlinks, special files, and artifact paths outside the managed root.

## Verify completion delivery in Grok

1. Read the current tool specifications for Task parent completion notification and subagent Shell waiting. Record the installed project path and execution computer.
2. Start a nontrivial native task with `launch_and_wait.py --no-wait`. Give an existing Task subagent the handoff command with the returned IDs and artifact path. Keep native IDs, Task ID, and subagent Shell terminal ID in the original conversation.
3. End the parent's response and confirm Task completion wakes the original conversation without new user input. Also test a task that exceeds the subagent Shell's maximum wait duration.
4. Report terminal state and errors from the JSON result. Send real files with `SendMessage` attachments, then open them in Grok and inspect the content.
5. Start a follow-up turn and a new finite waiter. Confirm delivery repeats after the first turn.
6. Watch another Bot's thread and receive its next completion. Unwatch through the recorded waiter PID inside control. Confirm the waiter and transport child exit while native execution continues. Unwatch before archiving or deleting the thread too.

A delayed Task wakeup or completed Shell wait alone does not establish the full native execution, wakeup, and attachment flow. Record concrete failures and their effects.

## Verify restart and removal

After control restarts, read an existing native thread. Re-register its worker environment and execute a fresh turn with that environment explicitly selected. Report recovery of a previously active turn only if you actually observe it.

Request purge dry runs on disposable threads with active execution, queued input, an active goal, and a runnable descendant. Each must be refused. Also check exact UUID confirmation and the configured artifact root. After separate stopping and confirmation, delete only managed history and artifacts. Source sentinels and uncommitted changes must remain. Distinguish native deletion errors from artifact deletion failures in results.

Finally remove workers and run `compose.sh down`. Compare preserved source, artifacts, runtime, and native data with removed containers and networks. Run history deletion checks only on disposable threads.
