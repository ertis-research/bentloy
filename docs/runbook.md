# Runbook: Kubernetes Launch Without `$env` Commands

Validated on 2026-04-13: gateway deployment and endpoint-based model deployment are working from `localhost`.

This runbook explains how to launch the project end-to-end using `.env` plus explicit script parameters, without setting `$env:*` variables in PowerShell commands.

## 1) `.env` configuration

Use this as a base and replace placeholder values:

```env
MLFLOW_TRACKING_URI=http://mlflow.example.com:5000
K8S_NAMESPACE=api-models
MODEL_SERVICE_PREFIX=model
MODEL_URL_TEMPLATE=http://{service_name}:3000
GATEWAY_PORT=8000

REGISTRY_SERVER=registry.example.com
TU_ORG=api-models
REGISTRY_USERNAME=YOUR_REGISTRY_USER
REGISTRY_PASSWORD=YOUR_REGISTRY_TOKEN_OR_PASSWORD
MODEL_REGISTRY_SECRET=model-registry-creds

MODEL_BASE_IMAGE=registry.example.com/api-models/model-service-base:py310
KANIKO_SKIP_TLS_VERIFY=1

AUTO_DEPLOY_ENABLED=1
DEPLOY_BUILD_TIMEOUT_SEC=1800
DEPLOY_ROLLOUT_TIMEOUT_SEC=300

GATEWAY_IMAGE=registry.example.com/api-models/model-gateway:20260413-01
MODEL_SERVICE_IMAGE=registry.example.com/api-models/model-service:20260413-01
```

Notes:

1. `REGISTRY_SERVER` must be the registry host, not a UI path.
2. Keep `GATEWAY_IMAGE` and `MODEL_SERVICE_IMAGE` aligned with the tag built in step 5.
3. If your registry has a self-signed certificate, keep `KANIKO_SKIP_TLS_VERIFY=1`.

## 2) First launch from zero

Run from repository root:

1. Allow local script execution for current shell:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
```

2. Login to the registry:

```powershell
docker login registry.example.com
```

3. Create/update registry secret in Kubernetes:

```powershell
.\k8s\bootstrap\create_registry_secret.ps1 -Namespace api-models -SecretName model-registry-creds
```

4. Build and push base runtime image:

```powershell
.\k8s\bootstrap\build_model_base_image.ps1 -Org api-models -Tag py310
```

5. Build and push gateway + model-service images with a unique tag:

```powershell
.\k8s\build_images.ps1 -Tag 20260413-01 -RegistryPrefix registry.example.com/api-models
```

6. Deploy gateway:

```powershell
.\k8s\deploy_gateway.ps1 -Namespace api-models
```

7. Validate gateway rollout:

```powershell
kubectl -n api-models rollout status deploy/gateway --timeout=180s
kubectl -n api-models get deploy,svc,pods
```

8. Open local access to gateway:

```powershell
kubectl -n api-models port-forward svc/gateway 8000:8000
```

## 3) Deploy one model (one image per model)

1. Build model image in-cluster with Job:

```powershell
.\k8s\build_model_image_job.ps1 -ModelName tiny_demo_model -ModelVersion latest -Namespace api-models -Wait -InsecureRegistryTls
```

2. Copy `Target image:` value from previous command output, then deploy:

```powershell
.\k8s\deploy_model_image.ps1 -ModelName tiny_demo_model -ModelImage registry.example.com/api-models/model-tiny-demo-model:vlatest-t20260413143000 -Namespace api-models -Replicas 1
```

3. Validate model rollout:

```powershell
kubectl -n api-models rollout status deploy/model-tiny-demo-model --timeout=300s
kubectl -n api-models get deploy,svc,pods
```

## 4) Endpoint-based deployment from `localhost`

With `port-forward` active:

```powershell
kubectl -n api-models port-forward svc/gateway 8000:8000
```

1. Start asynchronous deployment:

```powershell
$body = @{
  model_name = "tiny_demo_model_2"
  model_version = "latest"
  namespace = "api-models"
  replicas = 1
  tracking_uri = "http://mlflow.example.com:5000"
  insecure_registry_tls = $false
} | ConvertTo-Json

$result = Invoke-RestMethod -Method Post -Uri "http://localhost:8000/deploy_model" -ContentType "application/json" -Body $body
$result
```

2. Manual status check:

```powershell
$statusBody = @{ operation_id = $result.operation_id } | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri "http://localhost:8000/deploy_status" -ContentType "application/json" -Body $statusBody
```

3. Automatic status watcher with progress bar:

```powershell
.\scripts\watch_deploy_status.ps1 -OperationId $result.operation_id -GatewayUrl "http://localhost:8000"
```

The watcher uses a progress bar format:

```python
bar = "█" * bar_filled + "░" * (20 - bar_filled)
```

and increasing polling interval (backoff) to reduce request frequency.

## 5) REST API validation

With `port-forward` active:

1. List models:

```powershell
curl -X POST "http://localhost:8000/list_models" -H "Content-Type: application/json" -d "{}"
```

2. Predict:

```powershell
curl -X POST "http://localhost:8000/predict" -H "Content-Type: application/json" -d "{\"model_name\":\"tiny_demo_model\",\"data\":{\"features\":[1.0,2.0,3.0]}}"
```

## 6) Deploy a second model

You do not need to repeat full bootstrap each time.

Repeat only:

1. `build_model_image_job.ps1` for the new model name/version.
2. `deploy_model_image.ps1` with the new generated target image.
3. `rollout status` and API test.

Use bootstrap steps again only when:

1. You changed gateway/model-service code and need fresh shared images.
2. You changed base runtime dependencies and need a new base image.
3. You recreated namespace/cluster and lost secrets/resources.

## 7) Practical test examples

### Example A: full flow with endpoint + watcher

1. Start deploy:

```powershell
$deployBody = @{
  model_name = "tiny_demo_model_2"
  model_version = "latest"
  namespace = "api-models"
  replicas = 1
  tracking_uri = "http://mlflow.example.com:5000"
  insecure_registry_tls = $false
} | ConvertTo-Json

$deployResult = Invoke-RestMethod -Method Post -Uri "http://localhost:8000/deploy_model" -ContentType "application/json" -Body $deployBody
```

2. Watch until `ready`:

```powershell
.\scripts\watch_deploy_status.ps1 -OperationId $deployResult.operation_id -GatewayUrl "http://localhost:8000"
```

3. Test prediction:

```powershell
$predictBody = @{
  model_name = "tiny_demo_model_2"
  data = @{ features = @(1.0, 2.0, 3.0) }
} | ConvertTo-Json -Depth 5

Invoke-RestMethod -Method Post -Uri "http://localhost:8000/predict" -ContentType "application/json" -Body $predictBody
```

### Example B: check discoverability and endpoints

```powershell
Invoke-RestMethod -Method Post -Uri "http://localhost:8000/list_models" -ContentType "application/json" -Body "{}"
```

Confirm:

1. `discovered_models` includes the deployed model service.
2. `endpoints` includes `<model_name> -> /predict`.
3. `deploy_operations` includes the latest `operation_id`.

### Example C: route prediction to an external URL

This bypasses local service discovery and forces routing to `model_url`.

```powershell
$externalPredictBody = @{
  model_name = "remote_demo_model"
  model_url = "http://external-model-host:3000"
  data = @{ features = @(1.0, 2.0, 3.0) }
} | ConvertTo-Json -Depth 6

Invoke-RestMethod -Method Post -Uri "http://localhost:8000/predict" -ContentType "application/json" -Body $externalPredictBody
```

Batch with mixed local + external routing:

```powershell
$batchBody = @{
  requests = @(
    @{
      model_name = "tiny_demo_model_2"
      data = @{ features = @(1.0, 2.0, 3.0) }
    },
    @{
      model_name = "remote_demo_model"
      model_url = "http://external-model-host:3000"
      data = @{ features = @(4.0, 5.0, 6.0) }
    }
  )
} | ConvertTo-Json -Depth 8

Invoke-RestMethod -Method Post -Uri "http://localhost:8000/batch_predict" -ContentType "application/json" -Body $batchBody
```
