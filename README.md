# NITRATE_model

REST API for serving MLflow models on Kubernetes using BentoML.

This repository is now focused on model serving infrastructure (gateway + per-model services), not on nitrate model research/training.

## Project structure

- `gateway/`: API gateway service that routes `/predict` requests by `model_name`.
- `model_service/`: Generic BentoML service that loads one MLflow model.
- `k8s/`: Deployment and image build scripts (`.sh` and `.ps1`).
- `scripts/`: Utilities for model registry and REST API validation.
- `examples/rest/`: Request payload samples.
- `docs/`: Workflow and operational notes.
- `legacy/nitrate_research/`: Archived research/training assets from the old nitrate-focused workflow.

## Quick start

1. Create local config from `.env_example` and set at least:
   - `MLFLOW_TRACKING_URI`
   - `K8S_NAMESPACE` (default: `api-models`)
2. Build images:
   - Bash: `./k8s/build_images.sh latest`
   - PowerShell: `.\\k8s\\build_images.ps1 -Tag latest`
3. Deploy gateway (once per namespace):
   - Bash: `./k8s/deploy_gateway.sh api-models`
   - PowerShell: `.\\k8s\\deploy_gateway.ps1 -Namespace api-models`
4. Deploy one model:
   - Bash: `./k8s/deploy_model.sh <MODEL_NAME> latest api-models 1`
   - PowerShell: `.\\k8s\\deploy_model.ps1 -ModelName <MODEL_NAME> -ModelVersion latest -Namespace api-models -Replicas 1`
5. Test REST API:
   - PowerShell smoke test: `.\\scripts\\test_models_rest_api.ps1 -GatewayUrl "http://localhost:8000"`

If `gateway` is not externally reachable, use:

```bash
kubectl -n api-models port-forward svc/gateway 8000:8000
```

## REST API example

```bash
curl -X POST "http://localhost:8000/predict" \\
  -H "Content-Type: application/json" \\
  -d '{
    "model_name": "tiny_demo_model",
    "data": {"features": [1.0, 2.0, 3.0]}
  }'
```

List discovered/configured models:

```bash
curl -X POST "http://localhost:8000/list_models" \\
  -H "Content-Type: application/json" \\
  -d '{}'
```

## Notes

- `MODEL_VERSION=latest` resolves to:
  1. Highest version in `Production` stage, if available.
  2. Otherwise highest numeric registered version.
- You can bypass registry version resolution with `MODEL_URI` (for example `runs:/<RUN_ID>/model`).

## Documentation

- Kubernetes + MLflow workflow (archived): `docs/history/mlflow_to_k8s_workflow.md`
- Kubernetes runbook (no `$env` commands): `docs/runbook.md`
- Script-only deployment flow: `k8s/build_images.*`, `k8s/deploy_gateway.*`, `k8s/deploy_model.*`
- Kubernetes YAML manifests are generated/applied by the scripts in `k8s/`.
- Change log and execution notes: `docs/history/cambios_k8s_2026-04-08.md`
