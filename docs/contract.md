# Capabilities and limits

This project moves the delegation workflow of Grok Bot's built-in Cloud Agent to Codex on the Bot's own cloud computer. This page records what was ported, what changed, and what was observed in testing.

Complete the [acceptance checks](verification.md) before relying on your own installation. The implementation targets the pinned versions in [sources](sources.md).

## Capability matrix

The list comes from a Bot reading its own Cloud Agent tool definitions on 2026-11-03. That description says the Cloud Agent runs on Cursor-managed Linux computers by default. It is not official documentation and may change.

| Status | Meaning |
| --- | --- |
| Ported | The workflow works through Codex, with any difference noted. |
| Partial | Part of the workflow works, or a tested part failed. |
| Narrowed | Intentionally reduced in scope. |
| Not ported | Intentionally left out. |

| # | Cloud Agent capability | With this project | Status | Difference or limit |
| --- | --- | --- | --- | --- |
| 1 | Start with a task, repository, ref, title, files, images, or secrets | Docker worker, Git, native thread and turn inputs | Partial | No hosted task page or link. Use native IDs and history files. |
| 2 | Start without a repository, then make it permanent | Local `git init`, then GitHub tools or `gh` | Partial | Moving a temporary repository to a permanent one is untested. |
| 3 | Choose where the task runs | Docker worker on the Bot's cloud computer | Narrowed | Personal computers, team computers, and saved environments are out of scope. |
| 4 | Set a model and its settings on request | Native model catalog and thread settings | Ported | Unavailable choices return the native rejection. |
| 5 | Start in a repository skill's mode | Native skill input | Ported | |
| 6 | Get notified on completion, with files | Finite handoff, Grok Task, `SendMessage` attachments | Ported | Requires a Task waiter. Shell-only background wakeup is untested. |
| 7 | Watch another agent's task and unwatch | Native attachment, observer Task, and waiter | Ported | Unwatch before archiving or deleting the thread. |
| 8 | Steer, queue, or interrupt and restart | Native turn control and thread queue | Partial | Steer and queue passed. An interrupted process kept running, so confirm it stopped before restarting. |
| 9 | Inspect status, branch, PR, and change size | Native status and history, Git, GitHub tools | Ported | Check Git and GitHub state separately. |
| 10 | Export full history | Full turn and item pagination | Partial | An interrupted turn's running command was missing from native history. |
| 11 | Stop, archive, restore, rename, and permanently delete | Native methods and purge preflight | Partial | Stopping does not guarantee the process ended immediately. |
| 12 | List your tasks or all tasks | `grok.origin` identity and native `thread/list` | Ported | No project filter. |
| 13 | Find a task by PR | Native `pull_request` attachment | Ported | Only finds PRs whose attachment was recorded. |
| 14 | List artifacts | Bind mount and checked regular-file paths | Ported | |
| 15 | Discover repositories, models, environments, and skills | Existing GitHub and Grok tools, native catalogs, Docker | Partial | No single lookup. A control skill catalog does not describe a worker. |
| 16 | Project mode, where a coordinator splits work across subtasks | None | Not ported | There is no coordinator or Project UI. |

## What was tested

A maintainer ran these checks on one cloud computer on 2026-11-03, using Codex CLI `0.166.0` and the default model. The logs are private, and the results do not establish behavior on your installation. Run the [acceptance checks](verification.md) to reproduce them.

| Case | Result | What it does not establish |
| --- | --- | --- |
| Eight light tasks at once, capped at 0.75 CPU, 256 MiB, and 128 PIDs each | All passed. Commands overlapped for 77 seconds. Control peaked at 407 MiB, each worker at 23 MiB or less, and the host kept at least 2.96 GiB available. | Capacity for arbitrary workloads. |
| One browser and HTTP-server worker plus seven light workers | All passed. Commands overlapped for 58 seconds. Control peaked at 467 MiB, the browser worker at 243 MiB and 163 PIDs, and the host kept at least 2.88 GiB available. | Eight browser workloads. |
| 40-minute task with a finite Task waiter, parent Bot idle | Completed, then woke the parent and attached real files. | Longer tasks. The computer was not observed asleep. |
| 10-minute task with no waiter while the computer slept | Work paused for about 701 seconds. After a manual Wake, the same process continued and the turn completed. | Work continuing during sleep, or a general no-loss guarantee. |
| **Update Bot's Computer** after tasks completed | Docker and containers were removed. The installer and four worker restores succeeded, with the login kept and history byte-identical. | Recovery of running tasks. |
| Full Reset during a 20-minute task | **The task was interrupted at about 228 seconds and did not resume.** Thirteen workers and 17 earlier threads were restored, and an explicit new turn in the original thread succeeded. | Automatic continuation. |
| Native interrupt | **The worker process was still alive about 8.4 seconds after the `interrupted` state.** The exact stop time and cause are unknown. | Immediate termination. |
| Two workers' network boundaries | Public HTTPS worked. The host Docker API and the other worker were blocked. Missing or wrong exec tokens were rejected. | Reachability of every external service. |
| GitHub through `gh` | Private clone, push, and draft PR worked, hosted CI passed, and the PR found its thread. | Human review flows. |
| Explicit model and effort, repository skill, image, fake secret, and permanent deletion, on an earlier separate Linux deployment | All passed, with deletion limited to disposable threads. | The same checks on the cloud computer. |

## Execution and lifecycle limits

- **Sleep:** Sleep pauses workers. Report paused time separately from execution time.
- **Reset and updates:** Treat running tasks as lost. Follow the [recovery procedure](operations.md#recover-after-hosted-computer-replacement) and continue with an explicit new turn.
- **Interruption:** `interrupted` is a native state, not proof that a process ended. This client adds no process-kill bridge.
- **Isolation:** Workers use Docker filesystem, PID, and network isolation under native `externalSandbox`, and share the host kernel. Docker's default seccomp and AppArmor policies stay enabled.
- **Trust:** Control uses Linux host networking. Worker exec ports bind only to loopback and require a token. Computer and Docker administrators are trusted.
- **Credentials:** The ChatGPT login stays in control. Workers receive neither that login nor the Docker socket. Worker GitHub credentials and task secrets are separate. [All your Bots share the computer's files and command-line credentials](https://docs.x.ai/grok-bot/computer-and-apps).
- **Environment selection:** `include_local = false` excludes the local execution environment, and a task selects exactly its registered worker.
- **Experimental dependencies:** Native environments and `deferred_executor` are experimental, and the pinned source marks `deferred_executor` as `UnderDevelopment`. Websocat is a pinned prerelease.
- **Restarts:** Environment registrations live in server memory and must be recreated after control restarts. There is no `environment/remove` method.
- **Resuming:** `thread/resume` does not restore the worker selection. Set `turn/start.environments` for a fresh execution. Before resuming an unloaded thread or attaching a waiter that may resume it, inspect and cancel leftover queued input. Queue input only while the thread is active.
- **Approvals:** `approval_policy = "never"` and an explicit native decline handler reject permission escalation. There is no approval relay between conversations.
- **Deletion:** Purge accepts only the configured artifact root, exact native UUID ownership, and stopped threads and descendants. Preflight and deletion are not atomic. Source, uncommitted work, external GitHub objects, and delivered file copies are kept.

## Added responsibilities

`grok_codex_native.handoff` observes native threads and turns for a finite period, then returns full history and file paths. Existing Grok tools send messages and attachments.

`grok_codex_native.purge` checks the requested deletion scope before native deletion. It refuses runnable threads and descendants, and deletes only validated managed artifact directories after native deletion succeeds.

Deployment scripts combine Docker, Git, and native `environment/add`. There is no separate state database, launch queue, external retry service, automatic model selection, or callback service.

## Constraints from observed failures

- Large frames stopped with websocat `1.14.1`, and a larger buffer did not help. `4.0.0-alpha3` passed the large SDK round trip, so the project pins that prerelease and adds no custom transport.
- Codex's default sandbox could not create a bwrap namespace under Docker's default policies. Tasks therefore use native `externalSandbox`, with Docker supplying the isolation.
- A follow-up after restart lacked a usable shell because `thread/resume` does not accept `environments`. Selecting the worker through `turn/start.environments` fixed it.

## Untested

- Bots or installations beyond the maintainer's cloud computer, and a fresh installation from this repository.
- Tasks longer than 40 minutes, more than eight concurrent tasks, and models other than the default in the cases above.
- Shell-only background wakeup.
- Moving a temporary task repository to a permanent one, and review by a real GitHub reviewer.
- Restarting control during an active task.
