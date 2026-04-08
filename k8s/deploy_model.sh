#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "Usage: ./k8s/deploy_model.sh <MODEL_NAME> [MODEL_VERSION] [NAMESPACE] [REPLICAS]"
  exit 1
fi

MODEL_NAME="$1"
MODEL_VERSION="${2:-latest}"
NAMESPACE="${3:-${K8S_NAMESPACE:-ml-serving}}"
REPLICAS="${4:-1}"

if [[ -f .env ]]; then
  # shellcheck disable=SC1091
  source .env
fi

MODEL_SERVICE_IMAGE="${MODEL_SERVICE_IMAGE:-model-service:latest}"
MODEL_SERVICE_PREFIX="${MODEL_SERVICE_PREFIX:-model}"
MLFLOW_TRACKING_URI="${MLFLOW_TRACKING_URI:-}"

if [[ -z "$MLFLOW_TRACKING_URI" ]]; then
  echo "Error: MLFLOW_TRACKING_URI is not set (in .env or environment)."
  exit 1
fi

if ! command -v kubectl >/dev/null 2>&1; then
  echo "Error: kubectl is not installed or not in PATH."
  exit 1
fi

to_slug() {
  echo "$1" \
    | tr '[:upper:]' '[:lower:]' \
    | tr '_' '-' \
    | sed -E 's/[^a-z0-9-]+/-/g; s/^-+//; s/-+$//; s/-+/-/g'
}

MODEL_SLUG="$(to_slug "$MODEL_NAME")"
DEPLOY_NAME="${MODEL_SERVICE_PREFIX}-${MODEL_SLUG}"
SERVICE_NAME="${MODEL_SERVICE_PREFIX}-${MODEL_SLUG}"

kubectl get namespace "$NAMESPACE" >/dev/null 2>&1 || kubectl create namespace "$NAMESPACE"

cat <<EOF | kubectl apply -f -
apiVersion: apps/v1
kind: Deployment
metadata:
  name: ${DEPLOY_NAME}
  namespace: ${NAMESPACE}
  labels:
    app: model-service
    model-slug: ${MODEL_SLUG}
spec:
  replicas: ${REPLICAS}
  selector:
    matchLabels:
      app: model-service
      model-slug: ${MODEL_SLUG}
  template:
    metadata:
      labels:
        app: model-service
        model-slug: ${MODEL_SLUG}
    spec:
      containers:
        - name: model-service
          image: ${MODEL_SERVICE_IMAGE}
          imagePullPolicy: IfNotPresent
          ports:
            - containerPort: 3000
          env:
            - name: MLFLOW_TRACKING_URI
              value: "${MLFLOW_TRACKING_URI}"
            - name: MODEL_NAME
              value: "${MODEL_NAME}"
            - name: MODEL_VERSION
              value: "${MODEL_VERSION}"
          readinessProbe:
            httpGet:
              path: /health
              port: 3000
            initialDelaySeconds: 10
            periodSeconds: 10
          livenessProbe:
            httpGet:
              path: /health
              port: 3000
            initialDelaySeconds: 20
            periodSeconds: 15
---
apiVersion: v1
kind: Service
metadata:
  name: ${SERVICE_NAME}
  namespace: ${NAMESPACE}
  labels:
    app: model-service
    model-slug: ${MODEL_SLUG}
spec:
  type: ClusterIP
  selector:
    app: model-service
    model-slug: ${MODEL_SLUG}
  ports:
    - name: http
      port: 3000
      targetPort: 3000
EOF

echo "Model deployed: ${MODEL_NAME} (${MODEL_VERSION})"
echo "Namespace: ${NAMESPACE}"
echo "Internal service URL: http://${SERVICE_NAME}.${NAMESPACE}.svc.cluster.local:3000"
echo "Call through gateway with model_name=${MODEL_NAME}"
