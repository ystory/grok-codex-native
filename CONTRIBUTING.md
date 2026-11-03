# Development

Use Python 3.11 or newer. From the project root, create a virtual environment and install the development extra.

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
python -m pytest -q
python -m pip wheel --no-deps --wheel-dir dist .
```

The unit tests use fixtures. They do not need a ChatGPT login, Grok account, running Docker engine, or model calls.

## What CI checks

The [workflow](.github/workflows/ci.yml) uses Python 3.11, the minimum supported version and the version in the deployment images. The Ubuntu 24.04 runner supplies Docker and Compose.

| Check | What a failure detects |
| --- | --- |
| Unit tests | Regressions in native control, deletion guards, completion results, and archive boundaries. |
| `scripts/check-deployment.py` | Invalid Compose settings, changed mount or credential boundaries, missing native integrity locks, and invalid deployment Python syntax. It renders configuration without starting services. |
| Wheel build and import outside the checkout | Missing package metadata or files that editable source imports would hide. |
| Deployment archive build | Release-script errors, forbidden tracked paths, non-regular files, and tracked changes from earlier steps. It packages the tracked file set; it does not check a required file list or scan for all secrets. |
| Disposable container smoke check | Image build failures, broken native server or SDK transport, and a worker executable that cannot run as its configured user. |

The container job starts with no ChatGPT credentials. It uses `deploy/healthcheck.py` without `--require-chatgpt`, runs the worker's `codex --version`, and cleans up. Worker registration requires ChatGPT login in the deployment helper, so registration, live model execution, and Grok delivery remain manual acceptance checks. CI does not bypass that guard. The [sources](docs/sources.md#development-and-repository-conventions) record the official specifications and reference repositories behind these choices.

Live checks in `checks/` use a prepared Linux deployment. Model checks require the explicit `--run-models` flag and use your subscription. Follow [the verification guide](docs/verification.md) when changing execution behavior.

Keep changes small and describe the problem, resulting behavior, and checks you ran. For a larger change, discuss the approach before implementing it.

Codex owns sessions, turns, history, queues, and errors. Reuse native APIs and existing tools. Add integration code only when a concrete native limitation requires it. See [the capability contract](docs/contract.md).

Before sharing a patch, review its files and commit messages for credentials, private prompts, session identifiers, and local machine paths. Keep authentication, deployment data, and private logs outside the checkout. Use redacted reproductions for bug reports.

Contributions are provided under the project's [Apache-2.0 license](LICENSE).
