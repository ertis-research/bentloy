# Runbook: MLflow Models on Kubernetes

Updated: 2026-04-16

This runbook describes the operational flow currently implemented in this repository.

Technical reference:

- `docs/gateway_technical.md`

## 1) Prerequisites

| Component | Required | Notes |
|---|---|---|
| Kubernetes access (`kubectl`) | Yes | Correct context and RBAC permissions |
| Docker | Yes | Required for local shared image builds |
| Registry credentials | Yes | For push/pull (`REGISTRY_USERNAME`, `REGISTRY_PASSWORD`) |
| MLflow Tracking URI | Yes | Needed to resolve/download model artifacts |
| PowerShell or Bash | Yes | Both script variants are available |

## 2) Environment configuration (`.env`)

Use `.env_example` as template. Minimum recommended configuration:

```env
MLFLOW_TRACKING_URI=http://mlflow-host:5000
K8S_NAMESPACE=api-models
MODEL_SERVICE_PREFIX=model
MODEL_URL_TEMPLATE=http://{service_name}:3000
GATEWAY_PORT=8000

REGISTRY_SERVER=registry.example.com
TU_ORG=my-project
REGISTRY_USERNAME=YOUR_REGISTRY_USER
REGISTRY_PASSWORD=YOUR_REGISTRY_TOKEN_OR_PASSWORD
MODEL_REGISTRY_SECRET=model-registry-creds

MODEL_BASE_IMAGE=registry.example.com/my-project/model-service-base:py310
KANIKO_SKIP_TLS_VERIFY=1

AUTO_DEPLOY_ENABLED=1
DEPLOY_BUILD_TIMEOUT_SEC=1800
DEPLOY_ROLLOUT_TIMEOUT_SEC=300

GATEWAY_IMAGE=registry.example.com/my-project/model-gateway:20260416-01
MODEL_SERVICE_IMAGE=registry.example.com/my-project/model-service:20260416-01
```

Notes:

1. `REGISTRY_SERVER` must be only the registry host.
2. Keep image tags aligned with your latest `build_images` execution.
3. Use `KANIKO_SKIP_TLS_VERIFY=1` only when registry trust is not configured.

## 3) From zero: bootstrap and gateway

### 3.1 Registry login

PowerShell/Bash:

```bash
docker login <REGISTRY_SERVER>
```

### 3.2 Create/update image pull secret

PowerShell:

```powershell
.\k8s\bootstrap\create_registry_secret.ps1 -Namespace api-models -SecretName model-registry-creds
```

Bash:

```bash
./k8s/bootstrap/create_registry_secret.sh api-models model-registry-creds
```

### 3.3 Build and push base image

PowerShell:

```powershell
.\k8s\bootstrap\build_model_base_image.ps1 -Org <TU_ORG> -Tag py310
```

Bash:

```bash
./k8s/bootstrap/build_model_base_image.sh <TU_ORG> py310
```

### 3.4 Build and push shared images (`gateway`, `model_service`)

PowerShell:

```powershell
.\k8s\build_images.ps1 -Tag 20260416-01 -RegistryPrefix <REGISTRY_SERVER>/<TU_ORG>
```

Bash:

```bash
./k8s/build_images.sh 20260416-01 <REGISTRY_SERVER>/<TU_ORG>
```

These scripts are Dockerfile-only for shared images.  
If neither source code nor dependencies changed in `gateway/` and `model_service/`, you can reuse previously published tags and skip this step.

### 3.5 Deploy gateway

PowerShell:

```powershell
.\k8s\deploy_gateway.ps1 -Namespace api-models
kubectl -n api-models rollout status deploy/gateway --timeout=180s
kubectl -n api-models get deploy,svc,pods
```

Bash:

```bash
./k8s/deploy_gateway.sh api-models
kubectl -n api-models rollout status deploy/gateway --timeout=180s
kubectl -n api-models get deploy,svc,pods
```

The script prints `Gateway URL` when LoadBalancer ingress becomes available.

## 4) Deploy one model (image-per-model)

### 4.1 Build model image inside cluster (Kaniko Job)

PowerShell:

```powershell
.\k8s\build_model_image_job.ps1 -ModelName tiny_demo_model -ModelVersion latest -Namespace api-models -Wait -InsecureRegistryTls
```

Bash:

```bash
./k8s/build_model_image_job.sh --model-name tiny_demo_model --model-version latest --namespace api-models --wait --insecure-registry-tls
```

Copy the `Target image:` from output.

### 4.2 Deploy the generated model image

PowerShell:

```powershell
.\k8s\deploy_model_image.ps1 -ModelName tiny_demo_model -ModelImage <REGISTRY_SERVER>/<TU_ORG>/model-tiny-demo-model:<tag> -Namespace api-models -Replicas 1 -DeployedName tiny_demo_model_v2 -AppendDate
kubectl -n api-models get deploy | findstr model-
```

Bash:

```bash
./k8s/deploy_model_image.sh tiny_demo_model <REGISTRY_SERVER>/<TU_ORG>/model-tiny-demo-model:<tag> api-models 1 tiny_demo_model_v2 true
kubectl -n api-models get deploy | grep model-
```

Notes:

1. `ModelName` is still the MLflow source model.
2. `DeployedName` controls Kubernetes resource naming and the `model_name` you call in gateway `/predict`.
3. `AppendDate` appends `YYYYMMDD` to `DeployedName`.

## 5) Deploy by gateway endpoint (async)

### 5.1 Start deploy

```powershell
$gatewayUrl = "http://<gateway-ip-or-host>:8000"
$body = @{
  model_name = "tiny_demo_model_2"
  model_version = "latest"
  namespace = "api-models"
  replicas = 1
  tracking_uri = "http://mlflow-host:5000"
  insecure_registry_tls = $true
  deployed_name = "tiny_demo_model_prod"
  append_date = $true
} | ConvertTo-Json

$result = Invoke-RestMethod -Method Post -Uri "$gatewayUrl/deploy_model" -ContentType "application/json" -Body $body
$result
```

### 5.2 Poll status

```powershell
$operationId = $result.operation_id
do {
  $statusBody = @{ operation_id = $operationId } | ConvertTo-Json
  $status = Invoke-RestMethod -Method Post -Uri "$gatewayUrl/deploy_status" -ContentType "application/json" -Body $statusBody
  $status
  Start-Sleep -Seconds 3
} while ($status.status -in @("queued", "building", "pushing", "deploying"))
```

Expected terminal statuses:

1. `ready`
2. `failed`

When `deployed_name` is set (or `append_date=true`), call `/predict` using that deployed alias.

## 6) API verification

### 6.1 List models

```bash
curl -X POST "<gateway_url>/list_models" -H "Content-Type: application/json" -d "{}"
```

### 6.2 Predict

```bash
curl -X POST "<gateway_url>/predict" \
  -H "Content-Type: application/json" \
  -d "{\"model_name\":\"tiny_demo_model\",\"data\":{\"features\":[1.0,2.0,3.0]}}"
```

## 7) What to rerun and when

| Change | Repeat step |
|---|---|
| Registry credentials changed | `create_registry_secret.*` |
| Base runtime dependencies changed | `build_model_base_image.*` |
| Gateway/model_service source changed | `build_images.*` + `deploy_gateway.*` |
| New model only | `build_model_image_job.*` + `deploy_model_image.*` |
| Cluster/namespace recreated | Full bootstrap from section 3 |

## 8) Troubleshooting

### 8.1 `list_models` empty

1. Check model service name prefix (`model-...`).
2. Check namespace (`MODEL_NAMESPACE` in gateway env).
3. Check gateway RBAC can list services/pods.

### 8.2 `Model not available` in `/predict`

1. Confirm model deployment is `Ready`.
2. Confirm service exists and answers `/predict`.
3. Check gateway routing target in `/list_models`.

### 8.3 Shared image build strategy

`k8s/build_images.*` builds shared images directly from Dockerfiles (no Bento fallback path).

### 8.4 Kaniko TLS / registry errors

1. For self-signed registries, keep `KANIKO_SKIP_TLS_VERIFY=1`.
2. Ensure `MODEL_REGISTRY_SECRET` exists in the target namespace.
3. Verify push permissions for `<REGISTRY_SERVER>/<TU_ORG>`.
