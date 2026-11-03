---
name: grok-codex-native
description: Delegate coding tasks from Grok Bot to native Codex on Grok Bot's own cloud computer, collect completion and files, and manage follow-up instructions, history, PR lookup, watch, and task lifecycle.
---

# Use native Codex from Grok

Use the installed project to execute the user's coding task on Grok Bot's account-shared cloud computer. Run commands through Shell on that computer, where both control and Docker workers are installed. Preserve the task's purpose and authorization scope. Grok decides the task and next action. Native Codex owns sessions, history, queued input, and errors.

## Locate the installation

Read the installation record for the actual absolute project path on Grok Bot's cloud computer. The recommended location is `/workspace/grok-codex-native`, with private data in `/workspace/grok-codex-data`. Set `PROJECT_DIR` to the recorded installation.

```sh
PROJECT_DIR=/absolute/path/to/grok-codex-native
cd "$PROJECT_DIR"
sg docker -c './scripts/healthcheck.sh'
```

If the project or its runtime is absent, follow the project's README and run `python3 scripts/install-grok-cloud.py` as the normal cloud user. Complete the user's ChatGPT login before the authenticated health check. File presence does not establish installation, registration, or execution. Save the full skill through the supported Grok UI or currently available `save_skill`, then verify activation under Private skills for the target Bot. Check existing names first because Bots share the account's private skill library.

Run host Docker commands through `sg docker -c 'COMMAND'`, including `new-worker.py`, `compose.sh`, and `healthcheck.sh`, so existing Grok Shell sessions gain Docker group access. Rerun the installer for preparation so it retains the deployment UID/GID. Shell-quote actual argument values inside the command and paths used for outer redirections.

If a hosted-computer update or recovery removed Docker, containers, images, or dependency environments, read `docs/operations.md` under "Recover after hosted-computer replacement". Rebuild control from the preserved `/workspace` project and private data, then read the original thread, queue, goal, and progress before restoring workers, registering environments, attaching a waiter, or resuming. In the verified full Reset, the running command ended interrupted and its prior progress remained. For an existing recorded worker, use `sg docker -c 'python3 scripts/restore-worker.py NAME'`; this requires its saved launch manifest and restores its environment definition rather than the original process. Legacy data without a manifest is preserved for investigation. Continue intended work only through an explicit follow-up with the worker selected.

## Start a task

1. Identify the task, repository and ref, and required input files and secret names on that cloud computer. Set a model, settings, or repository skill only when explicitly requested. Inspect available choices through the resource procedures in project file `docs/native-use.md`.
2. Use a fresh worker name. Create it with `sg docker -c 'python3 scripts/new-worker.py NAME --repo URL --ref REF'`. Omit `--repo` for a local repository without publication. Follow the input table in `docs/native-use.md` for images, files, and secrets.
3. Keep worker and environment IDs and source and artifact paths from creation output. If a worker was accepted before an error, inspect its actual Docker state before creating another one.
4. Copy `examples/brief.json` and fill `worker`, `origin`, `title`, native `thread`, and `input`. Use a stable calling Bot or conversation identity for `origin`. Select exactly that worker environment. Keep `include_local = false`.
5. Start with the following command. Replace `BRIEF` with the actual JSON file. Keep accepted native IDs, the effective model, and the artifact path in the original conversation.

```sh
sg docker -c './scripts/compose.sh exec -T control python examples/launch_and_wait.py --no-wait /dev/stdin' < BRIEF
```

6. Give an existing Task subagent the finite waiter below with the actual returned values. The subagent uses current Shell and wait tools until process exit, then returns native result JSON and file paths.

```sh
sg docker -c './scripts/compose.sh exec -T control python -m grok_codex_native.handoff --thread THREAD_ID --turn TURN_ID --artifacts /var/lib/grok-codex/artifacts/WORKER/THREAD_ID'
```

Keep Task and subagent Shell terminal IDs in the original conversation too. To start and wait in one process, omit `--no-wait`. Preserve native errors and `willRetry`. Report rejection and decide the next step within the user's request. Add no separate launch queue, external automatic retry, or model fallback.

## Return completion and files

After assigning Task, the parent Bot can end its response. When Task completion wakes the parent, read terminal state, errors, full history, and actual file paths. Worker deliverables belong in `/work/artifacts/THREAD_ID/`. Control sees them at `/var/lib/grok-codex/artifacts/WORKER/THREAD_ID/`. Read host artifact paths from worker creation output.

Report results in the original conversation and deliver files through the actual attachment fields in the current `SendMessage` schema. Map control paths to host paths on that same cloud computer. A path in message text does not establish attachment.

The verified installation delivered consecutive native completions and actual files through finite Task waiters. Verify this flow for each installation. Actual cloud-computer sleep paused a foreground Codex command; waking the computer continued the same process and turn. Report paused time separately from execution time. Read native state, queue, goal, and progress before attaching a waiter or resuming after wake or runtime loss. See `docs/contract.md` for the observed boundaries. Shell-only background wakeup and maximum wait duration remain unverified. Return native IDs, history files, artifacts, and actual PR URLs. Create no hosted task page or unverified Codex deep link.

## Send follow-ups and observe each turn

Read only the needed procedures in `docs/native-use.md` for control, history, lists, PRs, and artifacts. Connect through `grok_codex_native.client.connect()` to retain the explicit native decline handler. Do not resume for read, steer, or interrupt.

- Steer with `turn_steer` and the actual current turn ID.
- Queue through native `thread/queue/add` only when `thread/read` reports `active`. Otherwise start a turn with an explicit environment.
- Before resuming an unloaded thread, inspect and explicitly cancel leftover queued input, then send intended input through a fresh turn with the worker selected. A handoff waiter can resume when no terminal turn exists, so apply the same check before attaching it to an unloaded thread.
- To interrupt and replace input, record the native terminal result and verify the task's physical worker execution has stopped before conflicting replacement work or file deletion. A real worker process survived the native `interrupted` result, and its running command was absent from full native history. Use known task PID and workload evidence; if physical stop is unverified, preserve files and report that uncertainty. See `docs/contract.md` under "What was tested". Resume first only if the thread is unloaded. Set the worker in fresh `turn/start.environments`, including after server restart. `thread/resume` has no such field.
- For each follow-up turn or requested continuous watch, assign a new finite waiter to an existing Task subagent. One waiter returns once.

For another Bot's thread, record the observer in a native `grok.watch` attachment. To wait after the last reported completion, use `--after LAST_TERMINAL_TURN_ID` instead of `--turn`. After reporting the next completion, reattach with the returned `after` value if continuous watch was requested. If no terminal turn exists, omit both options after the unloaded-thread queue check.

Start observer waiters with the PID-reporting command in `docs/native-use.md`. Record the real PID inside control. To unwatch, verify and terminate that PID inside control, then clean up its Shell, observer Task, and `grok.watch` attachment. Killing only the outer Docker exec client can leave the waiter alive. Unwatch before archiving or deleting the target. A separate stop request controls native execution, queues, and goals.

## Manage lifecycle and deletion

For a stop request, inspect the current turn and any native queue or goal within the requested scope. Use native archive, restore, and rename methods. Archive is not stop. Read every full-history turn and item page when exporting history.

Before permanent deletion, confirm the exact native thread UUID and the managed history and artifact scope. Inspect `grok_codex_native.purge --dry-run` first. The artifact root must exactly match trusted `GROK_CODEX_ARTIFACTS_ROOT`. Artifact attachments must identify the exact thread UUID and `root/worker/thread-UUID` path.

Purge refuses runnable threads and descendants, queued input, runnable goals, and unknown state. Stop requested execution separately. Purge itself does not stop it. Preflight and deletion are not atomic against new input, so do not submit new input during the check. Preserve source files, uncommitted work, external GitHub objects, and previously delivered copies. Report native deletion uncertainty and partial artifact deletion from actual output.

The model login uses control's ChatGPT subscription. Do not mount its credentials or the Docker socket in workers. Keep approval policy `never`, explicit native decline responses, and `include_local = false`. There is no cross-conversation interactive approval relay. Docker workers use native `externalSandbox` and separate filesystems, PIDs, and networks while sharing the host kernel. Do not widen permissions automatically after execution failure.

Read `docs/contract.md` for experimental native environments, `deferred_executor`, and prerelease websocat limits. Read `docs/operations.md` for restart, upgrade, and removal. Re-register environments after control restarts. Judge installation results through `docs/verification.md`. Registering this skill does not change global instructions, memory, or routing preferences.
