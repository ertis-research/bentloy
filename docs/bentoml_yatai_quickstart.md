# BentoML + Yatai Quickstart

## 1) Instalar dependencias del proyecto

Ejecuta:

uv sync

## 2) Entrenar y exportar artefactos

En el notebook [prueba_entrenamiento.ipynb](../prueba_entrenamiento.ipynb), ejecuta las celdas de entrenamiento y guardado para generar:

- test_models/prueba.h5
- test_models/scaler_prueba.pkl
- test_models/features_prueba.json

## 3) Probar el servicio BentoML en local

Lanzar servidor:

bentoml serve <modulo_servicio>:<ClaseServicio> --reload

Endpoint de predicción:

POST /predict

Payload de ejemplo:

{
  "samples": [
    [
      [0.12, 0.53, 0.74],
      [0.11, 0.51, 0.70],
      [0.10, 0.50, 0.69]
    ]
  ]
}

Nota: el último eje debe coincidir con el número real de features del modelo guardado.

## 4) Construir Bento

bentoml build

## 5) Flujo sin Docker local (Kubernetes/Yatai)

Si trabajas en un entorno sin Docker local, omite `bentoml containerize`.

Primero construye el Bento y copia el tag resultante del output:

bentoml build

Ejemplo de tag generado:

my_service:zqciuybbh6guwlwl

Luego súbelo al registry configurado para Bento/Yatai:

bentoml push my_service:zqciuybbh6guwlwl

Con eso, en Yatai crea el deployment apuntando a ese tag de Bento.

Nota: `:latest` no siempre existe. Es más seguro usar el tag exacto que devuelve `bentoml build`.

## 6) Empezar con Yatai (Kubernetes)

Yatai requiere clúster Kubernetes (local o remoto). Flujo mínimo:

1. Instalar Yatai en el clúster (Helm).
2. Registrar credenciales del registry para las imágenes Bento.
3. Hacer deploy del Bento desde Yatai con autoscaling y métricas.

Comandos de referencia (ejemplo, ajústalos a tu entorno):

helm repo add bentoml https://bentoml.github.io/helm-charts
helm repo update
helm install yatai bentoml/yatai

Después, crea un Deployment en Yatai apuntando al Bento tag que subiste (por ejemplo `my_service:zqciuybbh6guwlwl`).

## 7) Recomendación práctica para siguiente paso

Antes de ir a producción en Yatai, valida en local:

- latencia p95
- throughput
- consistencia de escalado y orden de features
- estrategia de versionado de modelos
