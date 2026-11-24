# CLAUDE.md

Guidance for Claude Code when working in this repository.

## Project

Bentloy is an API gateway and model-serving stack that deploys MLflow models on Kubernetes using BentoML.

- **Gateway** (`gateway/service.py`, BentoML service `GatewayService`): public API for predictions, deployments, listing, deletion and logs. It talks to the Kubernetes API directly over HTTP (`httpx`) using its ServiceAccount (`bentloy-gateway-sa`).
- **Model service** (`model_service/`): BentoML runtime image. Each deployed model runs as its own Deployment and Service named `bentloy-model-<slug>` (container `model-service`).
- **Build**: a Kubernetes Job (`bentloy-build-<slug>-<suffix>`) pulls the model artifacts from MLflow (init container `prepare-build-context`) and builds and pushes one image per model with Kaniko (container `kaniko`).
- **CLI** (`bentloy.py`, stdlib only): `up`, `build`, `deploy`, `status`, `models`, `predict`, `delete`. `python bentloy.py up` builds the base images, renders `k8s/templates/*.yaml` from `.env` and deploys the gateway. Do not `kubectl apply` the templates directly.
- **Consumers**: the OpenTwins Grafana plugin is a frontend client of the gateway API (for example, it polls the log endpoints every ~3 s).

### Gateway endpoints

`/predict`, `/batch_predict`, `/deploy_model`, `/deploy_status`, `/list_models`, `/delete_model`, `/delete_operation`, `/deployment_logs`, `/build_logs`, `/health`. Contracts are documented in `docs/gateway_technical.md`; keep it in sync with any API change.

### Main flows

- **Deploy**: `/deploy_model` → `DeployHandler` validates, checks conflicts (409) and queues an operation → `DeployOrchestrator.run_operation` runs the steps `queued` → `create_job` → `kaniko_push` → `apply_resources` → `rollout` → `completed` | `failed`. Clients poll `/deploy_status`.
- **Operations** live in an in-memory store (`DeployOperationStore`) and are lost when the gateway restarts.
- **Predict routing** (`routing/target_resolver.py`): explicit `model_url`, then the `MODELO_<NAME>_URL` env mapping, then `MODEL_URL_TEMPLATE` with the `bentloy-model-<slug>` convention. Discovery lists Services with the `MODEL_SERVICE_PREFIX` prefix.
- **Delete**: removes the Deployment and Service and deletes the registry image unless another deployment still uses it.
- **Logs**: `/deployment_logs` (model pod) and `/build_logs` (build Job pod). Expected states return 200 with `available: false` and a `reason`.

### Layout

| Path | Purpose |
|---|---|
| `gateway/service.py` | API surface; maps request errors to HTTP status codes |
| `gateway/app/config/` | `GatewaySettings.from_env` |
| `gateway/app/deploy/` | Deploy validation, orchestration, operation store, failure messages |
| `gateway/app/handlers/` | Predict, info (`health`, `list_models`) and logs handlers |
| `gateway/app/routing/` | Predict target resolution and Service discovery |
| `gateway/app/kubernetes/` | Thin async Kubernetes API client and in-cluster auth |
| `gateway/app/manifests/` | Manifest builders and YAML/shell templates |
| `gateway/app/domain/` | Shared helpers (slugs, labels, types) |
| `model_service/` | Per-model BentoML runtime |
| `k8s/templates/` | Gateway RBAC, ConfigMap, Deployment, Service; registry secret |
| `docs/` | `gateway_technical.md` (API contracts), `runbook.md` (operations) |
| `tests/` | pytest tests mirroring the code: `gateway/`, `model_service/` and `cli/` (`bentloy.py`) |

## Commands

Python 3.12. Windows virtual environment in `.venv`.

```bash
py -3.12 -m venv .venv
.venv/Scripts/python.exe -m pip install -r requirements.txt

# Tests (pytest)
.venv/Scripts/python.exe -m pytest                      # all suites
.venv/Scripts/python.exe -m pytest tests/gateway        # one suite (or a single test file)
.venv/Scripts/python.exe -m pytest -k build_logs         # by name

# Deploy the gateway to the cluster
python bentloy.py up            # --dry-run to preview
```

The configuration comes from `.env` (see `.env_example`). `.env` contains secrets: never print or commit it. Local, environment-specific values (such as the gateway URL) live in `CLAUDE.local.md`, which Claude Code loads automatically and git ignores.

## Agents and workflow

The main session orchestrates and verifies; it delegates the work to the project agents in `.claude/agents/`:

| Agent | Model | Use it to |
|---|---|---|
| `explore` | haiku | Search and read the codebase (read-only). Use it for any lookup instead of reading files directly. |
| `coder` | sonnet | Write or modify code, tests and docs, following precise instructions. |
| `tester` | sonnet | Act as an end user against the real cluster over HTTP only (no code, no files, no `kubectl`). The main session passes it the gateway URL, read from `CLAUDE.local.md`; without a reachable URL it stops and asks for one. |

1. Gather context with `explore`.
2. Give `coder` a precise design: files, signatures, contracts, edge cases and the tests to add.
3. Verify the result yourself: run the test suites and review the diff against the request. Send corrections back to `coder`.
4. When the change affects the deployed system, validate it with `tester` on the cluster. Read the gateway URL from `CLAUDE.local.md` and pass it in the task prompt. If the URL is missing, or `tester` reports `NEED_GATEWAY_URL`, ask the user for the current one and update `CLAUDE.local.md`.

## Git

- **Never commit, never create branches, never push.** Leave all changes uncommitted on `main`; the user commits.
- When a task is done, give the user a ready-to-use commit message in professional English following Conventional Commits (`feat`, `fix`, `refactor`, `docs`, `test`, `chore`, with an optional scope such as `feat(gateway):`, and `!` for breaking changes). It has a short imperative subject (72 characters at most), a body explaining what changed and why, and the list of files to include.

## Sensitive information

- Never write URLs or IPs of real environments, hostnames, credentials, tokens, passwords, emails or personal paths in tracked files: code, docs, `CLAUDE.md`, `.claude/agents/`, tests, examples or commit messages.
- Use placeholders (for example `<gateway-url>`) or environment variables instead.
- Keep local values in `CLAUDE.local.md` and secrets in `.env`. Both are git-ignored.
- Before finishing a task, check the diff for sensitive data.

## Coding standards

- **English everywhere**: identifiers, comments, docstrings, log and error messages, docs. Use professional wording.
- **Docstrings**: every module, class, method and function has a docstring that explains what it does (one line is enough when the behavior is obvious; add details for parameters, return values and errors when it is not).
- **Conventions**: PEP 8, snake_case functions and variables, PascalCase classes, `_private` helpers, `UPPER_CASE` constants, type hints throughout (`str | None`, `dict[str, Any]`). Handlers are `*Handler`, exceptions `*Error`.
- **Reuse before writing**: look for an existing helper first (`kubernetes/client.py`, `domain/helpers.py`, `deploy/errors.py`, manifest builders). Extract shared logic instead of duplicating it.
- **Size and structure**: keep functions short and focused, and modules cohesive. Split a module when it mixes responsibilities or grows hard to read, but do not create a file per function: group related code by responsibility, as in the existing `app/` subpackages.
- **Readability over cleverness**: clear names, no dead code, comments explain *why*, not *what* the code already says.
- **Errors**: request errors return `{"error": str, "detail": str}` with the HTTP status set on `ctx.response.status_code` (see `DeployRequestError`, `ListModelsRequestError`, `LogsRequestError`). Expected states are not errors. Kubernetes API failures map to 502.
- **Tests**: every change comes with tests. Tests live in `tests/<component>/<package>/`, mirroring the code, and are written as pytest functions (classes only to group related tests, without inheritance). Shared setup goes in fixtures in `conftest.py` and fake classes in `fakes.py`; use `parametrize` for variants, `monkeypatch` and `tmp_path` instead of manual patching and temporary files, and `pytest-asyncio` in auto mode for async tests (no decorator needed). Every test and fixture has a one-line docstring. Mock the Kubernetes API with `httpx.MockTransport` injected through an `http_client_factory`. The full suite must pass.
- **Docs**: update `docs/gateway_technical.md` (and `README.md` or `docs/runbook.md` when relevant) together with the code.
- **RBAC**: the gateway Role is in `k8s/templates/gateway.yaml`; check it before relying on new Kubernetes permissions.
