# Gateway Technical Documentation

Updated: 2026-11-19

## 1) Scope

This document describes the current gateway implementation, module boundaries, and launch procedure.

Current service entrypoint:

- `gateway/service.py`

## 2) Current architecture

### 2.1 Runtime composition (`GatewayService`)

`GatewayService` is defined in `gateway/service.py` and composes:

- config: `app/config/settings.py`
- deploy API handler: `app/deploy/deploy_handler.py`
- deploy operation store: `app/deploy/operation_store.py`
- deploy orchestrator: `app/deploy/orchestrator.py`
- predict handler: `app/handlers/predict_handler.py`
- info handler: `app/handlers/info_handler.py`
- routing/discovery: `app/routing/*`
- kubernetes auth/client: `app/kubernetes/*`
- manifest builders/templates: `app/manifests/*`

### 2.2 API ownership by module

- `deploy_model`, `deploy_status`: delegated to `DeployHandler`
- `predict`, `batch_predict`: delegated to `PredictHandler`
- `health`, `list_models`: delegated to `InfoHandler`

### 2.3 Deploy status lifecycle

Deploy operation states:

- `queued`
- `building`
- `pushing`
- `deploying`
- `ready`
- `failed`

The in-memory state store is `DeployOperationStore`. Build/deploy execution is in `DeployOrchestrator`.

## 3) API contracts (stable)

Gateway keeps these endpoints:

- `POST /deploy_model`
- `POST /deploy_status`
- `POST /predict`
- `POST /batch_predict`
- `POST /health`
- `POST /list_models`
- `POST /delete_model`
- `POST /delete_operation`

Compatibility objective remains: no payload/response contract break relative to the refactor baseline.

### Model (experiment) and version (run) tracking

One MLflow experiment = one model; each MLflow run = one version of it.

- `POST /deploy_model` accepts `run_id`, `model_uri` (`runs:/<run_id>/ludwig-model`) and
  `experiment_name`. `model_name` is the base name of the deployment (slug, image, default
  `deployed_name`), not the experiment. `run_id` must be a valid Kubernetes label value
  (4-63 chars, alphanumeric at both ends; MLflow run ids are 32 hex chars).
- The three values are stored in the operation (`deploy_status`, `list_models.deploy_operations`)
  and on the model Deployment, its pods and its Service, so they survive gateway restarts
  (the operation store is in memory):

  | Kind | Key | Value |
  |---|---|---|
  | label | `bentloy.io/run-id` | run id |
  | label | `bentloy.io/experiment` | experiment name as a slug (max 63 chars) |
  | annotation | `bentloy.io/model-uri` | source MLflow URI |
  | annotation | `bentloy.io/experiment-name` | exact experiment name |

  The keys are always written (empty when unknown) so a redeploy under the same name
  overwrites them. The selector stays `{app, model-slug}` (a Deployment selector is immutable).
- `list_models.discovered_models[]` reads them back as `run_id`, `model_uri`, `experiment_name`
  (`null` for services deployed before these keys existed).
- `POST /list_models` accepts `experiment_name` and `run_id`: exact filters applied to
  `discovered_models` and `deploy_operations` before pagination. When either is set,
  `configured_models` and `recently_routed_models` are empty. `query` is a case-insensitive
  substring of the deployed name, applied to `discovered_models` and `deploy_operations`.
- `page` starts at 1 (default `1`) and is shared by every list. `page < 1` answers `400`
  with `{"error": msg, "detail": msg}`. A page past the end of a list returns it empty
  (`has_next: false`, `has_previous: true`). Each `pagination.<list>` has `total_items`,
  `total_pages` (`ceil(total_items / page_size)`, `0` for an empty list), `current_page`,
  `page_size`, `has_next` (`current_page < total_pages`) and `has_previous` (`current_page > 1`).

### Failed operations: `error` and `error_log`

`error` holds only the reason (max 500 chars): the last error line of the build container
that failed (`prepare-build-context` or `kaniko`), with a hint when the registry rejects the
push. `error_log` holds the full log of both build containers (or the full Kubernetes answer)
and is returned only by `/deploy_status`, to keep `list_models` light.

### `/deploy_model` errors

Requests that cannot be queued answer with an HTTP error and `{"error": msg, "detail": msg}`:

| Code | When |
|---|---|
| `400` | Invalid parameters, including a `deployed_name` whose slug is empty or whose resource name `bentloy-model-<slug>` exceeds 63 characters |
| `403` | Automatic deploy disabled |
| `409` | The resource name is already in use (see below) |
| `500` | Gateway misconfiguration (registry / base image settings) |
| `503` | The gateway could not read the Kubernetes API to check for an existing deployment |

`error` and `detail` are always present (same text, meant for people, the CLI and logs).
Clients should tell the cases apart by these extra fields, not by the text:

| Answer | Extra fields |
|---|---|
| `409` name in use | `conflict: "exists"`, `resource_name`, `run_id`, `experiment_name` (`null` when the existing resource has no `bentloy.io/run-id` label / `bentloy.io/experiment-name` annotation) |
| `409` operation in progress | `conflict: "in_progress"`, `resource_name`, `operation_id`, `status` |
| `400` invalid `deployed_name` (empty slug or too long) | `invalid_field: "deployed_name"`, `max_length` (longest valid slug: `63 - len(MODEL_SERVICE_PREFIX) - 1`, `49` with the default `bentloy-model`) |
| Other `400`, `403`, `500`, `503` | none |

```json
{"error": "...", "detail": "...", "conflict": "exists",
 "resource_name": "bentloy-model-churn-v2",
 "run_id": "3f2a9c1d8e7b4a6f9c0d1e2f3a4b5c6d", "experiment_name": "Churn Prediction"}
{"error": "...", "detail": "...", "conflict": "in_progress",
 "resource_name": "bentloy-model-churn-v2", "operation_id": "op_20261123101500_a1b2c3",
 "status": "building"}
{"error": "...", "detail": "...", "invalid_field": "deployed_name", "max_length": 49}
```

### `/deploy_model` conflicts and `overwrite`

`deployed_name` is normalized to a slug (`Churn_V2`, `churn-v2` and `churn v2` all give
`churn-v2`) and the model is deployed as the Deployment and Service `bentloy-model-<slug>`.
Different names with the same slug therefore share one resource. Before queuing anything
(before the build Job), the handler checks the target namespace:

1. **Operation in progress** for the same resource (status `queued`, `building`, `pushing`,
   `deploying` or `deleting`): `409`, **even with `overwrite=true`**. A deployment that is
   still being built or deleted cannot be replaced.
2. **Existing Service or Deployment** named `bentloy-model-<slug>`: `409` unless
   `overwrite=true` is passed. The `detail` names the requested `deployed_name`, the
   resource, and the `run_id` / `experiment_name` of the existing deployment, read from its
   `bentloy.io/run-id` label and `bentloy.io/experiment-name` annotation when present:

   ```
   'Churn V2' maps to bentloy-model-churn-v2 in namespace 'api-models', already used by run
   3f2a... (experiment 'Churn Prediction'). Use another name or overwrite=true.
   ```

With `overwrite=true` (default `false`) and nothing in progress, the existing Deployment and
Service are patched in place, as all deploys did before this check existed.

The check and the creation of the resources are not atomic (the build takes minutes). Within
one gateway the operation store closes the gap: an active operation blocks new deploys of the
same resource and the store re-checks atomically when queuing. Several gateway replicas, or
resources applied by hand during a build, can still race; no distributed lock is used.

> **Behaviour change:** before this check, a deploy whose slug matched an existing model
> replaced it silently. Clients that relied on that must now pass `overwrite=true`.

### Deleting: `/delete_model` and `/delete_operation`

Both work on the resource name `bentloy-model-<slug>`, never on the raw `deployed_name`.

- `POST /delete_model {"service_name"}` deletes the Deployment and the Service named
  `service_name` (Deployment pods are removed by Kubernetes garbage collection), then the
  image the Deployment ran. Only `<MODEL_SERVICE_PREFIX>-*` names are accepted, so it cannot
  delete other workloads of the namespace (e.g. the gateway). Answers
  `{"status": "success" | "partial_success" | "error", "message"}`.
- `POST /delete_operation {"operation_id"}` sets the operation to `deleting` and, in the
  background, deletes what that operation created, then removes it from the store
  (`delete_failed` + `error` if something fails):
  - the Deployment and the Service, **only if** their `bentloy.io/operation-id`
    annotation is this operation. Every deploy writes it on the Deployment and Service
    metadata (not on the pod template). If another operation owns them (a later
    deploy with `overwrite=true`) or they have no annotation (deployed by an older
    gateway), they are kept;
  - its build Job;
  - its image, under the rule below.

**Images in use are kept.** The image name is `model-<model_name slug>:v<version>-r<run_id[:8]>`
and does not depend on `deployed_name`, so several deployments of the same model and run
share one image. Before deleting an image, the gateway lists the Deployments of the
namespace and checks the operations still deploying (`queued`, `building`, `pushing`,
`deploying`). If any of them uses the image, it is kept and the answer/log says who uses
it; deleting that last user later deletes the image. The cost is one `LIST deployments`
call per deletion (the RBAC already allows it), negligible next to the `crane` Job.
`crane` deletes by `repo@digest`; tags of different builds never share a digest because
kaniko builds are not reproducible (no `--reproducible`), so comparing image references is
enough.

Limits: only the target namespace is checked; the image a Deployment ran before an
`overwrite=true` redeploy is not deleted by that redeploy (it stays in the registry).

## 4) Environment and config

Main environment variables consumed by gateway:

- routing/discovery: `MODEL_SERVICE_PREFIX`, `K8S_NAMESPACE` (default: `default`), `MODEL_URL_TEMPLATE`
- deploy flow toggle/timeouts: `AUTO_DEPLOY_ENABLED`, `DEPLOY_BUILD_TIMEOUT_SEC`, `DEPLOY_ROLLOUT_TIMEOUT_SEC`
- registry/build: `REGISTRY_SERVER`, `REGISTRY_NAMESPACE` (optional; empty = `<REGISTRY_SERVER>/<image>`), `MODEL_BASE_IMAGE`, `MODEL_REGISTRY_SECRET`, `KANIKO_IMAGE`, `KANIKO_SKIP_TLS_VERIFY`
- build options: `KANIKO_CACHE` (default `1`: cache the dependency layer in `<REGISTRY_SERVER>[/<REGISTRY_NAMESPACE>]/kaniko-cache`), `MODEL_PIP_EXTRA_INDEX_URL` (default `https://download.pytorch.org/whl/cpu`; extra pip index for model dependencies, set it empty to disable)
- MLflow default: `MLFLOW_TRACKING_URI`
- optional model url mappings: `MODELO_<NAME>_URL`

Source parser:

- `app/config/settings.py`

## 5) Packaging and container entry

- Docker command: `gateway/Dockerfile` -> `bentoml serve service:GatewayService ...`
- Source path: `gateway/service.py` and `gateway/app/**`

## 6) Launch from zero

Canonical runbook: `docs/runbook.md`. In short:

1. Configure `.env` (`MLFLOW_TRACKING_URI`, `REGISTRY_SERVER` (and optionally `REGISTRY_NAMESPACE`, `REGISTRY_USERNAME/PASSWORD`), `K8S_NAMESPACE`).
2. `python bentloy.py up`: builds/pushes the gateway and model base images if missing,
   applies the registry Secret and `k8s/templates/gateway.yaml`, waits for the rollout.
   The gateway reads its configuration from the `bentloy-gateway-config` ConfigMap (`envFrom`).
3. `python bentloy.py deploy <model>`: `POST /deploy_model` + polling of `POST /deploy_status`.

Notes:

- If the registry Secret is managed externally, leave `REGISTRY_USERNAME/PASSWORD`
  empty and `up` will not touch it.
- Gateway exposure is `Service.type=LoadBalancer` by default (`GATEWAY_SERVICE_TYPE`).

## 7) Tests

```bash
python -m unittest discover -s gateway/tests -t .
python -m unittest discover -s model_service/tests -t .
python -m unittest discover -s tests -t .    # bentloy.py CLI
```

These cover internal contracts and module wiring. Cluster-level rollout still depends
on registry, RBAC, and Kubernetes connectivity in your environment.
