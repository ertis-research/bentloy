#!/usr/bin/env bash
set -euo pipefail

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

fail() {
  echo "Error: $*" >&2
  exit 1
}

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

is_valid_k8s_name() {
  [[ "$1" =~ ^[a-z0-9]([-a-z0-9]*[a-z0-9])?$ ]]
}

is_valid_image_ref() {
  [[ "$1" =~ ^[a-z0-9./:_-]+$ ]]
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

MODEL_NAME=""
MODEL_VERSION="latest"
NAMESPACE="${K8S_NAMESPACE:-api-models}"
RUN_ID=""
MODEL_URI="${MODEL_URI:-}"
TARGET_IMAGE="${TARGET_IMAGE:-}"
WAIT_FOR_COMPLETION="0"
INSECURE_REGISTRY_TLS="${KANIKO_SKIP_TLS_VERIFY:-0}"

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

to_slug() {
  echo "$1" \
    | tr '[:upper:]' '[:lower:]' \
    | tr '_' '-' \
    | sed -E 's/[^a-z0-9-]+/-/g; s/^-+//; s/-+$//; s/-+/-/g'
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
KANIKO_TLS_ARGS_YAML=""
if [[ "$TLS_FLAG" == "1" || "$TLS_FLAG" == "true" || "$TLS_FLAG" == "yes" ]]; then
  KANIKO_TLS_ARGS_YAML=$'            - --skip-tls-verify\n            - --skip-tls-verify-pull'
  if [[ -n "$REGISTRY_SERVER" ]]; then
    KANIKO_TLS_ARGS_YAML+=$'\n'"            - --skip-tls-verify-registry=${REGISTRY_SERVER}"
  fi
  echo "Warning: TLS verification is disabled for Kaniko registry operations."
fi

JOB_NAME="build-model-${MODEL_SLUG}-$(date +%H%M%S)"
JOB_NAME="$(echo "$JOB_NAME" | sed -E 's/[^a-z0-9-]+/-/g' | cut -c1-63 | sed -E 's/-+$//')"

kubectl get namespace "$NAMESPACE" >/dev/null 2>&1 || kubectl create namespace "$NAMESPACE"
kubectl -n "$NAMESPACE" get secret "$MODEL_REGISTRY_SECRET" >/dev/null 2>&1 || fail "Secret '$MODEL_REGISTRY_SECRET' not found in namespace '$NAMESPACE'."

cat <<EOF | kubectl apply -f -
apiVersion: batch/v1
kind: Job
metadata:
  name: ${JOB_NAME}
  namespace: ${NAMESPACE}
  labels:
    app: model-image-build
    model-slug: ${MODEL_SLUG}
spec:
  backoffLimit: 0
  ttlSecondsAfterFinished: 3600
  template:
    metadata:
      labels:
        app: model-image-build
        model-slug: ${MODEL_SLUG}
    spec:
      restartPolicy: Never
      automountServiceAccountToken: false
      securityContext:
        seccompProfile:
          type: RuntimeDefault
      imagePullSecrets:
        - name: ${MODEL_REGISTRY_SECRET}
      volumes:
        - name: workspace
          emptyDir: {}
        - name: registry-creds
          secret:
            secretName: ${MODEL_REGISTRY_SECRET}
            items:
              - key: .dockerconfigjson
                path: config.json
      initContainers:
        - name: prepare-build-context
          image: python:3.10-slim
          imagePullPolicy: IfNotPresent
          securityContext:
            allowPrivilegeEscalation: false
            capabilities:
              drop: ["ALL"]
          resources:
            requests:
              cpu: "250m"
              memory: "512Mi"
            limits:
              cpu: "1000m"
              memory: "2Gi"
          env:
            - name: MLFLOW_TRACKING_URI
              value: "${MLFLOW_TRACKING_URI}"
            - name: MODEL_NAME
              value: "${MODEL_NAME}"
            - name: MODEL_VERSION
              value: "${MODEL_VERSION}"
            - name: MODEL_URI
              value: "${MODEL_URI}"
          command: ["/bin/sh", "-c"]
          args:
            - |
              set -euo pipefail
              python -m pip install --no-cache-dir mlflow==3.10.0 'PyYAML>=6.0'
              python - <<'PY'
              import json
              import os
              import re
              import shutil
              import textwrap
              from pathlib import Path
              import mlflow
              from mlflow.tracking import MlflowClient
              import yaml

              tracking_uri = os.getenv("MLFLOW_TRACKING_URI", "").strip()
              model_name = os.environ["MODEL_NAME"]
              model_version = os.getenv("MODEL_VERSION", "latest").strip() or "latest"
              explicit_model_uri = os.getenv("MODEL_URI", "").strip()

              if tracking_uri:
                  mlflow.set_tracking_uri(tracking_uri)

              workspace = Path("/workspace/context")
              workspace.mkdir(parents=True, exist_ok=True)
              download_root = Path("/workspace/download")
              if download_root.exists():
                  shutil.rmtree(download_root)
              download_root.mkdir(parents=True, exist_ok=True)

              def resolve_model_uri() -> str:
                  if explicit_model_uri:
                      return explicit_model_uri
                  if model_version != "latest":
                      return f"models:/{model_name}/{model_version}"

                  client = MlflowClient()
                  latest_versions = client.get_registered_model(model_name).latest_versions
                  if not latest_versions:
                      raise ValueError(f"No registered versions for model '{model_name}'.")

                  production_versions = [
                      v.version
                      for v in latest_versions
                      if (v.current_stage or "").lower() == "production" and v.version
                  ]
                  if production_versions:
                      chosen_version = max(production_versions, key=lambda value: int(value))
                      return f"models:/{model_name}/{chosen_version}"

                  available_versions = [v.version for v in latest_versions if v.version]
                  chosen_version = max(available_versions, key=lambda value: int(value))
                  return f"models:/{model_name}/{chosen_version}"

              resolved_model_uri = resolve_model_uri()
              downloaded_path = Path(
                  mlflow.artifacts.download_artifacts(
                      artifact_uri=resolved_model_uri,
                      dst_path=str(download_root),
                  )
              )

              model_dst = workspace / "model"
              if model_dst.exists():
                  shutil.rmtree(model_dst)
              shutil.copytree(downloaded_path, model_dst)

              def package_key(requirement: str) -> str:
                  requirement = requirement.strip()
                  if not requirement:
                      return ""
                  token = re.split(r"[<>=!~\\[\\s]", requirement, maxsplit=1)[0]
                  return token.lower().replace("_", "-")

              def load_requirements() -> list[str]:
                  collected: list[str] = []

                  for path in (model_dst / "requirements.txt", model_dst / "pip_requirements.txt"):
                      if not path.exists():
                          continue
                      for raw_line in path.read_text(encoding="utf-8").splitlines():
                          line = raw_line.strip()
                          if line and not line.startswith("#"):
                              collected.append(line)

                  conda_path = model_dst / "conda.yaml"
                  if conda_path.exists():
                      data = yaml.safe_load(conda_path.read_text(encoding="utf-8")) or {}
                      for entry in data.get("dependencies", []):
                          if isinstance(entry, dict) and "pip" in entry:
                              for req in entry.get("pip") or []:
                                  line = str(req).strip()
                                  if line and not line.startswith("#"):
                                      collected.append(line)
                  return collected

              base_packages = {"bentoml", "mlflow", "numpy", "pandas"}
              filtered_requirements: list[str] = []
              seen_packages: set[str] = set()

              for requirement in load_requirements():
                  key = package_key(requirement)
                  if not key or key in base_packages or key in seen_packages:
                      continue
                  seen_packages.add(key)
                  filtered_requirements.append(requirement)

              requirements_path = workspace / "extra_requirements.txt"
              if filtered_requirements:
                  requirements_path.write_text("\n".join(filtered_requirements) + "\n", encoding="utf-8")
              else:
                  requirements_path.write_text("", encoding="utf-8")

              dockerfile_content = textwrap.dedent(
                  """\
                  ARG BASE_IMAGE
                  FROM ${BASE_IMAGE}
                  WORKDIR /app
                  COPY model /models/model
                  COPY extra_requirements.txt /tmp/extra_requirements.txt
                  RUN if [ -s /tmp/extra_requirements.txt ]; then pip install --no-cache-dir -r /tmp/extra_requirements.txt; fi
                  ENV MODEL_URI=/models/model
                  """
              )
              (workspace / "Dockerfile").write_text(dockerfile_content, encoding="utf-8")

              metadata = {
                  "model_name": model_name,
                  "model_version_requested": model_version,
                  "model_uri_resolved": resolved_model_uri,
                  "extra_requirements": filtered_requirements,
              }
              (workspace / "build_meta.json").write_text(
                  json.dumps(metadata, indent=2),
                  encoding="utf-8",
              )

              print("Prepared model build context at /workspace/context")
              print(json.dumps(metadata, indent=2))
              PY
          volumeMounts:
            - name: workspace
              mountPath: /workspace
      containers:
        - name: kaniko
          image: ${KANIKO_IMAGE}
          imagePullPolicy: IfNotPresent
          securityContext:
            allowPrivilegeEscalation: false
          resources:
            requests:
              cpu: "500m"
              memory: "1Gi"
            limits:
              cpu: "2000m"
              memory: "4Gi"
          args:
            - --dockerfile=/workspace/context/Dockerfile
            - --context=dir:///workspace/context
            - --destination=${TARGET_IMAGE}
            - --build-arg=BASE_IMAGE=${BASE_IMAGE}
            - --snapshot-mode=redo
            - --use-new-run
${KANIKO_TLS_ARGS_YAML}
          volumeMounts:
            - name: workspace
              mountPath: /workspace
            - name: registry-creds
              mountPath: /kaniko/.docker
              readOnly: true
EOF

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
