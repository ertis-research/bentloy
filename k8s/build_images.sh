#!/usr/bin/env bash
# Build gateway and model_service images, and optionally push to a registry.
set -euo pipefail

TAG="${1:-latest}"
REGISTRY_PREFIX="${2:-}"

if ! command -v docker >/dev/null 2>&1; then
  echo "Error: docker is required and is not in PATH."
  exit 1
fi

if [[ -n "$REGISTRY_PREFIX" ]]; then
  GATEWAY_IMAGE="${REGISTRY_PREFIX}/model-gateway:${TAG}"
  MODEL_SERVICE_IMAGE="${REGISTRY_PREFIX}/model-service:${TAG}"
else
  GATEWAY_IMAGE="model-gateway:${TAG}"
  MODEL_SERVICE_IMAGE="model-service:${TAG}"
fi

echo "Building shared images using Dockerfiles..."
docker build -t "$GATEWAY_IMAGE" ./gateway
docker build -t "$MODEL_SERVICE_IMAGE" ./model_service

push_if_needed() {
  if [[ -n "$REGISTRY_PREFIX" ]]; then
    echo "Pushing images to registry..."
    docker push "$GATEWAY_IMAGE"
    docker push "$MODEL_SERVICE_IMAGE"
  fi
}

push_if_needed

echo "Built images:"
echo "  $GATEWAY_IMAGE"
echo "  $MODEL_SERVICE_IMAGE"
echo
echo "Use these exports before deploy scripts:"
echo "  export GATEWAY_IMAGE=$GATEWAY_IMAGE"
echo "  export MODEL_SERVICE_IMAGE=$MODEL_SERVICE_IMAGE"
