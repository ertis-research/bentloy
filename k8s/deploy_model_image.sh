#!/usr/bin/env bash
# Deploy a prebuilt model image by delegating to deploy_model.sh.
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

if [[ $# -lt 2 ]]; then
  echo "Usage: ./k8s/deploy_model_image.sh <MODEL_NAME> <MODEL_IMAGE> [NAMESPACE] [REPLICAS] [DEPLOYED_NAME] [APPEND_DATE]"
  exit 1
fi

MODEL_NAME="$1"
MODEL_IMAGE="$2"
NAMESPACE="${3:-${K8S_NAMESPACE:-api-models}}"
REPLICAS="${4:-1}"
DEPLOYED_NAME="${5:-}"
APPEND_DATE="${6:-false}"

export MODEL_SERVICE_IMAGE="$MODEL_IMAGE"
export MODEL_URI="/models/model"

"$SCRIPT_DIR/deploy_model.sh" "$MODEL_NAME" "image" "$NAMESPACE" "$REPLICAS" "$DEPLOYED_NAME" "$APPEND_DATE"
