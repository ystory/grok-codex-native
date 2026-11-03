# Verify your installation

Keep [recorded maintainer results](contract.md#what-was-tested) separate from checks on your installation. Use disposable tasks and fresh worker names. Keep sensitive logs and files in a private data directory. Record the command, CLI and SDK versions, worker and native IDs, expected result, and actual result. Investigate failures before marking the affected capability complete.

Run these commands through Grok's Shell on its account-shared cloud computer. Use the installer's Docker group context for host Docker commands, as in the examples below. The [expanded acceptance probes](#expanded-acceptance-probes) run through control's stdin without rebuilding its image.

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
sg docker -c './scripts/compose.sh exec -T control codex --version'
sg docker -c './scripts/compose.sh exec -T control python -m pip show openai-codex'
sg docker -c './scripts/healthcheck.sh'
```

Confirm CLI `0.166.0`, SDK `0.163.0`, native initialization, and account type `chatgpt`. Check repeated standard transport connections.

```sh
sg docker -c './scripts/compose.sh exec -T control python checks/native_transport.py --socket /run/grok-codex/control.sock'
```

After creating a worker, compare `register-worker.py` output with `environment/status`. Registration is separate from actual command execution.

## Run the supplied execution checks

The examples use the default project name `grok-codex`. For a custom project name, use actual container IDs from creation output. Each check name must be new in this deployment. If it exists, choose another name and update every subsequent command. Model checks consume ChatGPT subscription limits.

Create two workers and pass their published ports to the checks.

```sh
sg docker -c 'python3 scripts/new-worker.py verify-a --browser-port 3000'
sg docker -c 'python3 scripts/new-worker.py verify-b --browser-port 3000'
VERIFY_PORT_A=$(sg docker -c 'docker inspect --format "{{(index (index .NetworkSettings.Ports \"3000/tcp\") 0).HostPort}}" grok-codex-worker-verify-a')
VERIFY_PORT_B=$(sg docker -c 'docker inspect --format "{{(index (index .NetworkSettings.Ports \"3000/tcp\") 0).HostPort}}" grok-codex-worker-verify-b')
export VERIFY_PORT_A VERIFY_PORT_B
sg docker -c './scripts/compose.sh exec -T control python checks/smoke.py --worker-a grok-codex-worker-verify-a --worker-b grok-codex-worker-verify-b --port-a "$VERIFY_PORT_A" --port-b "$VERIFY_PORT_B" --run-models'
sg docker -c './scripts/compose.sh exec -T control python checks/lifecycle.py --worker grok-codex-worker-verify-a --run-models'
sg docker -c './scripts/compose.sh exec -T control python checks/start_only.py --worker grok-codex-worker-verify-a --run-models'
```

`smoke.py` checks actual dependencies, files, PNGs, and HTTP responses. `lifecycle.py` checks status, steer, queue registration and cancellation, interruption, follow-up turns, and disposable deletion. `start_only.py` checks immediate return, separate completion waiters, and automatic queued execution through actual file order.

Check executor authentication and large native protocol messages on the same worker. This check does not call a model.

```sh
VERIFY_EXEC_PORT=$(sg docker -c 'docker inspect --format "{{(index (index .NetworkSettings.Ports \"5000/tcp\") 0).HostPort}}" grok-codex-worker-verify-a')
export VERIFY_EXEC_PORT
sg docker -c './scripts/compose.sh exec -T control python checks/protocol.py --worker grok-codex-worker-verify-a --exec-port "$VERIFY_EXEC_PORT"'
```

Create a local fixture for a repository ref, skill, image, and fake secret. Keep the fixture outside managed data.

```sh
VERIFY_ROOT=$(mktemp -d /tmp/grok-codex-verify.XXXXXX)
export VERIFY_ROOT
python3 checks/fixture_repo.py "$VERIFY_ROOT/fixture"
sg docker -c 'python3 scripts/new-worker.py verify-input --repo file:///run/grok-codex/inputs/fixture.git --ref refs/pull/123/head --inputs-dir "$VERIFY_ROOT/fixture/inputs" --secrets-dir "$VERIFY_ROOT/fixture/secrets"'
sg docker -c './scripts/compose.sh exec -T control python checks/native_inputs.py --worker grok-codex-worker-verify-input --run-models'
```

The fixture secret is a public test string. Do not substitute real credentials. Check the reported model, effort, native IDs, and actual artifacts. Complete the manual acceptance checks below for behavior these scripts do not cover.

## Verify native execution and isolation

1. Create two fresh workers with `--browser-port 3000`. Mount a sentinel input file and fake secret in only one worker.
2. Start a native Codex turn in each worker. Have each turn write and reread a different sentinel in `/work/source`. Confirm `apply_patch`, repository `AGENTS.md`, requested skill input, and input-file reads execute in that worker.
3. Install different dependency versions in separate worker virtual environments or projects. Confirm installation in one worker does not change the other's version.
4. Start servers with different responses on `0.0.0.0:3000` in both workers. Check each response through its distinct loopback `browserPorts` mapping on the cloud computer and your actual browser tool.
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

In the maintainer's run, the control result was partial: steer, queue, cancel, and explicit-worker follow-up passed, while strict immediate process termination failed despite native `interrupted` status. That interrupted command was also absent from native full history. Preserve the actual PID and marker observations. See the [recorded limit](contract.md#what-was-tested).

## Verify history and lists

Create multiple turns with large outputs. Export every native-returned turn and item through the last page. Compare interrupted commands with physical evidence separately: the observed interrupted command's item was absent from native history itself. Test origin-based task lookup, all-task listing, archive, restore, rename, and PR attachment reverse lookup with actual native IDs.

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

## Expanded acceptance probes

Use the five `checks/acceptance_*.py` files from your checkout, and set `VERIFY_TOOLS` to their host directory. The examples feed them into the existing control image through `/dev/stdin`. Probes leave model and reasoning settings unspecified, retaining the deployment defaults, and select the exact registered worker environments. Only launch/control commands below use `--run-models`; inspection, resource sampling, and capture do not launch model work.

Use new case/report paths and idle validation workers. Replace `ENV_1` through `ENV_8` with eight exact environment IDs from worker creation output. In this deployment each ID is also its container name. The tested resource flags are in [what was tested](contract.md#what-was-tested); they did not change defaults.

```sh
cd /workspace/grok-codex-native
export VERIFY_TOOLS=/workspace/grok-codex-native/checks
export VERIFY_CONTROL_DIR=/var/lib/grok-codex/artifacts/acceptance-NEW-CASE
export VERIFY_HOST_DIR=/workspace/grok-codex-validation-NEW-CASE
export VERIFY_WORKERS="ENV_1 ENV_2 ENV_3 ENV_4 ENV_5 ENV_6 ENV_7 ENV_8"
```

### Eight light tasks and host resource samples

Start the bounded resource sampler in a separate Grok Shell before launch. It runs on the host, records the exact named containers and whole-host availability, and writes a `.summary.json` beside the JSONL. Use the actual control name if the project name differs. For the mixed case use its eight selected containers and `--seconds 360` with a new output path.

```sh
sg docker -c 'python3 "$VERIFY_TOOLS/acceptance_resources.py" --containers grok-codex-control-1 $VERIFY_WORKERS --seconds 240 --interval 3 --out "$VERIFY_HOST_DIR/light-resources.jsonl"'
```

The eight-task harness returns accepted native IDs before waiting. `inspect` records observed status; positive native and physical-command interval intersections in `collect` establish actual overlap. A Task subagent can own this finite collect command while the parent ends its response.

```sh
sg docker -c './scripts/compose.sh exec -T control python /dev/stdin launch --workers $VERIFY_WORKERS --origin verify-eight --report "$VERIFY_CONTROL_DIR/light-launch.json" --run-models' < "$VERIFY_TOOLS/acceptance_eight.py"
sg docker -c './scripts/compose.sh exec -T control python /dev/stdin inspect --report "$VERIFY_CONTROL_DIR/light-launch.json" --out "$VERIFY_CONTROL_DIR/light-inspect.json"' < "$VERIFY_TOOLS/acceptance_eight.py"
sg docker -c './scripts/compose.sh exec -T control python /dev/stdin collect --report "$VERIFY_CONTROL_DIR/light-launch.json" --out "$VERIFY_CONTROL_DIR/light-collect.json"' < "$VERIFY_TOOLS/acceptance_eight.py"
```

### One browser/HTTP worker and seven light tasks

Select eight idle environments for this case, with `VERIFY_HEAVY_ENV` naming exactly one of them. Copy the matching `acceptance_eight.py` companion to a new control scratch path, then pass that path before the mixed subcommand. The heavy worker must provide Chromium, as the supplied worker image does.

```sh
export VERIFY_HEAVY_ENV=ENV_1
export VERIFY_CONTROL_BASE=/tmp/acceptance-eight-NEW-CASE.py
sg docker -c './scripts/compose.sh cp "$VERIFY_TOOLS/acceptance_eight.py" "control:$VERIFY_CONTROL_BASE"'
sg docker -c './scripts/compose.sh exec -T control python /dev/stdin --base-path "$VERIFY_CONTROL_BASE" launch --workers $VERIFY_WORKERS --heavy-worker "$VERIFY_HEAVY_ENV" --origin verify-mixed --report "$VERIFY_CONTROL_DIR/mixed-launch.json" --run-models' < "$VERIFY_TOOLS/acceptance_mixed.py"
sg docker -c './scripts/compose.sh exec -T control python /dev/stdin --base-path "$VERIFY_CONTROL_BASE" inspect --report "$VERIFY_CONTROL_DIR/mixed-launch.json" --out "$VERIFY_CONTROL_DIR/mixed-inspect.json"' < "$VERIFY_TOOLS/acceptance_mixed.py"
sg docker -c './scripts/compose.sh exec -T control python /dev/stdin --base-path "$VERIFY_CONTROL_BASE" collect --report "$VERIFY_CONTROL_DIR/mixed-launch.json" --out "$VERIFY_CONTROL_DIR/mixed-collect.json"' < "$VERIFY_TOOLS/acceptance_mixed.py"
```

To rerun collection, run only `collect` against the same saved launch and turns with a new output path. Preserve the first result and original resource samples.

### Control and terminal-thread lifecycle

Read the exact selected worker's safe hostname metadata and pass it to the control probe. This runs steer, queue/cancel, interrupt and replacement with the deployment defaults.

```sh
export VERIFY_SINGLE_ENV=ENV_2
VERIFY_EXPECTED_HOSTNAME=$(sg docker -c 'docker inspect --format "{{.Config.Hostname}}" "$VERIFY_SINGLE_ENV"')
export VERIFY_EXPECTED_HOSTNAME
sg docker -c './scripts/compose.sh exec -T control python /dev/stdin --worker "$VERIFY_SINGLE_ENV" --expected-hostname "$VERIFY_EXPECTED_HOSTNAME" --origin verify-control --report-dir "$VERIFY_CONTROL_DIR/control" --run-models' < "$VERIFY_TOOLS/acceptance_control.py"
```

**The strict immediate-stop check failed in the maintainer's run.** A later physical check must identify the PID from `interrupt-started.json` in that exact worker and compare `/proc/PID/stat` and completion markers; native `interrupted` or a missing command-history item does not prove the process stopped. Later PID absence cannot establish its exact termination time. The probe can stop before its lifecycle block; keep its failed report.

For the separate model-free lifecycle procedure, first record the terminal turn IDs, known PID absence, queue and goal. Apply native name-set/archive, match the exact ID/name in archived listing, unarchive and match live listing plus thread-read, then compare full history and queue/goal with the baseline. The existing lifecycle block in `acceptance_control.py` defines those calls. This procedure does not rewrite a failed immediate-stop result or create a new turn.

### Active-recreation and natural-idle clock probe

The clock probe launches one foreground 600-second task with two-second ticks. `capture` is read-only: thread/history, all queue pages, goal, and artifact metadata/timeline gaps; it does not resume a thread or classify the result automatically.

```sh
sg docker -c './scripts/compose.sh exec -T control python /dev/stdin launch --worker "$VERIFY_SINGLE_ENV" --origin verify-clock --duration 600 --interval 2 --report "$VERIFY_CONTROL_DIR/clock-launch.json" --run-models' < "$VERIFY_TOOLS/acceptance_recreation.py"
sg docker -c './scripts/compose.sh exec -T control python /dev/stdin capture --report "$VERIFY_CONTROL_DIR/clock-launch.json" --out "$VERIFY_CONTROL_DIR/clock-before.json"' < "$VERIFY_TOOLS/acceptance_recreation.py"
```

For natural idle, end the parent response after recording real launch/start evidence. Leave **no Task waiter, handoff/collect process, sampler, keepalive, routine, or open computer preview** for this case. After your chosen idle window, perform one capture with a fresh output. Parent idle alone does not establish computer sleep; compare actual clock gaps, boot identity, native state, and artifacts.

For an active-computer recreation case, use a separate launch/report with a longer duration, such as `--duration 1200 --interval 2`. The probe itself performs no update/reset. Trigger Update or Reset yourself under **Settings → Computer**, and record its actual effect and boot/PID 1 identity. If runtime is absent afterward, rebuild control through the installer, then capture **before** worker restoration/registration, thread resume, waiter attachment, or queue changes. `collect` may resume through handoff, so it is not the first post-recreation observation.

```sh
sg docker -c './scripts/compose.sh exec -T control python /dev/stdin capture --report "$VERIFY_CONTROL_DIR/clock-launch.json" --out "$VERIFY_CONTROL_DIR/clock-after.json"' < "$VERIFY_TOOLS/acceptance_recreation.py"
```

Keep a launch report and observations for each distinct case. Recorded results for sleep and Reset are in [what was tested](contract.md#what-was-tested). Manual operational resumption and automatic continuation of the original process are different results.
