#!/usr/bin/env bash
set -euo pipefail

TAG="${1:-latest}"
REGISTRY_PREFIX="${2:-}"

if command -v bentoml >/dev/null 2>&1; then
  BENTOML_CMD=(bentoml)
elif command -v uv >/dev/null 2>&1; then
  BENTOML_CMD=(uv run bentoml)
else
  echo "Error: neither 'bentoml' nor 'uv' is available in PATH."
  exit 1
fi

if ! command -v docker >/dev/null 2>&1; then
  echo "Error: docker is required for 'bentoml containerize' and is not in PATH."
  exit 1
fi

if [[ -n "$REGISTRY_PREFIX" ]]; then
  GATEWAY_IMAGE="${REGISTRY_PREFIX}/model-gateway:${TAG}"
  MODEL_SERVICE_IMAGE="${REGISTRY_PREFIX}/model-service:${TAG}"
else
  GATEWAY_IMAGE="model-gateway:${TAG}"
  MODEL_SERVICE_IMAGE="model-service:${TAG}"
fi

echo "Building Bentos..."
GATEWAY_BENTO_RAW="$(cd gateway && "${BENTOML_CMD[@]}" build -f bentofile.yaml -o tag | tail -n 1 | tr -d '\r')"
MODEL_SERVICE_BENTO_RAW="$(cd model_service && "${BENTOML_CMD[@]}" build -f bentofile.yaml -o tag | tail -n 1 | tr -d '\r')"
GATEWAY_BENTO_TAG="${GATEWAY_BENTO_RAW#__tag__:}"
MODEL_SERVICE_BENTO_TAG="${MODEL_SERVICE_BENTO_RAW#__tag__:}"

if [[ -z "$GATEWAY_BENTO_TAG" || -z "$MODEL_SERVICE_BENTO_TAG" ]]; then
  echo "Error: failed to build Bento tags."
  exit 1
fi

echo "Containerizing Bentos..."
"${BENTOML_CMD[@]}" containerize "$GATEWAY_BENTO_TAG" -t "$GATEWAY_IMAGE"
"${BENTOML_CMD[@]}" containerize "$MODEL_SERVICE_BENTO_TAG" -t "$MODEL_SERVICE_IMAGE"

if [[ -n "$REGISTRY_PREFIX" ]]; then
  echo "Pushing images to registry..."
  docker push "$GATEWAY_IMAGE"
  docker push "$MODEL_SERVICE_IMAGE"
fi

echo "Built Bento tags:"
echo "  $GATEWAY_BENTO_TAG"
echo "  $MODEL_SERVICE_BENTO_TAG"

echo "Built images:"
echo "  $GATEWAY_IMAGE"
echo "  $MODEL_SERVICE_IMAGE"
echo
echo "Use these exports before deploy scripts:"
echo "  export GATEWAY_IMAGE=$GATEWAY_IMAGE"
echo "  export MODEL_SERVICE_IMAGE=$MODEL_SERVICE_IMAGE"
