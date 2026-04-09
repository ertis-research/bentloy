#!/usr/bin/env bash
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

build_with_dockerfile() {
  echo "Building images with Dockerfile fallback..."
  docker build -t "$GATEWAY_IMAGE" ./gateway
  docker build -t "$MODEL_SERVICE_IMAGE" ./model_service
}

push_if_needed() {
  if [[ -n "$REGISTRY_PREFIX" ]]; then
    echo "Pushing images to registry..."
    docker push "$GATEWAY_IMAGE"
    docker push "$MODEL_SERVICE_IMAGE"
  fi
}

BENTOML_CMD=()
if command -v bentoml >/dev/null 2>&1; then
  BENTOML_CMD=(bentoml)
elif command -v uv >/dev/null 2>&1; then
  BENTOML_CMD=(uv run bentoml)
fi

if [[ "${FORCE_DOCKER_BUILD:-0}" == "1" || ${#BENTOML_CMD[@]} -eq 0 ]]; then
  if [[ ${#BENTOML_CMD[@]} -eq 0 ]]; then
    echo "BentoML CLI not available."
  fi
  build_with_dockerfile
  push_if_needed
else
  echo "Building Bentos..."

  set +e
  GATEWAY_BENTO_RAW="$(cd gateway && "${BENTOML_CMD[@]}" build -f bentofile.yaml -o tag | tail -n 1 | tr -d '\r')"
  gw_status=$?
  MODEL_SERVICE_BENTO_RAW="$(cd model_service && "${BENTOML_CMD[@]}" build -f bentofile.yaml -o tag | tail -n 1 | tr -d '\r')"
  ms_status=$?
  set -e

  if [[ $gw_status -ne 0 || $ms_status -ne 0 ]]; then
    echo "Bento build failed. Falling back to Dockerfile build."
    build_with_dockerfile
    push_if_needed
  else
    GATEWAY_BENTO_TAG="${GATEWAY_BENTO_RAW#__tag__:}"
    MODEL_SERVICE_BENTO_TAG="${MODEL_SERVICE_BENTO_RAW#__tag__:}"

    if [[ -z "$GATEWAY_BENTO_TAG" || -z "$MODEL_SERVICE_BENTO_TAG" ]]; then
      echo "Bento tags missing. Falling back to Dockerfile build."
      build_with_dockerfile
      push_if_needed
    else
      echo "Containerizing Bentos..."
      if ! "${BENTOML_CMD[@]}" containerize "$GATEWAY_BENTO_TAG" -t "$GATEWAY_IMAGE"; then
        echo "Gateway containerize failed. Falling back to Dockerfile build."
        build_with_dockerfile
        push_if_needed
      elif ! "${BENTOML_CMD[@]}" containerize "$MODEL_SERVICE_BENTO_TAG" -t "$MODEL_SERVICE_IMAGE"; then
        echo "Model-service containerize failed. Falling back to Dockerfile build."
        build_with_dockerfile
        push_if_needed
      else
        push_if_needed
        echo "Built Bento tags:"
        echo "  $GATEWAY_BENTO_TAG"
        echo "  $MODEL_SERVICE_BENTO_TAG"
      fi
    fi
  fi
fi

echo "Built images:"
echo "  $GATEWAY_IMAGE"
echo "  $MODEL_SERVICE_IMAGE"
echo
echo "Use these exports before deploy scripts:"
echo "  export GATEWAY_IMAGE=$GATEWAY_IMAGE"
echo "  export MODEL_SERVICE_IMAGE=$MODEL_SERVICE_IMAGE"
