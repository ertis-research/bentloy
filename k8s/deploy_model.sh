#!/usr/bin/env bash
# Deploy one model_service instance from template manifests.
set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "Usage: ./k8s/deploy_model.sh <MODEL_NAME> [MODEL_VERSION] [NAMESPACE] [REPLICAS] [DEPLOYED_NAME] [APPEND_DATE]"
  exit 1
fi

MODEL_NAME="$1"
MODEL_VERSION="${2:-latest}"
NAMESPACE="${3:-${K8S_NAMESPACE:-api-models}}"
REPLICAS="${4:-1}"
DEPLOYED_NAME="${5:-}"
APPEND_DATE="${6:-false}"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
TEMPLATE_DIR="$SCRIPT_DIR/yaml/model_service"
DEPLOYMENT_TEMPLATE="$TEMPLATE_DIR/deployment.yaml"
SERVICE_TEMPLATE="$TEMPLATE_DIR/service.yaml"

# Imports KEY=VALUE pairs from .env only when variables are not already set.
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

# Validates Kubernetes DNS label format.
is_valid_k8s_name() {
  [[ "$1" =~ ^[a-z0-9]([-a-z0-9]*[a-z0-9])?$ ]]
}

# Validates allowed characters for a container image reference.
is_valid_image_ref() {
  [[ "$1" =~ ^[a-z0-9./:_-]+$ ]]
}

# Parses common truthy values (1/true/yes/on) from text.
is_true() {
  local value="${1:-}"
  value="$(echo "$value" | tr '[:upper:]' '[:lower:]')"
  [[ "$value" == "1" || "$value" == "true" || "$value" == "yes" || "$value" == "on" ]]
}

if [[ -z "$MLFLOW_TRACKING_URI" && -z "$MODEL_URI" ]]; then
  echo "Error: set MLFLOW_TRACKING_URI or MODEL_URI (in .env or environment)."
  exit 1
fi

if ! command -v kubectl >/dev/null 2>&1; then
  echo "Error: kubectl is not installed or not in PATH."
  exit 1
fi

# Converts a free-form name into a Kubernetes-safe slug.
to_slug() {
  echo "$1" \
    | tr '[:upper:]' '[:lower:]' \
    | tr '_' '-' \
    | sed -E 's/[^a-z0-9-]+/-/g; s/^-+//; s/-+$//; s/-+/-/g'
}

# Escapes backslashes and quotes for YAML double-quoted string values.
escape_yaml_dq() {
  local value="$1"
  value="${value//\\/\\\\}"
  value="${value//\"/\\\"}"
  printf '%s' "$value"
}

# Replaces placeholder tokens in a template file with runtime values.
render_template() {
  local template_path="$1"
  local rendered

  rendered="$(<"$template_path")"
  rendered="${rendered//__DEPLOY_NAME__/$DEPLOY_NAME}"
  rendered="${rendered//__SERVICE_NAME__/$SERVICE_NAME}"
  rendered="${rendered//__NAMESPACE__/$NAMESPACE}"
  rendered="${rendered//__MODEL_SLUG__/$MODEL_SLUG}"
  rendered="${rendered//__REPLICAS__/$REPLICAS}"
  rendered="${rendered//__MODEL_SERVICE_IMAGE__/$MODEL_SERVICE_IMAGE_ESCAPED}"
  rendered="${rendered//__MLFLOW_TRACKING_URI__/$MLFLOW_TRACKING_URI_ESCAPED}"
  rendered="${rendered//__MODEL_NAME__/$MODEL_NAME_ESCAPED}"
  rendered="${rendered//__MODEL_VERSION__/$MODEL_VERSION_ESCAPED}"
  rendered="${rendered//__IMAGE_PULL_SECRETS_BLOCK__/$IMAGE_PULL_SECRETS_BLOCK}"
  rendered="${rendered//__MODEL_URI_ENV_BLOCK__/$MODEL_URI_ENV_BLOCK}"

  printf '%s' "$rendered"
}

DEPLOYED_NAME_EFFECTIVE="${DEPLOYED_NAME:-$MODEL_NAME}"
if is_true "$APPEND_DATE"; then
  DATE_SUFFIX="$(date -u +%Y%m%d)"
  DEPLOYED_NAME_EFFECTIVE="${DEPLOYED_NAME_EFFECTIVE}_${DATE_SUFFIX}"
fi

MODEL_SLUG="$(to_slug "$DEPLOYED_NAME_EFFECTIVE")"
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

[[ -f "$DEPLOYMENT_TEMPLATE" ]] || {
  echo "Error: missing template file '$DEPLOYMENT_TEMPLATE'."
  exit 1
}

[[ -f "$SERVICE_TEMPLATE" ]] || {
  echo "Error: missing template file '$SERVICE_TEMPLATE'."
  exit 1
}

MODEL_URI_ENV_BLOCK=""
if [[ -n "$MODEL_URI" ]]; then
  MODEL_URI_ESCAPED="$(escape_yaml_dq "$MODEL_URI")"
  MODEL_URI_ENV_BLOCK=$(cat <<EOF
            - name: MODEL_URI
              value: "${MODEL_URI_ESCAPED}"
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

MODEL_SERVICE_IMAGE_ESCAPED="$(escape_yaml_dq "$MODEL_SERVICE_IMAGE")"
MLFLOW_TRACKING_URI_ESCAPED="$(escape_yaml_dq "$MLFLOW_TRACKING_URI")"
MODEL_NAME_ESCAPED="$(escape_yaml_dq "$MODEL_NAME")"
MODEL_VERSION_ESCAPED="$(escape_yaml_dq "$MODEL_VERSION")"

DEPLOYMENT_MANIFEST="$(render_template "$DEPLOYMENT_TEMPLATE")"
SERVICE_MANIFEST="$(render_template "$SERVICE_TEMPLATE")"
printf '%s\n---\n%s\n' "$DEPLOYMENT_MANIFEST" "$SERVICE_MANIFEST" | kubectl apply -f -

echo "Model deployed: ${MODEL_NAME} (${MODEL_VERSION})"
echo "Deployed alias: ${DEPLOYED_NAME_EFFECTIVE}"
echo "Namespace: ${NAMESPACE}"
echo "Internal service URL: http://${SERVICE_NAME}.${NAMESPACE}.svc.cluster.local:3000"
echo "Call through gateway with model_name=${DEPLOYED_NAME_EFFECTIVE}"
