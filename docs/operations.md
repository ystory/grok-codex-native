# Operate and remove the deployment

Run deployment and operation commands through Shell on Grok Bot's account-shared cloud computer. The project, control container, and Docker workers all reside on that computer.

The [installer](../scripts/install-grok-cloud.py) prepares Docker and retains the existing deployment UID/GID when running `prepare.sh`. Use `sg docker -c 'COMMAND'` for host Docker commands so existing Grok Shell sessions gain Docker access. Rerun the installer for later preparation. Shell-quote actual argument values inside the command.

Testing did not change the default resource caps. The eight-task profiles and their measured peaks are in [what was tested](contract.md#what-was-tested). They do not establish capacity for arbitrary tasks or eight browser workers.

## Build and verify a release archive

Review and stage or commit the project files before packaging. The script rejects unstaged changes to tracked project files. This step creates a local archive without publishing or deploying it. `--out` names a private output directory.

```sh
python3 scripts/build-release.py --out /absolute/private-release-dir
```

For package version `0.1.0`, the outputs are `grok-codex-native-0.1.0.tar.gz` and its `.tar.gz.sha256` file. The script reads the current contents of Git-tracked files. Untracked files are omitted, so review the file list and diff first. Private `deploy/.env`, credentials, caches, and `.local` data do not belong in the archive.

Tagged versions publish the same archive and checksum through the [release workflow](../.github/workflows/release.yml). Verify each archive using its companion checksum and internal manifest.

On Grok Bot's cloud computer, verify the archive checksum, extract into a new directory, and verify the internal manifest.

```sh
cd /absolute/private-release-dir
sha256sum --check grok-codex-native-0.1.0.tar.gz.sha256
mkdir /absolute/new-extract-dir
tar -xzf grok-codex-native-0.1.0.tar.gz -C /absolute/new-extract-dir
cd /absolute/new-extract-dir/grok-codex-native
sha256sum --check MANIFEST.sha256
```

Place the extracted project at `/workspace/grok-codex-native` on that cloud computer and keep private state separately at `/workspace/grok-codex-data`. Run the [README installation steps](../README.md#install) there. Checksum verification proves file integrity, not installation or execution.

## Register the shared skill

First verify installation, login, and a representative task on your cloud computer. Check activation for each additional Bot.

The full skill source is [skill/grok-codex-native/SKILL.md](../skill/grok-codex-native/SKILL.md). Keeping the file on disk does not register it in Grok.

1. Inspect Grok's current skill list for a `grok-codex-native` name collision or an existing skill with the same role.
2. Review the complete skill and record the installed project's actual absolute path on that cloud computer. The recommended path is `/workspace/grok-codex-native`.
3. Save the full skill through Grok's supported UI or currently exposed `save_skill` tool. Follow the current tool schema for name, description, and body.
4. In **Connect apps**, check that the skill is enabled for the target Bot under **Private skills**.
5. Run a new task and verify skill discovery, execution on that cloud computer, native IDs, Task completion wakeup, and actual file attachments.

The official [Grok skills documentation](https://docs.x.ai/grok-bot/skills-routines-and-automations) describes the shared private skill library and per-Bot activation. This project does not change global instructions, memory, or routing. Any separately authorized preference does not guarantee tool access or invocation by every Bot.

## Inspect status and logs

```sh
sg docker -c './scripts/compose.sh ps'
sg docker -c './scripts/compose.sh logs --tail 100 control'
sg docker -c './scripts/healthcheck.sh'
sg docker -c 'docker ps --filter label=io.grok-codex.role=worker'
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
sg docker -c './scripts/compose.sh up -d --wait --force-recreate control'
sg docker -c 'python3 scripts/register-worker.py NAME'
sg docker -c './scripts/healthcheck.sh'
```

To reuse a stopped worker whose container and dedicated network still exist, start it through the network helper. Use the actual project, network, and container names from creation output; this example uses the default project name.

```sh
sudo -n python3 scripts/worker-network.py start --project grok-codex --network grok-codex-worker-NAME-net --container grok-codex-worker-NAME
sg docker -c 'python3 scripts/register-worker.py NAME'
```

The helper validates the stopped worker, applies its network protections, and starts it under one lock. It refuses older workers that lack the required IPv6 isolation; preserve their source and inspect them before explicitly stopping or replacing them with the current worker script. A registration failure does not mean worker creation failed. Check accepted creation output and actual Docker state before creating another worker.

The native thread store persists. Automatic recovery of active turns and remote connections is not guaranteed. Read the thread, queue, and goal before deciding what to do next. Before resuming an unloaded thread or starting a waiter that may resume it, inspect and explicitly cancel leftover queue items. Send intended input through `turn/start.environments` with the worker selected. `thread/resume` cannot select the worker.

If a Grok Task or Shell waiter ended, attach a new finite waiter with the last observed turn ID after checking the queue. Re-registration alone does not prove task recovery.

Native `interrupted` status does not establish immediate termination of a worker command. The client forwards native interruption and has no separate process-kill bridge. Keep native state and physical process evidence distinct when reporting stopped work. See the [recorded limit](contract.md#what-was-tested).

## Recover after hosted-computer replacement

Grok's [file guidance](https://docs.x.ai/grok-bot/computer-and-apps#work-with-files) recommends `/workspace` for durable project files. Hosted-computer updates, recovery, or reset can replace manually installed packages and dependency environments. Keep project source and the private data directory there: native configuration, history and login, worker source, artifacts, and the specifications needed to recreate workers. Rebuild Docker Engine, images, containers, and dependencies from code.

Sleep pauses work rather than replacing the computer. In testing, the same process continued after a manual Wake. A full-computer Reset removed the running process instead, and an explicit new turn was needed to continue. See [what was tested](contract.md#what-was-tested).

Both differ from restarting only the control container. Update Bot's Computer changed the boot ID and removed Docker and containers. Reinstallation and restoration then passed using the preserved `/workspace` data.

Inspect the preserved project path and `deploy/.env` before rebuilding. Running `python3 scripts/install-grok-cloud.py` without `--data` reuses an existing deployment's recorded data path; a fresh installation defaults to `/workspace/grok-codex-data`. It does not move an existing directory automatically.

Keep the private data directory's `deployment.json` with the native store. This ordinary user configuration records the schema version, project name, UID, and GID. Existing `deploy/.env` takes precedence and must match it. A fresh or imported deployment reuses its saved project and GID only when the saved UID matches the current user. Identity mismatches and worker manifests without known deployment identity are refused rather than inferred. This file is separate from the replaceable root-owned bootstrap receipt.

For older private data outside `/workspace`, use an explicit cold import before rebuilding Docker. The command requires a fresh checkout below `/workspace` with no `deploy/.env`, no Docker socket, and an absent target below `/workspace` outside the checkout and original source.

```sh
cd /workspace/grok-codex-native
python3 scripts/install-grok-cloud.py --import-data /absolute/old-private-data --data /workspace/grok-codex-data
```

The original directory is preserved. Import retains ordinary repository symbolic links as links, while refusing symbolic links in managed top-level data and configuration paths or in source/target parent paths. It recreates the replaceable `control-run` directory empty and refuses other special files. If copying fails, the private partial target remains for inspection; it is not overwritten on a retry.

After control is available, first record the original native thread/history/queue/goal and progress files **before** worker restoration, environment registration, thread resume, or waiter attachment. The [read-only capture probe](verification.md#active-recreation-and-natural-idle-clock-probe) preserves that observation order. New workers save a private launch manifest at `DATA/manifests/WORKER.json`. After this first observation and the authenticated health check, restore each explicitly selected worker by its original short name:

```sh
sg docker -c 'python3 scripts/restore-worker.py NAME'
```

Restoration validates the manifest's project, worker identity, UID/GID, resources, image, and input paths. It derives managed source, artifact, and runtime paths from the current deployment data root, so imported data uses its new location. External environment files, inputs, and secrets must still exist at their recorded paths. The managed worker image can be rebuilt; a changed image ID is reported. A custom worker image requires its original immutable image ID.

The script recreates missing network/container definitions and validates existing ones. The network helper applies protections before starting a stopped worker under the same lock; a matching running worker stays running. The script then re-registers the environment. It reuses source without cloning or resetting it. Foreign or changed definitions, missing manifests, and identity mismatches are preserved and refused. Legacy data without a manifest requires separate investigation.

Then read native thread, queue, and goal state before starting an explicit follow-up turn with its worker selected. Restoration does not resume an interrupted native turn automatically.

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
sg docker -c 'docker stop grok-codex-worker-NAME'
sg docker -c './scripts/remove-worker.sh NAME'
```

`remove-worker.sh` rejects running, paused, or restarting containers. It removes a stopped worker with the required management labels and its dedicated Docker network. Source, artifact, and runtime directories remain.

The native protocol has no `environment/remove` method here. Leave stale registration until the next planned control restart. Do not disrupt other tasks just to clear registration.

## Remove control and installation files

After cleaning up workers and waiters, stop control.

```sh
sg docker -c './scripts/compose.sh down'
```

Remove dynamically created workers separately. This command does not delete host data, source, or GitHub objects. Once no containers use them, you can remove `grok-codex-control:0.166.0` and `grok-codex-worker:0.166.0` through Docker. Use actual image names for a custom project name.

Delete the reviewed checkout and private credential, native-history, and artifact directories only when complete removal is requested. Preserve source `workspaces/` separately. This guide provides no bulk deletion command for the entire data root.

Disable or delete the Grok skill through the current UI and check the effect on other Bots. External GitHub objects and file copies already sent to conversations are outside this removal procedure.
