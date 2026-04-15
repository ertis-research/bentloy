#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "Usage: ./k8s/deploy_model.sh <MODEL_NAME> [MODEL_VERSION] [NAMESPACE] [REPLICAS]"
  exit 1
fi

MODEL_NAME="$1"
MODEL_VERSION="${2:-latest}"
NAMESPACE="${3:-${K8S_NAMESPACE:-api-models}}"
REPLICAS="${4:-1}"

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

MODEL_SERVICE_IMAGE="${MODEL_SERVICE_IMAGE:-model-service:latest}"
MODEL_SERVICE_PREFIX="${MODEL_SERVICE_PREFIX:-model}"
MLFLOW_TRACKING_URI="${MLFLOW_TRACKING_URI:-}"
MODEL_URI="${MODEL_URI:-}"
MODEL_REGISTRY_SECRET="${MODEL_REGISTRY_SECRET:-}"

# If the image points to a remote registry and no pull secret was provided,
# default to the standard secret used in this project.
if [[ -z "$MODEL_REGISTRY_SECRET" && "$MODEL_SERVICE_IMAGE" =~ ^[a-z0-9.-]+(:[0-9]+)?/ ]]; then
  MODEL_REGISTRY_SECRET="model-registry-creds"
fi

is_valid_k8s_name() {
  [[ "$1" =~ ^[a-z0-9]([-a-z0-9]*[a-z0-9])?$ ]]
}

is_valid_image_ref() {
  [[ "$1" =~ ^[a-z0-9./:_-]+$ ]]
}

if [[ -z "$MLFLOW_TRACKING_URI" && -z "$MODEL_URI" ]]; then
  echo "Error: set MLFLOW_TRACKING_URI or MODEL_URI (in .env or environment)."
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

[[ -n "$MODEL_SLUG" ]] || {
  echo "Error: MODEL_NAME produced an empty slug."
  exit 1
}

is_valid_k8s_name "$NAMESPACE" || {
  echo "Error: invalid namespace '$NAMESPACE'."
  exit 1
}

is_valid_k8s_name "$MODEL_SLUG" || {
  echo "Error: invalid model slug '$MODEL_SLUG'."
  exit 1
}

is_valid_image_ref "$MODEL_SERVICE_IMAGE" || {
  echo "Error: invalid MODEL_SERVICE_IMAGE '$MODEL_SERVICE_IMAGE'."
  exit 1
}

MODEL_URI_ENV_BLOCK=""
if [[ -n "$MODEL_URI" ]]; then
  MODEL_URI_ENV_BLOCK=$(cat <<EOF
            - name: MODEL_URI
              value: "${MODEL_URI}"
EOF
)
fi

IMAGE_PULL_SECRETS_BLOCK=""
if [[ -n "$MODEL_REGISTRY_SECRET" ]]; then
  is_valid_k8s_name "$MODEL_REGISTRY_SECRET" || {
    echo "Error: invalid MODEL_REGISTRY_SECRET '$MODEL_REGISTRY_SECRET'."
    exit 1
  }

  IMAGE_PULL_SECRETS_BLOCK=$(cat <<EOF
      imagePullSecrets:
        - name: ${MODEL_REGISTRY_SECRET}
EOF
)
fi

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
${IMAGE_PULL_SECRETS_BLOCK}
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
${MODEL_URI_ENV_BLOCK}
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
