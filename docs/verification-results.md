# Maintainer verification results

A maintainer ran native Codex tasks with a ChatGPT subscription on Linux arm64. These are private maintainer verification results, not independently reproducible public evidence. Private logs and transcripts are excluded because they can contain credentials and personal history. The links below point to public check implementations and [manual acceptance procedures](verification.md), not the original private records.

Production Grok installation, shared skill registration, and the complete native-task-to-Grok-file-attachment flow remain unverified.

## Verified backend and reproducible checks

The verified versions were Codex CLI `0.166.0`, official Python SDK `0.163.0`, and websocat `4.0.0-alpha3`. The Codex source reference was `6ff1e1ca3b30fc8b914720681fcf5fe62e95f856`. Control and worker images were built and executed on Linux arm64. These results do not establish amd64 execution or the state of any current installation.

| Maintainer result | Public check or procedure | Limit |
| --- | --- | --- |
| Two native tasks ran concurrently, completed through finite handoff, and returned full command items, text files, and PNG artifacts. Workers used different dependency versions and separate loopback mappings for internal port 3000. | [smoke.py](../checks/smoke.py), [backend verification](verification.md#verify-native-execution-and-isolation) | This was not a comparison of different model families. |
| A separate connection read status and steered, queued and canceled input, interrupted a turn, and started a follow-up. Active purge was refused, and stopped disposable history and artifacts were deleted. An unloaded thread also ran a follow-up with an explicit worker environment. | [lifecycle.py](../checks/lifecycle.py) | Unknown states and descendant, queue, and goal guards also rely on behavior tests. |
| Start-only launch returned while a turn remained active. Separate waiters collected consecutive completions. Native queued input ran automatically after the current turn, with actual output files checked in order. | [start_only.py](../checks/start_only.py) | Independent Python waiters ran this check. It did not test Grok Task ownership. |
| A local bare repository supplied a PR-style ref, an explicitly selected model and effort, a worker repository skill, an image, and a fake secret whose digest matched. | [fixture_repo.py](../checks/fixture_repo.py), [native_inputs.py](../checks/native_inputs.py) | No real private GitHub authentication or PR creation was tested. |
| Large native requests and responses completed. Requests still worked after an RPC error. Exec connections without a token or with a wrong token received HTTP 401. | [native_transport.py](../checks/native_transport.py), [protocol.py](../checks/protocol.py) | Protocol and authentication checks do not call a model. |
| Worker secrets, control ChatGPT credentials, Docker socket access, and source preservation matched the intended separation. | [isolation procedure](verification.md#verify-native-execution-and-isolation) | Workers share a kernel. Host and Docker administrators remain trusted. |
| Native origin lookup, all-task listing, model and environment discovery, PR attachment identity lookup, rename, archive, and restore worked. | [history and lookup procedure](verification.md#verify-history-and-lists) | The PR attachment was metadata, not a newly created GitHub PR. |
| A candidate archive passed manifest checks, Linux builds, startup, authentication, and fresh worker registration from a new installation path. A wheel imported in a separate environment with hash-pinned dependencies. | [archive procedure](operations.md#build-and-verify-a-release-archive), [deployment checks](verification.md#verify-deployment-and-authentication) | An archive's integrity does not prove a different release or installation works. |
| After control restart and environment re-registration, native history, attachments, and files remained. A fresh turn with explicit `turn/start.environments` wrote its expected file. | [restart procedure](verification.md#verify-restart-and-removal) | Automatic recovery of a turn active during restart was not tested. |
| Terminating the observer's real PID inside control removed the waiter and its transport child. | [watch procedure](native-use.md#watch-another-task-and-unwatch) | Complete cross-Bot watch and unwatch remain unverified. |
| Selecting a prepared custom image created a worker with the expected image identity and a successful environment handshake. | [image requirements](native-use.md#inspect-available-resources) | No separate model turn ran for this custom-image check. |
| Health checks rejected an unauthenticated server and recognized added ChatGPT credentials without restart, then still recognized them after restart. | [authentication procedure](verification.md#verify-deployment-and-authentication) | This check did not exercise the device-login UI. |

The maintainer reported **49 passing behavior tests**. The total comprised 44 policy tests, two archive tests, and three completion-summary and artifact-error tests. This is a historical maintainer result, not a result for your checkout. Run the [current local tests](verification.md#verify-local-behavior-tests) and deployment checks in your checkout before release.

The archive tests covered tracked-file inclusion, private `.env` exclusion and rejection, manifest integrity, byte-identical repeated builds, and rejection of unstaged changes. The handoff tests covered completion summaries before large histories and preservation of native outcome and history when artifact validation fails. A separate completed native turn with a symlink artifact also returned an artifact error with exit code 1 while preserving its native result.

## Observed failures and resulting constraints

The maintainer observed large-frame transmission stop with websocat `1.14.1`. Increasing the buffer did not fix it. Standard websocat `4.0.0-alpha3` passed the large SDK round trip, including inside Linux arm64 containers. The project pins that prerelease and adds no custom transport implementation. Prerelease dependency risk remains.

The initial default Codex sandbox failed to create a bwrap namespace under Docker's default policies. Verified tasks used native `externalSandbox` with `networkAccess: enabled`, while retaining Docker's default seccomp and AppArmor policies. Docker supplies filesystem, PID, and network isolation. All workers share the host kernel.

An initial post-restart follow-up failed to write its expected file because execution lacked a usable shell. `thread/resume` does not accept `environments`. After re-registration, setting the worker through `turn/start.environments` produced the expected file. A later unloaded-thread lifecycle check also succeeded. This established the documented execution procedure, not an automatic recovery mechanism.

Native environment registration and `deferred_executor` remain experimental. Registrations are held in control memory and must be recreated after restart. Interactive approval relay is absent. The deployment uses `never` and explicit native decline responses. Purge preflight and native deletion are not atomic against concurrent input.

## Separate Grok observation

In a separate private maintainer observation, a delayed completion from an existing Grok Task caused the parent Bot to respond again without new user input. A Shell-only background attempt did not show the same wakeup. This observation supports using Task to own the finite waiter, but does not establish native Codex execution, long Task lifetime, or actual file attachment in Grok.

## Unverified acceptance criteria

Complete the [target-VM verification guide](verification.md) for each installation. The following results remain unverified.

- Production Grok VM installation and login, shared skill registration, and per-Bot activation.
- Native Codex results, full history, and real attachments arriving in the original Grok conversation and opening in the app.
- Long Task lifetime, repeated observation after follow-up turns, and cross-Bot watch and unwatch.
- Actual private GitHub authentication, transition to a permanent repository, PR creation, and lookup of that PR.
- Actual amd64 execution and recovery of a turn active during server restart.

The project provides no hosted task page and makes no claim that private verification services or artifacts remain available.
