# MLflow -> Deploy -> REST Gateway (Kubernetes)

Este flujo permite:

1. entrenar y registrar modelos en MLflow,
2. desplegar un modelo concreto con un comando,
3. consultar cualquier modelo desplegado por REST usando `model_name`.

Nota: este flujo es Kubernetes-only (no usa Docker Compose).

## PowerShell (Windows)

Si no puedes ejecutar `.sh`, usa estos equivalentes `.ps1`:

```powershell
.\k8s\build_images.ps1 -Tag latest
.\k8s\deploy_gateway.ps1 -Namespace api-models
.\k8s\deploy_model.ps1 -ModelName my_model -ModelVersion latest -Namespace api-models -Replicas 1
.\scripts\register_model_from_run.ps1 -ModelName my_model -RunId <RUN_ID> -ArtifactPath model -TargetStage Production
```

Si PowerShell bloquea scripts locales:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
```

## 1) Prerequisitos

- Cluster Kubernetes operativo.
- `kubectl` configurado contra el cluster.
- Docker disponible para crear imagenes.
- MLflow accesible desde los pods (`MLFLOW_TRACKING_URI`).

## 2) Construir imagenes (gateway + model service)

Desde la raiz del repo:

```bash
./k8s/build_images.sh latest
```

Si necesitas subir a registry remoto:

```bash
./k8s/build_images.sh v1 ghcr.io/TU_ORG
export GATEWAY_IMAGE=ghcr.io/TU_ORG/model-gateway:v1
export MODEL_SERVICE_IMAGE=ghcr.io/TU_ORG/model-service:v1
```

## 3) Desplegar gateway una sola vez (una vez hecho por primera vez, ya no hace falta hacerlo nunca más)

```bash
./k8s/deploy_gateway.sh api-models
```

Si no tienes LoadBalancer publico:

```bash
kubectl -n api-models port-forward svc/gateway 8000:8000
```

## 4) Registrar modelo ganador en MLflow Registry (opcional)

Si tienes un `run_id` y quieres promoverlo a `Production`:

```bash
./scripts/register_model_from_run.sh my_model <RUN_ID> model Production
```

## 5) Desplegar un modelo desde MLflow

```bash
./k8s/deploy_model.sh my_model latest api-models 1
```

`latest` se resuelve asi en `model_service`:

1. usa la ultima version en stage `Production` si existe,
2. si no existe, usa la mayor version numerica registrada (fallback).

Para una version concreta:

```bash
./k8s/deploy_model.sh my_model 7 api-models 1
```

Tambien puedes forzar una URI directa de MLflow (sin pasar por version):

```bash
export MODEL_URI=runs:/<RUN_ID>/model
./k8s/deploy_model.sh my_model latest api-models 1
```

## 6) Probar funcionamiento ahora (E2E)

1. Verifica que gateway y modelo estan `Ready`:

```bash
kubectl -n api-models rollout status deploy/gateway --timeout=180s
kubectl -n api-models rollout status deploy/model-my-model --timeout=300s
kubectl -n api-models get deploy,svc,pods
```

2. Si no tienes acceso directo al Service `gateway`, abre `port-forward`:

```bash
kubectl -n api-models port-forward svc/gateway 8000:8000
```

3. Prueba readiness/liveness del gateway:

```bash
curl -s http://localhost:8000/readyz
curl -s http://localhost:8000/livez
```

4. Comprueba descubrimiento de modelos con `/list_models`:

```bash
curl -X POST "http://localhost:8000/list_models" \
  -H "Content-Type: application/json" \
  -d '{}'
```

El resultado combina:

- `configured_models`: modelos definidos por variables `MODELO_*_URL`
- `recently_routed_models`: modelos vistos en llamadas previas
- `discovered_models`: servicios detectados en Kubernetes con prefijo `model-`
- `endpoints`: mapa de endpoint `/predict` por modelo inferido

Si `discovery.status=error` con `403 Forbidden`, vuelve a desplegar gateway para aplicar RBAC:

```bash
./k8s/deploy_gateway.sh api-models
```

5. Lanza una prediccion real contra el gateway:

```bash
curl -X POST "http://localhost:8000/predict" \
  -H "Content-Type: application/json" \
  -d '{
    "model_name": "my_model",
    "data": {
      "features": [1.0, 2.0, 3.0]
    }
  }'
```

Nota importante para `nitrate_nowcasting_best_01`:

- Este modelo concreto espera una secuencia con forma `(24, 12)` por muestra.
- Un payload tipo `features: [1.0, 2.0, 3.0]` devolvera error de shape.

Ejemplo rapido en PowerShell (genera una matriz 24x12):

```powershell
$matrix = @()
for ($i = 0; $i -lt 24; $i++) {
  $row = @()
  for ($j = 0; $j -lt 12; $j++) { $row += 1.0 }
  $matrix += ,$row
}
$payload = @{
  model_name = "nitrate_nowcasting_best_01"
  data = @{ features = $matrix }
} | ConvertTo-Json -Depth 12

Invoke-RestMethod -Method Post -Uri "http://localhost:8000/predict" -ContentType "application/json" -Body $payload
```

6. Verifica en logs que el modelo se cargo correctamente:

```bash
kubectl -n api-models logs deploy/model-my-model --tail=120
```

Busca lineas tipo:

- `Loading model from: models:/my_model/<version>`
- `Model loaded`

## 7) Consumir por API REST

Llamada al gateway:

```bash
curl -X POST "http://localhost:8000/predict" \
  -H "Content-Type: application/json" \
  -d '{
    "model_name": "my_model",
    "data": {
      "features": [1.0, 2.0, 3.0]
    }
  }'
```

## 8) Actualizar o escalar un modelo

Actualizar version del modelo:

```bash
./k8s/deploy_model.sh my_model 8 api-models 1
```

Escalar replicas:

```bash
./k8s/deploy_model.sh my_model latest api-models 3
```

## 9) Comprobaciones rapidas

```bash
kubectl -n api-models get deploy,svc,pods
kubectl -n api-models logs deploy/gateway --tail=100
kubectl -n api-models logs deploy/model-my-model --tail=100
```

Nota: los probes de Kubernetes usan endpoints internos de Bento:

- readiness: `/readyz`
- liveness: `/livez`

## 10) Como obtener RUN_ID

Opcion A (rapida, desde MLflow UI):

1. Abre la UI de MLflow.
2. Entra al experimento/run.
3. Copia el `Run ID`.

Opcion B (API, desde un modelo registrado):

```bash
curl -s "$MLFLOW_TRACKING_URI/api/2.0/mlflow/registered-models/get-latest-versions?name=my_model" \
| jq -r '.model_versions[] | "version=\(.version) stage=\(.current_stage) run_id=\(.run_id)"'
```

Opcion C (API, ultimos runs de un experimento):

```bash
curl -s -X POST "$MLFLOW_TRACKING_URI/api/2.0/mlflow/runs/search" \
  -H "Content-Type: application/json" \
  -d '{"experiment_ids":["0"],"max_results":20,"order_by":["attributes.start_time DESC"]}' \
| jq -r '.runs[] | .info.run_id'
```

Con ese `RUN_ID` puedes:

```bash
./scripts/register_model_from_run.sh my_model <RUN_ID> model Production
```

o usar URI directa:

```bash
export MODEL_URI=runs:/<RUN_ID>/model
./k8s/deploy_model.sh my_model latest api-models 1
```

## 11) Como obtener TU_ORG (para ghcr.io/TU_ORG)

`TU_ORG` es el owner en GitHub donde vas a publicar imagenes (`usuario` u `organizacion`).

Recomendado (tomar owner del repo actual):

```bash
gh repo view --json owner -q .owner.login
```

Alternativas:

```bash
gh api user -q .login
git remote get-url origin
```

Uso tipico:

```bash
export TU_ORG=$(gh repo view --json owner -q .owner.login)
./k8s/build_images.sh v1 ghcr.io/$TU_ORG
export GATEWAY_IMAGE=ghcr.io/$TU_ORG/model-gateway:v1
export MODEL_SERVICE_IMAGE=ghcr.io/$TU_ORG/model-service:v1
```

## 12) Script de prueba REST (recomendado)

Se incluye:

- `scripts/test_models_rest_api.ps1`

Hace:

1. `GET /readyz`
2. `GET /livez`
3. `POST /list_models`
4. opcionalmente `POST /predict`

Uso rapido:

```powershell
.\scripts\test_models_rest_api.ps1 -GatewayUrl "http://localhost:8000"
```

Con prediccion:

```powershell
.\scripts\test_models_rest_api.ps1 `
  -GatewayUrl "http://localhost:8000" `
  -ModelName "nitrate_nowcasting_best_01" `
  -PayloadPath ".\\examples\\rest\\nitrate_24x12_data.json"
```

Payload demo para modelo tabular pequeno:

- `examples/rest/tiny_model_data.json`

## 13) Crear y subir un modelo pequeno y sencillo (demo)

Se incluye:

- `scripts/create_tiny_mlflow_model.py`

El script:

1. entrena una regresion lineal minima (3 features),
2. guarda el modelo en un run de MLflow,
3. lo registra en Model Registry (por defecto `tiny_demo_model`),
4. lo mueve a `Production` (por defecto).

Ejemplo:

```powershell
.\.venv\Scripts\python.exe .\scripts\create_tiny_mlflow_model.py `
  --tracking-uri "http://mlflow.example.com:5000" `
  --experiment-name "tiny-model-demo" `
  --registered-model-name "tiny_demo_model" `
  --stage "Production"
```

Luego desplegar:

```bash
./k8s/deploy_model.sh tiny_demo_model latest api-models 1
```

Y probar por gateway:

```powershell
.\scripts\test_models_rest_api.ps1 `
  -GatewayUrl "http://localhost:8000" `
  -ModelName "tiny_demo_model" `
  -PayloadPath ".\\examples\\rest\\tiny_model_data.json"
```
