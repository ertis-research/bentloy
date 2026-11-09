# Prepare a model build context for Kaniko using MLflow artifacts.
set -euo pipefail
python -m pip install --no-cache-dir mlflow==3.17.2 'PyYAML>=6.0' 'pyarrow<24'
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
    token = re.split(r"[<>=!~\[\s]", requirement, maxsplit=1)[0]
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

if filtered_requirements:
    filtered_requirements.append("pyarrow<24")
# ----------------------

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
    RUN if [ -s /tmp/extra_requirements.txt ]; then pip freeze > /tmp/base_constraints.txt && pip install --default-timeout=1000 --retries=10 --no-cache-dir -c /tmp/base_constraints.txt -r /tmp/extra_requirements.txt; fi
    ENV MODEL_URI=/models/model
    """
)
(workspace / "Dockerfile").write_text(dockerfile_content, encoding="utf-8")
print("Prepared model build context at /workspace/context")
PY
