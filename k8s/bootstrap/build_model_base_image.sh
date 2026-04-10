#!/usr/bin/env bash
set -euo pipefail

ORG="${1:-${TU_ORG:-}}"
TAG="${2:-${MODEL_BASE_TAG:-py310}}"
REGISTRY_SERVER="${REGISTRY_SERVER:-}"
REGISTRY_USERNAME="${REGISTRY_USERNAME:-}"
REGISTRY_PASSWORD="${REGISTRY_PASSWORD:-}"

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

if [[ -f .env ]]; then
  # shellcheck disable=SC1091
  source .env
  ORG="${1:-${TU_ORG:-$ORG}}"
  TAG="${2:-${MODEL_BASE_TAG:-$TAG}}"
  REGISTRY_SERVER="${REGISTRY_SERVER:-$REGISTRY_SERVER}"
fi

REGISTRY_SERVER="$(normalize_registry_server "$REGISTRY_SERVER")"

if ! command -v docker >/dev/null 2>&1; then
  echo "Error: docker is required and is not in PATH."
  exit 1
fi

if [[ -z "$ORG" ]]; then
  echo "Error: missing org. Pass it as first argument or set TU_ORG."
  echo "Example: ./k8s/bootstrap/build_model_base_image.sh my-org py310"
  exit 1
fi

if [[ -z "$REGISTRY_SERVER" ]]; then
  echo "Error: missing REGISTRY_SERVER (no default is assumed)."
  echo "Example: export REGISTRY_SERVER=registry.example.com"
  exit 1
fi
if ! is_valid_registry_server "$REGISTRY_SERVER"; then
  echo "Error: REGISTRY_SERVER must be a registry host (example: registry.example.com or registry.example.com:5000)."
  exit 1
fi
if [[ -n "$REGISTRY_USERNAME" && -z "$REGISTRY_PASSWORD" ]] || [[ -z "$REGISTRY_USERNAME" && -n "$REGISTRY_PASSWORD" ]]; then
  echo "Error: set both REGISTRY_USERNAME and REGISTRY_PASSWORD, or neither."
  exit 1
fi

BASE_IMAGE="${REGISTRY_SERVER}/${ORG}/model-service-base:${TAG}"

docker build -f model_service/Dockerfile.base -t "$BASE_IMAGE" model_service
if [[ -n "$REGISTRY_USERNAME" ]]; then
  if ! printf '%s' "$REGISTRY_PASSWORD" | docker login "$REGISTRY_SERVER" --username "$REGISTRY_USERNAME" --password-stdin; then
    echo "Error: docker login failed for $REGISTRY_SERVER."
    exit 1
  fi
fi
if ! docker push "$BASE_IMAGE"; then
  echo "Error: pushing base image failed. Check docker login and push permissions for '$REGISTRY_SERVER/$ORG'."
  exit 1
fi

echo "Base image built and pushed."
echo "MODEL_BASE_IMAGE=$BASE_IMAGE"
