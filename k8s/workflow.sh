#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

ACTION="${1:-help}"
if [[ $# -gt 0 ]]; then
  shift
fi

TAG="latest"
REGISTRY_PREFIX=""
NAMESPACE="${K8S_NAMESPACE:-api-models}"
MODEL_NAME=""
MODEL_VERSION="latest"
REPLICAS="1"
SKIP_BUILD="0"
SKIP_GATEWAY="0"

usage() {
  cat <<'EOF'
Unified Kubernetes workflow wrapper.

Usage:
  ./k8s/workflow.sh bootstrap [options]
  ./k8s/workflow.sh deploy-model --model-name <name> [options]
  ./k8s/workflow.sh full --model-name <name> [options]

Actions:
  bootstrap      Build shared images (optional) and deploy/update gateway
  deploy-model   Deploy/update one model service
  full           bootstrap + deploy-model

Options:
  --tag <tag>                  Image tag for build_images (default: latest)
  --registry-prefix <prefix>   Registry prefix (example: registry.example.com/my-project)
  --namespace <ns>             Kubernetes namespace (default: K8S_NAMESPACE or api-models)
  --model-name <name>          Model name for deploy-model/full
  --model-version <version>    Model version (default: latest)
  --replicas <n>               Replica count for model deploy (default: 1)
  --skip-build                 Skip image build step for bootstrap/full
  --skip-gateway               Skip gateway deploy step for full
  -h, --help                   Show this help

Examples:
  ./k8s/workflow.sh bootstrap --tag latest --namespace api-models
  ./k8s/workflow.sh deploy-model --model-name tiny_demo_model --model-version latest --namespace api-models
  ./k8s/workflow.sh full --model-name tiny_demo_model --tag v2 --registry-prefix registry.example.com/my-project --namespace api-models
EOF
}

fail() {
  echo "Error: $*" >&2
  exit 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --tag)
      [[ $# -ge 2 ]] || fail "Missing value for --tag"
      TAG="$2"
      shift 2
      ;;
    --registry-prefix)
      [[ $# -ge 2 ]] || fail "Missing value for --registry-prefix"
      REGISTRY_PREFIX="$2"
      shift 2
      ;;
    --namespace)
      [[ $# -ge 2 ]] || fail "Missing value for --namespace"
      NAMESPACE="$2"
      shift 2
      ;;
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
    --replicas)
      [[ $# -ge 2 ]] || fail "Missing value for --replicas"
      REPLICAS="$2"
      shift 2
      ;;
    --skip-build)
      SKIP_BUILD="1"
      shift
      ;;
    --skip-gateway)
      SKIP_GATEWAY="1"
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

build_images() {
  "$SCRIPT_DIR/build_images.sh" "$TAG" "$REGISTRY_PREFIX"
  if [[ -n "$REGISTRY_PREFIX" ]]; then
    export GATEWAY_IMAGE="${REGISTRY_PREFIX}/model-gateway:${TAG}"
    export MODEL_SERVICE_IMAGE="${REGISTRY_PREFIX}/model-service:${TAG}"
  fi
}

deploy_gateway() {
  "$SCRIPT_DIR/deploy_gateway.sh" "$NAMESPACE"
}

deploy_model() {
  [[ -n "$MODEL_NAME" ]] || fail "--model-name is required for action '$ACTION'"
  "$SCRIPT_DIR/deploy_model.sh" "$MODEL_NAME" "$MODEL_VERSION" "$NAMESPACE" "$REPLICAS"
}

case "$ACTION" in
  bootstrap)
    if [[ "$SKIP_BUILD" != "1" ]]; then
      build_images
    fi
    deploy_gateway
    ;;
  deploy-model)
    deploy_model
    ;;
  full)
    if [[ "$SKIP_BUILD" != "1" ]]; then
      build_images
    fi
    if [[ "$SKIP_GATEWAY" != "1" ]]; then
      deploy_gateway
    fi
    deploy_model
    ;;
  help|-h|--help|"")
    usage
    ;;
  *)
    fail "Unknown action: $ACTION"
    ;;
esac
