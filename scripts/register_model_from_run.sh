#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 2 ]]; then
  echo "Usage: ./scripts/register_model_from_run.sh <MODEL_NAME> <RUN_ID> [ARTIFACT_PATH] [STAGE]"
  echo "Example: ./scripts/register_model_from_run.sh my_model 9a3bea0a201f4b6e912c205f7f64d836 model Production"
  exit 1
fi

MODEL_NAME="$1"
RUN_ID="$2"
ARTIFACT_PATH="${3:-model}"
TARGET_STAGE="${4:-Production}"

if [[ -f .env ]]; then
  # shellcheck disable=SC1091
  source .env
fi

MLFLOW_TRACKING_URI="${MLFLOW_TRACKING_URI:-}"
K8S_NAMESPACE="${K8S_NAMESPACE:-api-models}"

if [[ -z "$MLFLOW_TRACKING_URI" ]]; then
  echo "Error: MLFLOW_TRACKING_URI is not set (in .env or environment)."
  exit 1
fi

if ! command -v curl >/dev/null 2>&1; then
  echo "Error: curl is required."
  exit 1
fi

if ! command -v jq >/dev/null 2>&1; then
  echo "Error: jq is required."
  exit 1
fi

base_url="${MLFLOW_TRACKING_URI%/}/api/2.0/mlflow"

create_model_payload=$(jq -n --arg name "$MODEL_NAME" '{name: $name}')
create_model_resp=$(curl -s -X POST "$base_url/registered-models/create" \
  -H "Content-Type: application/json" \
  -d "$create_model_payload")

if echo "$create_model_resp" | jq -e '.error_code == "RESOURCE_ALREADY_EXISTS"' >/dev/null 2>&1; then
  echo "Registered model already exists: $MODEL_NAME"
elif echo "$create_model_resp" | jq -e '.registered_model.name' >/dev/null 2>&1; then
  echo "Registered model created: $MODEL_NAME"
else
  echo "Error creating registered model: $create_model_resp"
  exit 1
fi

SOURCE_URI="runs:/${RUN_ID}/${ARTIFACT_PATH}"
create_version_payload=$(jq -n \
  --arg name "$MODEL_NAME" \
  --arg source "$SOURCE_URI" \
  --arg run_id "$RUN_ID" \
  '{name: $name, source: $source, run_id: $run_id}')

create_version_resp=$(curl -s -X POST "$base_url/model-versions/create" \
  -H "Content-Type: application/json" \
  -d "$create_version_payload")

MODEL_VERSION=$(echo "$create_version_resp" | jq -r '.model_version.version // empty')

if [[ -z "$MODEL_VERSION" ]]; then
  echo "Error creating model version: $create_version_resp"
  exit 1
fi

echo "Model version created: ${MODEL_NAME} v${MODEL_VERSION}"

transition_payload=$(jq -n \
  --arg name "$MODEL_NAME" \
  --arg version "$MODEL_VERSION" \
  --arg stage "$TARGET_STAGE" \
  '{name: $name, version: $version, stage: $stage, archive_existing_versions: true}')

transition_resp=$(curl -s -X POST "$base_url/model-versions/transition-stage" \
  -H "Content-Type: application/json" \
  -d "$transition_payload")

if echo "$transition_resp" | jq -e '.model_version.current_stage == $stage' --arg stage "$TARGET_STAGE" >/dev/null 2>&1; then
  echo "Model transitioned to stage ${TARGET_STAGE}: ${MODEL_NAME} v${MODEL_VERSION}"
else
  echo "Warning: transition response: $transition_resp"
fi

echo "Done. You can now deploy with:"
echo "  ./k8s/deploy_model.sh ${MODEL_NAME} latest ${K8S_NAMESPACE} 1"
