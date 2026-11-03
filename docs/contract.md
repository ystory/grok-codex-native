# Capabilities and limits

The implementation uses the pinned native protocol described in [sources](sources.md). [Maintainer verification](verification-results.md) covers a Linux arm64 backend and a separate Grok Task observation. Complete each deployment's [acceptance checks](verification.md) before claiming that installation works.

## Capability reference

| Capability | Implementation | Verification boundary |
| --- | --- | --- |
| Start with a task, repository, ref, title, files, images, or secrets | Docker, Git, native thread and turn inputs, launch example | Maintainer verified a local PR-style ref, repository skill, image input, fake secret, and explicit model settings. Real target inputs require acceptance checks. |
| Start without a repository and later add a remote | Local `git init`, existing GitHub tools or `gh` | Local initialization verified. Private GitHub repository creation and transition remain unverified. |
| Select the execution computer and worker | Grok computer selection or SSH, native environment | Backend worker execution verified. Grok installation remains unverified. |
| Use an explicitly requested model, settings, or repository skill | Native model catalog, thread configuration, skill input | Explicit model and effort, and worker skill use verified. Return native rejection for unavailable choices. |
| Return completion and files to the original Grok conversation | Finite handoff, existing Task completion notification, `SendMessage` | Backend handoff and separate Task wakeup observed. Complete delivery and actual attachments remain unverified. |
| Watch another Bot's task and unwatch | Native attachment, observer Task and Shell waiter | Container waiter and transport cleanup verified. Cross-Bot delivery remains unverified. |
| Steer, queue after the current turn, or interrupt and start again | Native turn control and native thread queue | Backend control and automatic queued execution verified. Queue only behind an active turn. Otherwise use an explicit worker on `turn/start`. |
| Inspect status, branch, PR, and change size | Native status and history, Git, GitHub tools | Native status verified. Confirm actual Git and GitHub state separately. |
| Export full history | Full turn and item pagination | Backend full records and pagination behavior tests verified. Large real installation histories still require acceptance checks. |
| Stop, archive, restore, rename, and permanently delete | Native methods and purge preflight | Backend lifecycle and stopped-only deletion verified. Queue, goal, descendant, and unknown-state guards have behavior tests. |
| List the calling Bot's tasks or all tasks | Exact `grok.origin` identity, native `thread/list` | Native lookup verified. Read all pages, archived states, and source kinds. |
| Find a task by PR URL | Canonical `pull_request` attachment identity | Metadata fixture verified. Actual private GitHub PR creation remains unverified. |
| List artifacts | Bind mount and checked regular-file paths | Real files and PNGs verified. Path and symlink guards have behavior tests. Grok attachments remain unverified. |
| Discover repositories, models, computers, environments, and skills | Existing GitHub and Grok tools, native catalogs, Docker | Verify current access in each installation. A control-local skill catalog is not a worker catalog. |

Project coordination and global routing changes are outside the scope of this project.

## Added responsibilities

`grok_codex_native.handoff` observes native threads and turns for a finite period, then returns full history and file paths. Existing Grok tools send messages and attachments.

`grok_codex_native.purge` checks the requested deletion scope before native deletion. It refuses runnable threads and descendants, and deletes only validated managed artifact directories after native deletion succeeds.

Deployment scripts combine Docker, Git, and native `environment/add`. There is no separate state database, launch queue, external retry service, automatic model selection, callback service, or Project UI.

## Execution and lifecycle limits

- Workers use Docker filesystem, PID, and network isolation under native `externalSandbox`. Workers share the host kernel. Docker's default seccomp and AppArmor policies stay enabled.
- Control uses Linux host networking. Worker exec ports bind only to VM loopback and require capability-token authentication. VM and Docker administrators are trusted.
- The ChatGPT login stays in control. Workers receive neither control credentials nor the Docker socket. Worker GitHub credentials and task secrets are separate.
- `include_local = false` excludes the local execution environment. A task selects exactly its registered worker.
- Native environments and `deferred_executor` are experimental. The pinned source marks `deferred_executor` as `UnderDevelopment`. Environment registrations live in server memory and must be recreated after control restarts. There is no claimed `environment/remove` method or automatic recovery of remote turns.
- Cold resume does not restore the worker selection. Queue input only when the thread is active. For a fresh execution on an existing thread, set `turn/start.environments`, not `thread/resume`. Before resuming an unloaded thread, inspect and cancel leftover queued input, then send it through an explicit turn. Apply the same check before starting a waiter that may resume the thread.
- `approval_policy = "never"` and an explicit native decline handler reject permission escalation. There is no interactive approval relay between conversations.
- Purge accepts only the configured artifact root, exact native UUID ownership, and stopped threads and descendants. Preflight and deletion are not atomic. Unknown state and connection errors do not mean deletion is safe.
- Unwatch before archiving or deleting a watched thread. A waiter does not treat archive or deletion as task completion. Stop the observer's real container PID, then clean up its Shell, Task, and attachment.
- No hosted task page is available through this project. Use native IDs, full history files, artifacts, and actual PR URLs.
- The source tree, uncommitted work, external GitHub objects, and previously delivered file copies are preserved by managed history and artifact deletion.

## Remaining acceptance work

Production Grok installation, shared skill registration, complete native-task delivery with actual attachments, long Task lifetimes, repeated follow-up delivery, and cross-Bot watch remain unverified. Real private GitHub authentication, repository transition, and PR creation remain unverified. Actual amd64 execution and automatic recovery of a turn interrupted by a server restart also remain unverified.
