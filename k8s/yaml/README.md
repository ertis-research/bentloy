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

## Validate

```bash
kubectl -n api-models rollout status deploy/gateway --timeout=180s
kubectl -n api-models get deploy,svc,pods
```
