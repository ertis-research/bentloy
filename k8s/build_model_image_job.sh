#!/usr/bin/env bash
# Submit a Kubernetes Job that builds a model image from MLflow artifacts.
set -euo pipefail

# Prints command usage and required/optional parameters.
usage() {
  cat <<'EOF'
Build one model image inside Kubernetes using a Job (init container + Kaniko).

Usage:
  ./k8s/build_model_image_job.sh --model-name <name> [options]

Options:
  --model-name <name>         Required model name.
  --model-version <version>   Model version (default: latest).
  --namespace <ns>            Namespace for the Job (default: K8S_NAMESPACE or api-models).
  --run-id <id>               Optional run id used to enrich the image tag.
  --model-uri <uri>           Optional explicit model URI (example: runs:/<run_id>/model).
  --target-image <image>      Optional full image ref. If omitted, script computes one.
  --insecure-registry-tls     Skip TLS verification for Kaniko registry operations.
  --wait                      Wait until Job completes and print logs.
  -h, --help                  Show this help.

Required environment (unless overridden by --target-image):
  TU_ORG                      Organization/project namespace in your registry.

Optional environment:
  MLFLOW_TRACKING_URI         Required when --model-uri is not provided.
  REGISTRY_SERVER             Required unless --target-image and MODEL_BASE_IMAGE are explicit.
  MODEL_BASE_IMAGE            Optional explicit base image. If empty, computed as <registry>/<org>/model-service-base:py310.
  MODEL_REGISTRY_SECRET       Default: model-registry-creds
  KANIKO_IMAGE                Default: gcr.io/kaniko-project/executor:v1.23.2-debug
  KANIKO_SKIP_TLS_VERIFY      Set to 1/true/yes to skip TLS verification in Kaniko.
EOF
}

# Prints an error and exits with non-zero status.
fail() {
  echo "Error: $*" >&2
  exit 1
}

# Normalizes registry input and returns host[:port] without scheme or path.
normalize_registry_server() {
  local value="${1:-}"
  value="${value#http://}"
  value="${value#https://}"
  value="${value%%/*}"
  echo "$value"
}

# Validates registry host[:port] format.
is_valid_registry_server() {
  [[ "$1" =~ ^[A-Za-z0-9.-]+(:[0-9]+)?$ ]]
}

# Validates Kubernetes DNS label format.
is_valid_k8s_name() {
  [[ "$1" =~ ^[a-z0-9]([-a-z0-9]*[a-z0-9])?$ ]]
}

# Validates allowed characters for a container image reference.
is_valid_image_ref() {
  [[ "$1" =~ ^[a-z0-9./:_-]+$ ]]
}

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

MODEL_NAME=""
MODEL_VERSION="latest"
NAMESPACE="${K8S_NAMESPACE:-api-models}"
RUN_ID=""
MODEL_URI="${MODEL_URI:-}"
TARGET_IMAGE="${TARGET_IMAGE:-}"
WAIT_FOR_COMPLETION="0"
INSECURE_REGISTRY_TLS="${KANIKO_SKIP_TLS_VERIFY:-0}"
TEMPLATE_PATH="$SCRIPT_DIR/yaml/model_image_job/job.yaml"

import_dotenv_without_override ".env"
NAMESPACE="${K8S_NAMESPACE:-$NAMESPACE}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --model-name)
      [[ $# -ge 2 ]] || fail "Missing value for --model-name"
      MODEL_NAME="$2"
      shift 2
      ;;
    --model-version)
      [[ $# -ge 2 ]] || fail "Missing value for --model-version"
      MODEL_VERSION="$2"
      shift 2
      ;;
    --namespace)
      [[ $# -ge 2 ]] || fail "Missing value for --namespace"
      NAMESPACE="$2"
      shift 2
      ;;
    --run-id)
      [[ $# -ge 2 ]] || fail "Missing value for --run-id"
      RUN_ID="$2"
      shift 2
      ;;
    --model-uri)
      [[ $# -ge 2 ]] || fail "Missing value for --model-uri"
      MODEL_URI="$2"
      shift 2
      ;;
    --target-image)
      [[ $# -ge 2 ]] || fail "Missing value for --target-image"
      TARGET_IMAGE="$2"
      shift 2
      ;;
    --wait)
      WAIT_FOR_COMPLETION="1"
      shift
      ;;
    --insecure-registry-tls)
      INSECURE_REGISTRY_TLS="1"
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      fail "Unknown option: $1"
      ;;
  esac
done

[[ -n "$MODEL_NAME" ]] || fail "--model-name is required"

if ! command -v kubectl >/dev/null 2>&1; then
  fail "kubectl is not installed or not in PATH."
fi

MLFLOW_TRACKING_URI="${MLFLOW_TRACKING_URI:-}"
if [[ -z "$MLFLOW_TRACKING_URI" && -z "$MODEL_URI" ]]; then
  fail "Set MLFLOW_TRACKING_URI or pass --model-uri."
fi

REGISTRY_SERVER="${REGISTRY_SERVER:-}"
TU_ORG="${TU_ORG:-}"
MODEL_REGISTRY_SECRET="${MODEL_REGISTRY_SECRET:-model-registry-creds}"
KANIKO_IMAGE="${KANIKO_IMAGE:-gcr.io/kaniko-project/executor:v1.23.2-debug}"
REGISTRY_SERVER="$(normalize_registry_server "$REGISTRY_SERVER")"

if [[ -n "$REGISTRY_SERVER" ]] && ! is_valid_registry_server "$REGISTRY_SERVER"; then
  fail "REGISTRY_SERVER must be a registry host (example: registry.example.com or registry.example.com:5000)."
fi

# Converts a free-form model name into a Kubernetes-safe slug.
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
  rendered="${rendered//__JOB_NAME__/$JOB_NAME}"
  rendered="${rendered//__NAMESPACE__/$NAMESPACE}"
  rendered="${rendered//__MODEL_SLUG__/$MODEL_SLUG}"
  rendered="${rendered//__REGISTRY_SECRET__/$MODEL_REGISTRY_SECRET}"
  rendered="${rendered//__MLFLOW_TRACKING_URI__/$MLFLOW_TRACKING_URI_ESCAPED}"
  rendered="${rendered//__MODEL_NAME__/$MODEL_NAME_ESCAPED}"
  rendered="${rendered//__MODEL_VERSION__/$MODEL_VERSION_ESCAPED}"
  rendered="${rendered//__MODEL_URI__/$MODEL_URI_ESCAPED}"
  rendered="${rendered//__KANIKO_IMAGE__/$KANIKO_IMAGE}"
  rendered="${rendered//__TARGET_IMAGE__/$TARGET_IMAGE}"
  rendered="${rendered//__BASE_IMAGE__/$BASE_IMAGE}"
  rendered="${rendered//__KANIKO_TLS_ARGS_BLOCK__/$KANIKO_TLS_ARGS_BLOCK}"

  printf '%s' "$rendered"
}

MODEL_SLUG="$(to_slug "$MODEL_NAME")"
MODEL_VERSION_SLUG="$(echo "$MODEL_VERSION" | tr '[:upper:]' '[:lower:]' | sed -E 's/[^a-z0-9._-]+/-/g')"

[[ -n "$MODEL_SLUG" ]] || fail "MODEL_NAME produced an empty slug."
is_valid_k8s_name "$MODEL_SLUG" || fail "MODEL_NAME slug '$MODEL_SLUG' is not a valid Kubernetes label value."
is_valid_k8s_name "$NAMESPACE" || fail "Namespace '$NAMESPACE' is not a valid Kubernetes namespace name."

if [[ -n "$RUN_ID" && ! "$RUN_ID" =~ ^[A-Za-z0-9._-]{4,128}$ ]]; then
  fail "Invalid --run-id format."
fi

if [[ -z "$TARGET_IMAGE" ]]; then
  [[ -n "$TU_ORG" ]] || fail "Set TU_ORG or pass --target-image."
  [[ -n "$REGISTRY_SERVER" ]] || fail "Set REGISTRY_SERVER or pass --target-image."

  if [[ -n "$RUN_ID" ]]; then
    RUN_SHORT="$(echo "$RUN_ID" | cut -c1-8)"
    TAG="v${MODEL_VERSION_SLUG}-r${RUN_SHORT}"
  else
    NOW_UTC="$(date -u +%Y%m%d%H%M%S)"
    TAG="v${MODEL_VERSION_SLUG}-t${NOW_UTC}"
  fi
  TARGET_IMAGE="${REGISTRY_SERVER}/${TU_ORG}/model-${MODEL_SLUG}:${TAG}"
fi

is_valid_image_ref "$TARGET_IMAGE" || fail "Computed/received target image '$TARGET_IMAGE' has invalid characters."

if [[ -n "${MODEL_BASE_IMAGE:-}" ]]; then
  BASE_IMAGE="$MODEL_BASE_IMAGE"
else
  [[ -n "$TU_ORG" ]] || fail "Set MODEL_BASE_IMAGE or TU_ORG."
  [[ -n "$REGISTRY_SERVER" ]] || fail "Set MODEL_BASE_IMAGE or REGISTRY_SERVER."
  BASE_IMAGE="${REGISTRY_SERVER}/${TU_ORG}/model-service-base:py310"
fi

TLS_FLAG="$(echo "$INSECURE_REGISTRY_TLS" | tr '[:upper:]' '[:lower:]')"
KANIKO_TLS_ARGS_BLOCK=""
if [[ "$TLS_FLAG" == "1" || "$TLS_FLAG" == "true" || "$TLS_FLAG" == "yes" ]]; then
  KANIKO_TLS_ARGS_BLOCK=$'            - --skip-tls-verify\n            - --skip-tls-verify-pull'
  if [[ -n "$REGISTRY_SERVER" ]]; then
    KANIKO_TLS_ARGS_BLOCK+=$'\n'"            - --skip-tls-verify-registry=${REGISTRY_SERVER}"
  fi
  echo "Warning: TLS verification is disabled for Kaniko registry operations."
fi

JOB_NAME="build-model-${MODEL_SLUG}-$(date +%H%M%S)"
JOB_NAME="$(echo "$JOB_NAME" | sed -E 's/[^a-z0-9-]+/-/g' | cut -c1-63 | sed -E 's/-+$//')"

kubectl get namespace "$NAMESPACE" >/dev/null 2>&1 || kubectl create namespace "$NAMESPACE"
kubectl -n "$NAMESPACE" get secret "$MODEL_REGISTRY_SECRET" >/dev/null 2>&1 || fail "Secret '$MODEL_REGISTRY_SECRET' not found in namespace '$NAMESPACE'."

[[ -f "$TEMPLATE_PATH" ]] || fail "Missing template file '$TEMPLATE_PATH'."

MLFLOW_TRACKING_URI_ESCAPED="$(escape_yaml_dq "$MLFLOW_TRACKING_URI")"
MODEL_NAME_ESCAPED="$(escape_yaml_dq "$MODEL_NAME")"
MODEL_VERSION_ESCAPED="$(escape_yaml_dq "$MODEL_VERSION")"
MODEL_URI_ESCAPED="$(escape_yaml_dq "$MODEL_URI")"

MANIFEST="$(render_template "$TEMPLATE_PATH")"
printf '%s\n' "$MANIFEST" | kubectl apply -f -

echo "Build Job submitted."
echo "Job: $JOB_NAME"
echo "Namespace: $NAMESPACE"
echo "Target image: $TARGET_IMAGE"
echo "Base image: $BASE_IMAGE"
echo
echo "Check status:"
echo "  kubectl -n $NAMESPACE get job $JOB_NAME"
echo "  kubectl -n $NAMESPACE logs job/$JOB_NAME -c prepare-build-context --tail=200"
echo "  kubectl -n $NAMESPACE logs job/$JOB_NAME -c kaniko --tail=200"

if [[ "$WAIT_FOR_COMPLETION" == "1" ]]; then
  echo
  echo "Waiting for Job completion..."
  if ! kubectl -n "$NAMESPACE" wait --for=condition=complete --timeout=1800s "job/$JOB_NAME"; then
    echo "Job did not complete successfully. Showing logs:"
    kubectl -n "$NAMESPACE" logs "job/$JOB_NAME" -c prepare-build-context --tail=200 || true
    kubectl -n "$NAMESPACE" logs "job/$JOB_NAME" -c kaniko --tail=200 || true
    exit 1
  fi
  echo "Job completed successfully."
  kubectl -n "$NAMESPACE" logs "job/$JOB_NAME" -c prepare-build-context --tail=200 || true
  kubectl -n "$NAMESPACE" logs "job/$JOB_NAME" -c kaniko --tail=200 || true
fi
