#!/usr/bin/env bash
set -euo pipefail

CLI_NAMESPACE="${1:-}"
NAMESPACE="${CLI_NAMESPACE:-${K8S_NAMESPACE:-api-models}}"

import_dotenv_without_override() {
  local dotenv_path="${1:-.env}"
  [[ -f "$dotenv_path" ]] || return 0

  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line%$'\r'}"
    line="${line#$'\xEF\xBB\xBF'}"
    line="${line#"${line%%[![:space:]]*}"}"
    line="${line%"${line##*[![:space:]]}"}"
    [[ -z "$line" || "${line:0:1}" == "#" ]] && continue
    [[ "$line" == *=* ]] || continue

    local key="${line%%=*}"
    local value="${line#*=}"
    key="${key#"${key%%[![:space:]]*}"}"
    key="${key%"${key##*[![:space:]]}"}"
    value="${value#"${value%%[![:space:]]*}"}"
    value="${value%"${value##*[![:space:]]}"}"

    if [[ "$value" =~ ^\".*\"$ || "$value" =~ ^\'.*\'$ ]]; then
      value="${value:1:${#value}-2}"
    fi

    [[ "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue

    if [[ -z "${!key-}" ]]; then
      export "$key=$value"
    fi
  done < "$dotenv_path"
}

import_dotenv_without_override ".env"

# Resolve namespace after loading .env (arg has priority).
NAMESPACE="${CLI_NAMESPACE:-${K8S_NAMESPACE:-api-models}}"

GATEWAY_IMAGE="${GATEWAY_IMAGE:-model-gateway:latest}"
GATEWAY_PORT="${GATEWAY_PORT:-8000}"
MODEL_SERVICE_PREFIX="${MODEL_SERVICE_PREFIX:-model}"
MODEL_URL_TEMPLATE="${MODEL_URL_TEMPLATE:-http://{service_name}:3000}"
MLFLOW_TRACKING_URI="${MLFLOW_TRACKING_URI:-}"
REGISTRY_SERVER="${REGISTRY_SERVER:-}"
TU_ORG="${TU_ORG:-}"
MODEL_BASE_IMAGE="${MODEL_BASE_IMAGE:-}"
MODEL_REGISTRY_SECRET="${MODEL_REGISTRY_SECRET:-model-registry-creds}"
KANIKO_IMAGE="${KANIKO_IMAGE:-gcr.io/kaniko-project/executor:v1.23.2-debug}"
KANIKO_SKIP_TLS_VERIFY="${KANIKO_SKIP_TLS_VERIFY:-0}"
AUTO_DEPLOY_ENABLED="${AUTO_DEPLOY_ENABLED:-1}"
DEPLOY_BUILD_TIMEOUT_SEC="${DEPLOY_BUILD_TIMEOUT_SEC:-1800}"
DEPLOY_ROLLOUT_TIMEOUT_SEC="${DEPLOY_ROLLOUT_TIMEOUT_SEC:-300}"

if ! command -v kubectl >/dev/null 2>&1; then
  echo "Error: kubectl is not installed or not in PATH."
  exit 1
fi

kubectl get namespace "$NAMESPACE" >/dev/null 2>&1 || kubectl create namespace "$NAMESPACE"

cat <<EOF | kubectl apply -f -
apiVersion: v1
kind: ServiceAccount
metadata:
  name: gateway-sa
  namespace: ${NAMESPACE}
---
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: gateway-service-discovery
  namespace: ${NAMESPACE}
rules:
  - apiGroups: [""]
    resources: ["services", "pods"]
    verbs: ["get", "list", "watch", "create", "update", "patch"]
  - apiGroups: [""]
    resources: ["pods/log"]
    verbs: ["get"]
  - apiGroups: ["apps"]
    resources: ["deployments"]
    verbs: ["get", "list", "watch", "create", "update", "patch"]
  - apiGroups: ["batch"]
    resources: ["jobs"]
    verbs: ["get", "list", "watch", "create", "delete"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: gateway-service-discovery
  namespace: ${NAMESPACE}
subjects:
  - kind: ServiceAccount
    name: gateway-sa
    namespace: ${NAMESPACE}
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: Role
  name: gateway-service-discovery
---
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
      serviceAccountName: gateway-sa
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
            - name: MLFLOW_TRACKING_URI
              value: "${MLFLOW_TRACKING_URI}"
            - name: REGISTRY_SERVER
              value: "${REGISTRY_SERVER}"
            - name: TU_ORG
              value: "${TU_ORG}"
            - name: MODEL_BASE_IMAGE
              value: "${MODEL_BASE_IMAGE}"
            - name: MODEL_REGISTRY_SECRET
              value: "${MODEL_REGISTRY_SECRET}"
            - name: KANIKO_IMAGE
              value: "${KANIKO_IMAGE}"
            - name: KANIKO_SKIP_TLS_VERIFY
              value: "${KANIKO_SKIP_TLS_VERIFY}"
            - name: AUTO_DEPLOY_ENABLED
              value: "${AUTO_DEPLOY_ENABLED}"
            - name: DEPLOY_BUILD_TIMEOUT_SEC
              value: "${DEPLOY_BUILD_TIMEOUT_SEC}"
            - name: DEPLOY_ROLLOUT_TIMEOUT_SEC
              value: "${DEPLOY_ROLLOUT_TIMEOUT_SEC}"
          readinessProbe:
            httpGet:
              path: /readyz
              port: 3000
            initialDelaySeconds: 10
            periodSeconds: 10
          livenessProbe:
            httpGet:
              path: /livez
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
