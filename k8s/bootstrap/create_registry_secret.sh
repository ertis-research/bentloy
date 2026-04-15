#!/usr/bin/env bash
set -euo pipefail

NAMESPACE="${1:-${K8S_NAMESPACE:-api-models}}"
SECRET_NAME="${2:-${MODEL_REGISTRY_SECRET:-model-registry-creds}}"
REGISTRY_SERVER="${REGISTRY_SERVER:-}"
REGISTRY_USERNAME="${REGISTRY_USERNAME:-}"
REGISTRY_PASSWORD="${REGISTRY_PASSWORD:-}"
REGISTRY_EMAIL="${REGISTRY_EMAIL:-noreply@example.com}"

normalize_registry_server() {
  local value="${1:-}"
  value="${value#http://}"
  value="${value#https://}"
  value="${value%%/*}"
  echo "$value"
}

is_valid_registry_server() {
  [[ "$1" =~ ^[A-Za-z0-9.-]+(:[0-9]+)?$ ]]
}

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
NAMESPACE="${1:-${K8S_NAMESPACE:-$NAMESPACE}}"
SECRET_NAME="${2:-${MODEL_REGISTRY_SECRET:-$SECRET_NAME}}"

REGISTRY_SERVER="$(normalize_registry_server "$REGISTRY_SERVER")"

if ! command -v kubectl >/dev/null 2>&1; then
  echo "Error: kubectl is not installed or not in PATH."
  exit 1
fi

if [[ -z "$REGISTRY_USERNAME" || -z "$REGISTRY_PASSWORD" ]]; then
  echo "Error: set REGISTRY_USERNAME and REGISTRY_PASSWORD before running this script."
  echo "Example:"
  echo "  export REGISTRY_USERNAME=<registry_user>"
  echo "  export REGISTRY_PASSWORD=<registry_password_or_token>"
  exit 1
fi

if [[ -z "$REGISTRY_SERVER" ]]; then
  echo "Error: set REGISTRY_SERVER (no default is assumed)."
  echo "Example:"
  echo "  export REGISTRY_SERVER=registry.example.com"
  exit 1
fi
if ! is_valid_registry_server "$REGISTRY_SERVER"; then
  echo "Error: REGISTRY_SERVER must be a registry host (example: registry.example.com or registry.example.com:5000)."
  exit 1
fi

kubectl get namespace "$NAMESPACE" >/dev/null 2>&1 || kubectl create namespace "$NAMESPACE"

kubectl -n "$NAMESPACE" create secret docker-registry "$SECRET_NAME" \
  --docker-server="$REGISTRY_SERVER" \
  --docker-username="$REGISTRY_USERNAME" \
  --docker-password="$REGISTRY_PASSWORD" \
  --docker-email="$REGISTRY_EMAIL" \
  --dry-run=client -o yaml | kubectl apply -f -

echo "Registry secret ensured."
echo "Namespace: $NAMESPACE"
echo "Secret: $SECRET_NAME"
echo "Registry: $REGISTRY_SERVER"
