# Operate and remove the deployment

## Build and verify a release archive

Review and stage or commit the project files before packaging. The script rejects unstaged changes to tracked project files. This step creates a local archive without publishing or deploying it. `--out` names a private output directory.

```sh
python3 scripts/build-release.py --out /absolute/private-release-dir
```

For package version `0.1.0`, the outputs are `grok-codex-native-0.1.0.tar.gz` and its `.tar.gz.sha256` file. The script reads the current contents of Git-tracked files. Untracked files are omitted, so review the file list and diff first. Private `deploy/.env`, credentials, caches, and `.local` data do not belong in the archive.

On the receiving computer, verify the archive checksum, extract into a new directory, and verify the internal manifest.

```sh
cd /absolute/private-release-dir
sha256sum --check grok-codex-native-0.1.0.tar.gz.sha256
mkdir /absolute/new-extract-dir
tar -xzf grok-codex-native-0.1.0.tar.gz -C /absolute/new-extract-dir
cd /absolute/new-extract-dir/codex-native
sha256sum --check MANIFEST.sha256
```

You can move the extracted directory to `~/code/grok-codex-native` or another absolute path. Run the [README installation steps](../README.md#install-and-log-in) on the Linux VM. Checksum verification proves file integrity, not installation or execution.

## Register the shared skill

First verify installation, login, and a representative task on the target VM. The [maintainer report](verification-results.md) covers a separate backend verification environment and a Grok Task observation. It does not establish production installation, registration, or complete delivery.

The full skill source is [skill/grok-codex-native/SKILL.md](../skill/grok-codex-native/SKILL.md). Keeping the file on disk does not register it in Grok.

1. Inspect Grok's current skill list for a `grok-codex-native` name collision or an existing skill with the same role.
2. Review the complete skill and record the installed project's actual absolute path and VM. The suggested standalone path is `~/code/grok-codex-native`.
3. Save the full skill through Grok's supported UI or currently exposed `save_skill` tool. Follow the current tool schema for name, description, and body.
4. In **Connect apps**, check that the skill is enabled for the target Bot under **Private skills**.
5. Run a new task and verify skill discovery, execution on the correct VM, native IDs, Task completion wakeup, and actual file attachments.

The official [Grok skills documentation](https://docs.x.ai/grok-bot/skills-routines-and-automations) describes the shared private skill library and per-Bot activation. This project does not change global instructions, memory, or routing. Any separately authorized preference does not guarantee tool access or invocation by every Bot.

## Inspect status and logs

```sh
./scripts/compose.sh ps
./scripts/compose.sh logs --tail 100 control
./scripts/healthcheck.sh
docker ps --filter label=io.grok-codex.role=worker
```

Check each worker's native connection from inside control.

```python
from pydantic import RootModel
from grok_codex_native.client import connect

with connect() as c:
	c.initialize()
	print(c.request("environment/status", {
		"environmentId": "grok-codex-worker-NAME",
	}, response_model=RootModel[dict]).model_dump_json(by_alias=True))
```

`environment/status` does not create or repair a connection. For a disconnected environment, inspect its container, loopback port, registration, and authentication. The raw exec token is stored in the host data directory at `runtime/WORKER/exec-token` with mode `0600`. Only its digest appears in worker arguments. Keep the token out of logs, chat, and Git.

## Re-register environments after restart

Control stores environment registrations in server memory. After restarting it, register every running worker again under the same ID.

```sh
./scripts/compose.sh up -d --wait --force-recreate control
python3 scripts/register-worker.py NAME
./scripts/healthcheck.sh
```

To reuse a stopped worker, run `docker start grok-codex-worker-NAME` first. A registration failure does not mean worker creation failed. Check accepted creation output and actual Docker state before creating another worker.

The native thread store persists. Automatic recovery of active turns and remote connections is not guaranteed. Read the thread, queue, and goal before deciding what to do next. Before resuming an unloaded thread or starting a waiter that may resume it, inspect and explicitly cancel leftover queue items. Send intended input through `turn/start.environments` with the worker selected. `thread/resume` cannot select the worker.

If a Grok Task or Shell waiter ended, attach a new finite waiter with the last observed turn ID after checking the queue. Re-registration alone does not prove task recovery.

## Upgrade pinned versions

1. Inspect active tasks, native queues, goals, and Grok waiters.
2. Explicitly stop affected work and confirm terminal state.
3. Stop control and back up the private data directory. Preserve SQLite data, rollouts, credentials, and artifacts together.
4. Review official CLI and SDK releases and native protocol changes. Update Dockerfile, lockfiles, and documentation together.
5. Build the images, start control, and re-register environments.
6. Run the [verification procedures](verification.md), including login, execution, control, full history, and Grok attachments.

The current pins are CLI `0.166.0`, SDK `0.163.0`, and websocat `4.0.0-alpha3`. This project does not guarantee live-backup consistency or native store compatibility across versions. Credentials can expire. After restoration, verify login and actual thread reads.

## Remove a worker

Stop native execution first. If permanent history deletion is requested, perform the separate [purge procedure](native-use.md#stop-archive-rename-or-delete-a-task). Confirm execution has stopped before removing the worker.

```sh
docker stop grok-codex-worker-NAME
./scripts/remove-worker.sh NAME
```

`remove-worker.sh` rejects running, paused, or restarting containers. It removes a stopped worker with the required management labels and its dedicated Docker network. Source, artifact, and runtime directories remain.

The native protocol has no `environment/remove` method here. Leave stale registration until the next planned control restart. Do not disrupt other tasks just to clear registration.

## Remove control and installation files

After cleaning up workers and waiters, stop control.

```sh
./scripts/compose.sh down
```

Remove dynamically created workers separately. This command does not delete host data, source, or GitHub objects. Once no containers use them, you can remove `grok-codex-control:0.166.0` and `grok-codex-worker:0.166.0` through Docker. Use actual image names for a custom project name.

Delete the reviewed checkout and private credential, native-history, and artifact directories only when complete removal is requested. Preserve source `workspaces/` separately. This guide provides no bulk deletion command for the entire data root.

Disable or delete the Grok skill through the current UI and check the effect on other Bots. External GitHub objects and file copies already sent to conversations are outside this removal procedure.
