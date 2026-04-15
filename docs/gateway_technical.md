# Gateway Technical Documentation

Updated: 2026-04-15

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

Compatibility objective remains: no payload/response contract break relative to the refactor baseline.

## 4) Environment and config

Main environment variables consumed by gateway:

- routing/discovery: `MODEL_SERVICE_PREFIX`, `MODEL_NAMESPACE`, `MODEL_URL_TEMPLATE`
- deploy flow toggle/timeouts: `AUTO_DEPLOY_ENABLED`, `DEPLOY_BUILD_TIMEOUT_SEC`, `DEPLOY_ROLLOUT_TIMEOUT_SEC`
- registry/build: `REGISTRY_SERVER`, `TU_ORG`, `MODEL_BASE_IMAGE`, `MODEL_REGISTRY_SECRET`, `KANIKO_IMAGE`, `KANIKO_SKIP_TLS_VERIFY`
- MLflow default: `MLFLOW_TRACKING_URI`
- optional model url mappings: `MODELO_<NAME>_URL`

Source parser:

- `app/config/settings.py`

## 5) Packaging and container entry

- Bento service reference: `gateway/bentofile.yaml` -> `service:GatewayService`
- Docker command: `gateway/Dockerfile` -> `bentoml serve service:GatewayService ...`
- Source include path: `service.py` and `app/**/*.py|yaml|yml|sh`

## 6) Launch from zero (script-based)

Canonical runbook:

- `docs/runbook.md`

Practical sequence:

1. Configure `.env` (`REGISTRY_*`, `TU_ORG`, `MLFLOW_TRACKING_URI`, `K8S_NAMESPACE`, etc.).
2. Ensure registry secret:
   - `k8s/bootstrap/create_registry_secret.ps1`
3. Build/push base runtime image:
   - `k8s/bootstrap/build_model_base_image.ps1`
4. Build/push gateway + model-service images:
   - `k8s/build_images.ps1`
5. Deploy gateway:
   - `k8s/deploy_gateway.ps1`
6. Validate:
   - `kubectl rollout status deploy/gateway`
   - `kubectl get svc gateway -o wide`
   - `POST /health`
   - `POST /list_models`
7. Trigger model deployment through endpoint:
   - `POST /deploy_model`
   - then poll `POST /deploy_status`

Notes:

- If namespace and secret are already managed externally, steps 2 and namespace creation can be skipped.
- `deploy_gateway.ps1` imports `.env` automatically when present.
- Gateway exposure is configured as `Service.type=LoadBalancer`; the intended access path is external LB endpoint.

## 7) Verification status in repository

Latest local verification:

- unit tests: `28 passed`
- command: `python -m unittest discover -s gateway/tests -p "test_*.py" -t . -v`

This confirms internal contracts and module wiring. Cluster-level rollout still depends on registry, RBAC, and Kubernetes connectivity in your environment.
