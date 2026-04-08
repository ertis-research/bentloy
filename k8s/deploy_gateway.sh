#!/usr/bin/env bash
set -euo pipefail

NAMESPACE="${1:-${K8S_NAMESPACE:-ml-serving}}"

if [[ -f .env ]]; then
  # shellcheck disable=SC1091
  source .env
fi

GATEWAY_IMAGE="${GATEWAY_IMAGE:-model-gateway:latest}"
GATEWAY_PORT="${GATEWAY_PORT:-8000}"
MODEL_SERVICE_PREFIX="${MODEL_SERVICE_PREFIX:-model}"
MODEL_URL_TEMPLATE="${MODEL_URL_TEMPLATE:-http://{service_name}:3000}"

if ! command -v kubectl >/dev/null 2>&1; then
  echo "Error: kubectl is not installed or not in PATH."
  exit 1
fi

kubectl get namespace "$NAMESPACE" >/dev/null 2>&1 || kubectl create namespace "$NAMESPACE"

cat <<EOF | kubectl apply -f -
apiVersion: apps/v1
kind: Deployment
metadata:
  name: gateway
  namespace: ${NAMESPACE}
  labels:
    app: gateway
spec:
  replicas: 1
  selector:
    matchLabels:
      app: gateway
  template:
    metadata:
      labels:
        app: gateway
    spec:
      containers:
        - name: gateway
          image: ${GATEWAY_IMAGE}
          imagePullPolicy: IfNotPresent
          ports:
            - containerPort: 3000
          env:
            - name: MODEL_SERVICE_PREFIX
              value: "${MODEL_SERVICE_PREFIX}"
            - name: MODEL_NAMESPACE
              value: "${NAMESPACE}"
            - name: MODEL_URL_TEMPLATE
              value: "${MODEL_URL_TEMPLATE}"
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
  name: gateway
  namespace: ${NAMESPACE}
  labels:
    app: gateway
spec:
  type: LoadBalancer
  selector:
    app: gateway
  ports:
    - name: http
      port: ${GATEWAY_PORT}
      targetPort: 3000
EOF

echo "Gateway deployed in namespace ${NAMESPACE}."
echo "If LoadBalancer is pending, use port-forward:"
echo "kubectl -n ${NAMESPACE} port-forward svc/gateway 8000:${GATEWAY_PORT}"
