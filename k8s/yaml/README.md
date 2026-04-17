# YAML Gateway Deployment

Use this file directly with Kubernetes:

- `k8s/yaml/gateway.yaml`

No `envsubst`, no Compose, no extra tooling.

## Manual values to review in `gateway.yaml`

1. `metadata.namespace` (default: `api-models`)
2. `spec.template.spec.containers[0].image` (default: `model-gateway:latest`)
3. `imagePullSecrets.name` (default: `model-registry-creds`)
4. `MLFLOW_TRACKING_URI` (default: `http://mlflow:5000`)
5. `REGISTRY_SERVER`, `TU_ORG`, `MODEL_BASE_IMAGE` if your deploy flow needs them
6. Service port (default: `8000`)

## Deploy

```bash
kubectl apply -f k8s/yaml/gateway.yaml
```

## Model Service Templates

`k8s/deploy_model.ps1` and `k8s/deploy_model.sh` now render model manifests from:

- `k8s/yaml/model_service/deployment.yaml`
- `k8s/yaml/model_service/service.yaml`

These files are templates with placeholders, not static manifests to apply directly.

## Model Image Build Job Template

`k8s/build_model_image_job.ps1` and `k8s/build_model_image_job.sh` now render the Job manifest from:

- `k8s/yaml/model_image_job/job.yaml`

This file is a template with placeholders, not a static manifest to apply directly.

## Validate

```bash
kubectl -n api-models rollout status deploy/gateway --timeout=180s
kubectl -n api-models get deploy,svc,pods
kubectl -n api-models get svc gateway -o wide
```
