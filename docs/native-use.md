# Control native Codex tasks

## Run SDK calls

Run Python examples inside control. To send a file from the host, use this command.

```sh
./scripts/compose.sh exec -T control python /dev/stdin < /tmp/control.py
```

Place the calls below inside this `with` block. Replace `THREAD_ID` and `TURN_ID` with actual native IDs. `connect()` configures the official SDK with an explicit native decline handler. Keep that handler and the `never` approval policy.

```python
import json
from pathlib import Path
from uuid import uuid4
from pydantic import RootModel
from grok_codex_native.client import connect, require_chatgpt

with connect() as c:
	c.initialize()
	require_chatgpt(c)
	print(c.thread_read("THREAD_ID").model_dump_json(by_alias=True))
```

Read, steer, and interrupt without `thread/resume`. Resume can replay pending server requests. A completion waiter resumes when needed to subscribe. Before resuming an unloaded thread, inspect and cancel any queued input as described below. Apply that check before starting a waiter too.

For a fresh turn on an unloaded thread, resume first, then set the worker in `turn/start.environments`. `thread/resume` has no `environments` field. Do not assume the worker selection survives a server restart. Keep `include_local = false` and use only prepared Docker workers with `sandboxPolicy={"type":"externalSandbox","networkAccess":"enabled"}`. Docker provides execution isolation. There is no cross-conversation approval relay or model API billing fallback.

## Supply a task and inputs

The [launch example](../examples/launch_and_wait.py) takes native `thread/start` parameters in `brief.thread` and native input items in `brief.input`.

Set a model, settings, or repository skill only when explicitly requested. Check a requested model with `c.model_list()`, then set native thread parameter `model`. Set a requested reasoning effort through `config.model_reasoning_effort`. Keep `approvalPolicy: "never"`. The launch example applies native `externalSandbox` to the first turn. Inspect the effective values in the start response and preserve native errors for rejected options.

| Input | Method | Check |
| --- | --- | --- |
| Task | `{"type":"text","text":"Implement the requested change."}` | Include completion criteria and the artifact path. |
| Repository and ref | `new-worker.py --repo URL --ref REF` | Confirm the actual clone and checkout. |
| Title | `brief.title` or `thread_set_name` | Read the native thread name. |
| Repository files | Cloned files in `/work/source` | Confirm reads inside the worker. |
| Extra files | `new-worker.py --inputs-dir /absolute/private-inputs` | Files appear read-only at `/run/grok-codex/inputs`. |
| Image | A real data URL in native `image` input | Confirm the model reads the content. If using `localImage`, verify remote path resolution. |
| Secret | `--secrets-dir /absolute/private-secrets` or `--env-file /absolute/private.env` | Files appear in `/run/secrets`, or values appear in worker environment variables. Put only names and purposes in the task. |
| Requested repository skill | `{"type":"skill","name":"example-skill","path":"/work/source/.agents/skills/example-skill/SKILL.md"}` | Confirm worker discovery, content reads, and application. |

To add a PNG to a copied brief, run this Python on the host. Use the real file path and MIME type.

```python
import base64
import json
from pathlib import Path

brief_path = Path("/tmp/grok-codex-brief.json")
brief = json.loads(brief_path.read_text())
encoded = base64.b64encode(Path("/absolute/private-inputs/screenshot.png").read_bytes()).decode("ascii")
brief["input"].append({"type": "image", "url": "data:image/png;base64," + encoded})
brief_path.write_text(json.dumps(brief))
brief_path.chmod(0o600)
```

For private GitHub access, configure existing GitHub CLI credentials in a private `--env-file`. Supply `GH_TOKEN` through your approved credential path. Keep its value out of chat, logs, and Git. Git uses the worker's `gh` credential helper.

```text
GH_TOKEN=<repository-scoped-token>
GIT_CONFIG_COUNT=1
GIT_CONFIG_KEY_0=credential.https://github.com.helper
GIT_CONFIG_VALUE_0=!gh auth git-credential
```

Pass the same file to `new-worker.py` for clone and later `gh pr create` access. Existing authorization determines whether you may create tokens or log in.

`--env-file` rejects model login keys and changes to `HOME` or `CODEX_HOME`. Workers do not mount the control ChatGPT credential directory or Docker socket. Input mounts are fixed at worker creation. The project does not update mounts during execution.

## Start without a remote repository

`new-worker.py NAME` initializes a local Git repository without publishing it. If the task requires a temporary private remote, use existing GitHub tools or `gh` inside the worker after authentication.

```sh
cd /work/source
gh repo create OWNER/TEMPORARY-NAME --private --source . --remote origin
```

A first push requires a commit. To move to the permanent repository, use a GitHub rename or an authorized new repository and `git remote set-url`. Preserve source files and uncommitted changes, then verify the actual remote and visibility. No automatic repository publication or visibility change occurs.

## Inspect status and changes

Read status with `c.thread_read("THREAD_ID")`. Read detailed turn state and errors from full history. Preserve native `message`, `codexErrorInfo`, `additionalDetails`, and `willRetry`. After a connection failure, keep accepted native IDs and recheck whether execution continues.

Use Git inside the selected worker to inspect the branch, working tree, and changes.

```sh
docker exec grok-codex-worker-NAME git -C /work/source status --short --branch
docker exec grok-codex-worker-NAME git -C /work/source diff --stat
docker exec grok-codex-worker-NAME git -C /work/source diff --binary
```

Use existing GitHub tools, `gh pr view`, or `gh pr checks` for PR, CI, and review state. Do not infer a PR URL from the thread name or completion message.

## Send instructions during or after a turn

To steer the current turn, use its actual ID. If `expected_turn_id` no longer matches, preserve the native error and read the status again.

```python
c.turn_steer("THREAD_ID", "TURN_ID", [{"type": "text", "text": "Add the requested validation."}])
```

Read `thread/read` first. Use the native queue only when its status is `active`. Otherwise start a fresh turn with an explicit worker environment.

Before resuming a cold thread, inspect `thread/queue/list`. Explicitly cancel leftover items and send the intended input through a new turn with the worker selected. Cold resume can dispatch queued input without a worker. The same precaution applies before attaching a handoff waiter to an unloaded thread, because a waiter can resume it when no terminal turn exists.

Keep the returned `clientUserMessageId` with the result.

```python
queued = c.request("thread/queue/add", {
	"threadId": "THREAD_ID",
	"clientUserMessageId": str(uuid4()),
	"input": [{"type": "text", "text": "Run the requested checks after the current task."}],
}, response_model=RootModel[dict]).root
print(json.dumps(queued))
```

The native queue dispatches input when supported execution state becomes idle. Report queue-unavailable errors unchanged. The project adds no launch queue or external retry. Inspect with `thread/queue/list` and explicitly start a selected item with `thread/queue/start`.

To interrupt and replace the current input, call `c.turn_interrupt("THREAD_ID", "TURN_ID")`. Confirm `interrupted` in the waiter result or full history. For an unloaded thread, resume before starting the replacement turn. Skip resume if the thread is already loaded. Perform the cold-queue check first.

```python
c.thread_resume("THREAD_ID", {"excludeTurns": True})
turn = c.turn_start("THREAD_ID", [{"type": "text", "text": "Implement the revised request."}], {
	"environments": [{
		"environmentId": "WORKER", "cwd": "/work/source",
		"runtimeWorkspaceRoots": ["/work/source", "/work/artifacts"],
	}],
	"sandboxPolicy": {"type": "externalSandbox", "networkAccess": "enabled"},
})
print(turn.model_dump_json(by_alias=True))
```

Start a new completion observation for each new turn ID.

## Deliver completion and files to Grok

Wait for one turn with its actual IDs.

```sh
./scripts/compose.sh exec -T control python -m grok_codex_native.handoff --thread THREAD_ID --turn TURN_ID --artifacts /var/lib/grok-codex/artifacts/WORKER/THREAD_ID
```

The waiter returns after completion, failure, or interruption with full item records. `artifacts` contains absolute paths to regular files. Symlinks and special files are rejected. Worker path `/work/artifacts/THREAD_ID/`, control path `/var/lib/grok-codex/artifacts/WORKER/THREAD_ID/`, and host path `GROK_CODEX_DATA/artifacts/WORKER/THREAD_ID/` refer to the same bind mount.

Give this finite wait to Grok's existing `Task` subagent. The subagent uses current Shell and wait tools until the process exits, then returns JSON and file paths. Keep its Task ID and Shell terminal ID in the original conversation. When Task completion wakes the parent Bot, read the result and attach files using the current `SendMessage` attachment fields.

Map control paths to host paths, or copy files to the computer Grok can read. A path in message text does not prove attachment. In the maintainer's testing, delayed Task completion woke the parent. Native Codex execution through actual Grok file delivery remains an installation-specific acceptance check. Shell background execution alone has no verified wakeup behavior.

## Watch another task and unwatch

Record the observer identity in the target thread's native `grok.watch` attachment. The attachment stores the relationship only.

```python
c.request("thread/attachment/add", {
	"threadId": "THREAD_ID", "attachmentType": "grok.watch",
	"identityKey": "WATCHER_BOT_OR_CONVERSATION", "payload": {},
}, response_model=RootModel[dict])
```

Find the last terminal turn ID in full history. Start an existing Grok Task to run and wait for the next completion.

```sh
./scripts/compose.sh exec -T control python -m grok_codex_native.handoff --thread THREAD_ID --after LAST_TERMINAL_TURN_ID --artifacts /var/lib/grok-codex/artifacts/WORKER/THREAD_ID
```

If no terminal turn exists, omit `--after` and perform the unloaded-thread queue check before starting the waiter. For continuous watch requested by the user, start a new waiter after each report with the returned `after` ID. Each waiter returns once. Verify Shell wait limits, Task lifetime, wakeup, and restart behavior in the actual installation. Automatic watch recovery after restart is not guaranteed.

Record the waiter's PID inside control. This command writes the PID to stderr and result JSON to stdout.

```sh
./scripts/compose.sh exec -T control sh -c 'echo "HANDOFF_PID=$$" >&2; exec python -m grok_codex_native.handoff "$@"' sh --thread THREAD_ID --after LAST_TERMINAL_TURN_ID --artifacts /var/lib/grok-codex/artifacts/WORKER/THREAD_ID
```

To unwatch, verify the recorded observer PID and terminate it inside control. Killing only the outer Docker exec client can leave the waiter running.

```sh
./scripts/compose.sh exec -T control sh -c 'kill -TERM "$1"' sh OBSERVER_PID
```

Then clean up the original Shell and observer Task and remove the attachment. Unwatch before archiving or deleting the target thread. The waiter does not return archive or deletion as completion.

```python
c.request("thread/attachment/remove", {
	"threadId": "THREAD_ID", "attachmentType": "grok.watch",
	"identityKey": "WATCHER_BOT_OR_CONVERSATION",
}, response_model=RootModel[dict])
```

Unwatch stops the observer only. Interrupting Codex or stopping its worker requires a separate user request.

## Export full history and list files

Export all turns and items to a file. Neither `thread/read(includeTurns=True)` nor a completion summary is full history.

```python
from grok_codex_native.handoff import read_transcript
turns = read_transcript(c, "THREAD_ID")
Path("/var/lib/grok-codex/artifacts/WORKER/THREAD_ID/transcript.json").write_text(
	json.dumps({"threadId": "THREAD_ID", "turns": turns}, ensure_ascii=False, indent=2)
)
```

`read_transcript` reads `thread/turns/list` with `itemsView: "full"` and every item page. A running thread's export reflects the time of reading. Get file paths from handoff's `artifacts` or `artifact_paths(directory, thread_id)` in the same module.

Handoff puts native terminal states and errors in `outcomes` before full `turns`. If artifact validation fails, it preserves completion state and history, returns `artifactError`, and exits with code 1. Do not attach rejected files. For long history, export the file and inspect the summary and path within Grok Shell's output limit.

## List your tasks and all tasks

Find your tasks with the exact `grok.origin` identity saved at launch.

```python
cursor = None
while True:
	page = c.request("thread/attachmentOwner/list", {
		"attachmentType": "grok.origin", "identityKey": "ORIGIN_ID", "cursor": cursor,
	}, response_model=RootModel[dict]).root
	print(json.dumps(page["data"]))
	cursor = page["nextCursor"]
	if cursor is None:
		break
```

The full list covers this control's native store. It does not include external cloud-agent resources. Read every page for both archived states. Include all source kinds to see native subtasks.

```python
from openai_codex.generated.v2_all import ThreadSourceKind
sources = [source.value for source in ThreadSourceKind]
for archived in (False, True):
	cursor = None
	while True:
		page = c.thread_list({"archived": archived, "sourceKinds": sources, "cursor": cursor})
		print(page.model_dump_json(by_alias=True))
		cursor = page.next_cursor
		if cursor is None:
			break
```

## Find a task by PR URL

After creating a real PR, add a native attachment. Its canonical identity is a compact JSON array of hostname, lowercase owner, lowercase repository, and integer PR number. Populate these values from the actual PR, not an inferred URL.

```python
key = json.dumps([hostname, owner.lower(), repository.lower(), pr_number], separators=(",", ":"))
c.request("thread/attachment/add", {
	"threadId": "THREAD_ID", "attachmentType": "pull_request", "identityKey": key,
	"payload": {"url": actual_pr_url},
}, response_model=RootModel[dict])
page = c.request("thread/attachmentOwner/list", {
	"attachmentType": "pull_request", "identityKey": key,
}, response_model=RootModel[dict]).root
print(json.dumps(page))
```

Continue lookup until `nextCursor` is exhausted. A GitHub PR without this recorded attachment does not appear in native reverse lookup.

## Stop, archive, rename, or delete a task

`turn_interrupt` stops the current turn. If the user also requests stopping future execution, read every native queue page and pass each item's `id` as `queuedSubmissionId` to `thread/queue/delete`. Pause runnable goals with `c.pause_goal("THREAD_ID")`. Cancel queue items and pause goals only within the requested scope.

Use the following independent native methods for archive, restore, and rename. Run only the requested call. Archive does not mean stop. Unwatch before archive.

```python
c.thread_archive("THREAD_ID")
c.thread_unarchive("THREAD_ID")
c.thread_set_name("THREAD_ID", "Revised task title")
```

For permanent managed history and artifact deletion, confirm the exact native thread UUID and deletion scope. Unwatch first, then inspect a dry run.

```sh
./scripts/compose.sh exec -T control python -m grok_codex_native.purge --thread THREAD_ID --artifacts-root /var/lib/grok-codex/artifacts --confirm-thread THREAD_ID --dry-run
```

Remove `--dry-run` only after checking the targets and preserved data. The artifact root must exactly match trusted `GROK_CODEX_ARTIFACTS_ROOT`. Managed paths must match `root/worker/exact-thread-UUID` with matching attachment ownership. Symlinks and invalid paths are rejected.

Purge refuses active execution, waiting input, pending approval, queued input, runnable goals, unknown state, and runnable descendants. It checks spawned descendants and captures managed artifact references before native deletion. Purge does not stop tasks. Stop requested work separately first.

Source files, uncommitted changes, external GitHub objects, and copies already delivered to Grok are preserved. Preflight and native deletion are not atomic. Do not submit new input during the check. Report native deletion uncertainty and partial artifact deletion with their original errors and paths. If the native server rejects a queue query on an archived thread, explicitly restore that thread before checking again.

## Inspect available resources

- Read models with `c.model_list()` and follow native pagination.
- Inspect registered environments through Docker management labels, `environment/status`, and `environment/info`. Re-register after server restart.
- Find repositories through existing GitHub tools or `gh repo list`, and verify Git access.
- Use Shell on Grok Bot's own account-shared cloud computer, where this project and its workers are installed.
- Inspect source and artifact bind mounts and the `new-worker.py` output for file locations.
- Inspect the repository's actual `SKILL.md` and native catalog for skills. Control-local `skills/list` does not prove remote worker discovery.

Use `new-worker.py NAME --image IMAGE_REF` to select a prepared image on the same Docker engine. The image must provide the same Codex exec-server version and the `/home/node`, `/work/source`, and `/work/artifacts` execution layout. The script checks existence with `docker image inspect`. It does not pull or build images or maintain an environment registry.

See [pinned sources](sources.md) for exact protocol parameters and experimental feature boundaries.
