# MLflow -> Deploy -> REST Gateway (Kubernetes)

Este flujo permite:

1. entrenar y registrar modelos en MLflow,
2. desplegar un modelo concreto con un comando,
3. consultar cualquier modelo desplegado por REST usando `model_name`.

Nota: este flujo es Kubernetes-only (no usa Docker Compose).

## 1) Prerequisitos

- Cluster Kubernetes operativo.
- `kubectl` configurado contra el cluster.
- `bentoml` disponible (o `uv run bentoml`).
- Docker disponible para `bentoml containerize`.
- MLflow accesible desde los pods (`MLFLOW_TRACKING_URI`).

## 2) Construir imagenes (gateway + model service)

Este paso usa BentoML (no `docker build` manual):

- `bentoml build` para empaquetar cada servicio.
- `bentoml containerize` para crear la imagen OCI.

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

## 3) Desplegar gateway una sola vez

```bash
./k8s/deploy_gateway.sh api-model
```

Si no tienes LoadBalancer publico:

```bash
kubectl -n api-model port-forward svc/gateway 8000:8000
```

## 4) Registrar modelo ganador en MLflow Registry (opcional pero recomendado)

Si tienes un `run_id` de entrenamiento y quieres promoverlo a `Production`:

```bash
./scripts/register_model_from_run.sh my_model <RUN_ID> model Production
```

Esto deja disponible `my_model` para resolver `latest` en despliegue.

## 5) Desplegar un modelo desde MLflow Registry

```bash
# latest = stage Production en model_service
./k8s/deploy_model.sh my_model latest api-model 1
```

Para una version concreta:

```bash
./k8s/deploy_model.sh my_model 7 api-model 1
```

Nota: el gateway y los deployments de modelos comparten la variable `MODEL_SERVICE_PREFIX`.
Mientras mantengas la misma convención en ambos, todos los modelos desplegados quedarán accesibles desde la misma API REST del gateway usando `model_name`.

## 6) Consumir por API REST

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

El gateway resuelve dinamicamente el destino usando la convencion:

- Deployment/Service del modelo: `model-<model_slug>`
- `model_slug`: minusculas y guiones (ejemplo `Fraud_Model_v2` -> `fraud-model-v2`)

## 7) Actualizar o escalar un modelo

Actualizar version del modelo:

```bash
./k8s/deploy_model.sh my_model 8 api-model 1
```

Escalar replicas:

```bash
./k8s/deploy_model.sh my_model latest api-model 3
```

## 8) Comprobaciones rapidas

```bash
kubectl -n api-model get deploy,svc,pods
kubectl -n api-model logs deploy/gateway --tail=100
kubectl -n api-model logs deploy/model-my-model --tail=100
```
