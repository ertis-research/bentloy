import asyncio
import os
import re
import textwrap
import uuid
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

import bentoml
import httpx


@bentoml.service(resources={"cpu": "1"}, traffic={"timeout": 60})
class GatewayService:
    """API gateway that routes predictions to multiple models."""

    def __init__(self):
        # Explicit mapping via environment variables like MODELO_FOO_URL=http://...:3000
        self.model_urls: dict[str, str] = {}
        for key, value in os.environ.items():
            if key.endswith("_URL") and key.startswith("MODELO_"):
                model_name = key.replace("MODELO_", "").replace("_URL", "").lower()
                self.model_urls[model_name] = value

        # Dynamic resolution using Kubernetes service naming conventions.
        self.model_service_prefix = os.getenv("MODEL_SERVICE_PREFIX", "model")
        self.model_namespace = os.getenv("MODEL_NAMESPACE", "default")
        self.model_url_template = os.getenv(
            "MODEL_URL_TEMPLATE",
            "http://{service_name}:3000",
        )
        self.auto_deploy_enabled = self._as_bool(
            os.getenv("AUTO_DEPLOY_ENABLED"),
            default=True,
        )
        self.registry_server = self._normalize_registry_server(
            os.getenv("REGISTRY_SERVER", "")
        )
        self.registry_org = os.getenv("TU_ORG", "").strip()
        self.model_base_image = os.getenv("MODEL_BASE_IMAGE", "").strip()
        self.registry_secret = os.getenv(
            "MODEL_REGISTRY_SECRET",
            "model-registry-creds",
        ).strip()
        self.kaniko_image = os.getenv(
            "KANIKO_IMAGE",
            "gcr.io/kaniko-project/executor:v1.23.2-debug",
        ).strip()
        self.kaniko_skip_tls_verify_default = self._as_bool(
            os.getenv("KANIKO_SKIP_TLS_VERIFY"),
            default=False,
        )
        self.default_tracking_uri = os.getenv("MLFLOW_TRACKING_URI", "").strip()
        self.build_timeout_sec = self._int_from_env("DEPLOY_BUILD_TIMEOUT_SEC", 1800)
        self.rollout_timeout_sec = self._int_from_env("DEPLOY_ROLLOUT_TIMEOUT_SEC", 300)

        # Stores models observed in predict/batch_predict calls.
        self.recently_routed_models: dict[str, str] = {}
        self.deploy_operations: dict[str, dict[str, Any]] = {}
        self._deploy_lock = asyncio.Lock()

        print(f"Configured models from env: {list(self.model_urls.keys())}")
        print(
            f"Auto deploy enabled: {self.auto_deploy_enabled} | "
            f"registry_server={self.registry_server or '<unset>'}"
        )

    def _as_bool(self, value: str | None, default: bool = False) -> bool:
        if value is None:
            return default
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
        return default

    def _int_from_env(self, key: str, default: int) -> int:
        raw = os.getenv(key)
        if not raw:
            return default
        try:
            value = int(raw)
        except ValueError:
            return default
        return value if value > 0 else default

    def _now_iso(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def _normalize_registry_server(self, value: str) -> str:
        normalized = value.strip().lower()
        if normalized.startswith("http://"):
            normalized = normalized[7:]
        elif normalized.startswith("https://"):
            normalized = normalized[8:]
        if "/" in normalized:
            normalized = normalized.split("/", 1)[0]
        return normalized

    def _is_valid_registry_server(self, value: str) -> bool:
        return bool(re.fullmatch(r"[a-z0-9.-]+(?::[0-9]+)?", value))

    def _is_valid_k8s_name(self, value: str) -> bool:
        return bool(re.fullmatch(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?", value))

    def _is_valid_image_ref(self, value: str) -> bool:
        return bool(re.fullmatch(r"[a-z0-9./:_-]+", value))

    def _build_operation_id(self) -> str:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        suffix = uuid.uuid4().hex[:6]
        return f"op_{timestamp}_{suffix}"

    def _build_target_image(
        self,
        model_slug: str,
        model_version: str,
        run_id: str,
        registry_server: str,
        org: str,
    ) -> str:
        model_version_slug = re.sub(r"[^a-z0-9._-]+", "-", model_version.lower()).strip("-")
        if not model_version_slug:
            model_version_slug = "latest"

        if run_id:
            tag = f"v{model_version_slug}-r{run_id[:8]}"
        else:
            now_utc = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
            tag = f"v{model_version_slug}-t{now_utc}"

        return f"{registry_server}/{org}/model-{model_slug}:{tag}"

    def _to_model_slug(self, model_name: str) -> str:
        slug = model_name.strip().lower().replace("_", "-")
        slug = re.sub(r"[^a-z0-9-]", "-", slug)
        slug = re.sub(r"-+", "-", slug).strip("-")
        return slug

    def _slug_to_model_name(self, model_slug: str) -> str:
        return model_slug.replace("-", "_")

    def _resolve_model_base_url(self, model_name: str) -> tuple[str, bool]:
        model_key = model_name.lower()
        if model_key in self.model_urls:
            return self.model_urls[model_key], True

        model_slug = self._to_model_slug(model_name)
        service_name = f"{self.model_service_prefix}-{model_slug}"
        return (
            self.model_url_template.format(
                service_name=service_name,
                model_name=model_name,
                model_slug=model_slug,
                namespace=self.model_namespace,
            ),
            False,
        )

    def _normalize_external_model_url(self, model_url: str) -> str:
        normalized = (model_url or "").strip()
        if not normalized:
            raise ValueError("model_url is empty.")

        parsed = urlparse(normalized)
        if parsed.scheme not in {"http", "https"}:
            raise ValueError("model_url must start with http:// or https://")
        if not parsed.netloc:
            raise ValueError("model_url must include a host.")
        if parsed.query or parsed.fragment:
            raise ValueError("model_url must not include query or fragment.")

        return normalized.rstrip("/")

    def _resolve_predict_target(
        self,
        model_name: str,
        model_url: str = "",
    ) -> tuple[str, str, str]:
        explicit_url = (model_url or "").strip()
        if explicit_url:
            normalized = self._normalize_external_model_url(explicit_url)
            if normalized.endswith("/predict"):
                base_url = normalized[: -len("/predict")]
                predict_url = normalized
            else:
                base_url = normalized
                predict_url = f"{normalized}/predict"
            return predict_url, base_url, "external_url"

        base_url, from_config = self._resolve_model_base_url(model_name)
        source = "env_mapping" if from_config else "k8s_convention"
        return f"{base_url}/predict", base_url, source

    def _resolve_k8s_auth(self) -> tuple[str, dict[str, str], str | bool]:
        kube_host = os.getenv("KUBERNETES_SERVICE_HOST")
        kube_port = os.getenv("KUBERNETES_SERVICE_PORT_HTTPS", "443")
        token_path = "/var/run/secrets/kubernetes.io/serviceaccount/token"
        ca_path = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"

        if not kube_host:
            raise RuntimeError("KUBERNETES_SERVICE_HOST is not available.")
        if not os.path.exists(token_path):
            raise RuntimeError("ServiceAccount token is not available.")

        with open(token_path, "r", encoding="utf-8") as token_file:
            token = token_file.read().strip()
        if not token:
            raise RuntimeError("ServiceAccount token is empty.")

        verify: str | bool = ca_path if os.path.exists(ca_path) else False
        base_url = f"https://{kube_host}:{kube_port}"
        headers = {"Authorization": f"Bearer {token}"}
        return base_url, headers, verify

    async def _get_job_pod_name(
        self,
        client: httpx.AsyncClient,
        base_url: str,
        headers: dict[str, str],
        namespace: str,
        job_name: str,
    ) -> str | None:
        pods_url = f"{base_url}/api/v1/namespaces/{namespace}/pods"
        response = await client.get(
            pods_url,
            headers=headers,
            params={"labelSelector": f"job-name={job_name}"},
        )
        if response.status_code != 200:
            return None

        items = response.json().get("items", [])
        if not items:
            return None

        items.sort(
            key=lambda item: item.get("metadata", {}).get("creationTimestamp", ""),
            reverse=True,
        )
        return items[0].get("metadata", {}).get("name")

    async def _read_job_logs(
        self,
        client: httpx.AsyncClient,
        base_url: str,
        headers: dict[str, str],
        namespace: str,
        job_name: str,
        container: str,
    ) -> str:
        pod_name = await self._get_job_pod_name(
            client=client,
            base_url=base_url,
            headers=headers,
            namespace=namespace,
            job_name=job_name,
        )
        if not pod_name:
            return ""

        logs_url = f"{base_url}/api/v1/namespaces/{namespace}/pods/{pod_name}/log"
        response = await client.get(
            logs_url,
            headers=headers,
            params={"container": container, "tailLines": 200},
        )
        if response.status_code != 200:
            return ""
        return response.text

    async def _ensure_resource(
        self,
        client: httpx.AsyncClient,
        headers: dict[str, str],
        list_url: str,
        resource_url: str,
        manifest: dict[str, Any],
    ) -> None:
        get_response = await client.get(resource_url, headers=headers)
        if get_response.status_code == 404:
            create_response = await client.post(list_url, headers=headers, json=manifest)
            if create_response.status_code not in {200, 201, 202}:
                raise RuntimeError(
                    f"Create failed ({create_response.status_code}): {create_response.text}"
                )
            return

        if get_response.status_code != 200:
            raise RuntimeError(
                f"Read failed ({get_response.status_code}): {get_response.text}"
            )

        patch_response = await client.patch(
            resource_url,
            headers={**headers, "Content-Type": "application/merge-patch+json"},
            json=manifest,
        )
        if patch_response.status_code not in {200, 201, 202}:
            raise RuntimeError(
                f"Patch failed ({patch_response.status_code}): {patch_response.text}"
            )

    async def _wait_deployment_ready(
        self,
        client: httpx.AsyncClient,
        headers: dict[str, str],
        deployment_url: str,
        replicas: int,
        timeout_sec: int,
    ) -> None:
        start = asyncio.get_running_loop().time()
        while asyncio.get_running_loop().time() - start < timeout_sec:
            response = await client.get(deployment_url, headers=headers)
            if response.status_code != 200:
                await asyncio.sleep(3)
                continue

            payload = response.json()
            status = payload.get("status", {})
            metadata = payload.get("metadata", {})
            observed_generation = status.get("observedGeneration", 0)
            generation = metadata.get("generation", 0)
            ready = int(status.get("readyReplicas", 0))
            available = int(status.get("availableReplicas", 0))
            updated = int(status.get("updatedReplicas", 0))

            if (
                observed_generation >= generation
                and ready >= replicas
                and available >= replicas
                and updated >= replicas
            ):
                return

            await asyncio.sleep(3)

        raise RuntimeError("Deployment rollout timeout.")

    async def _update_operation(self, operation_id: str, **changes: Any) -> None:
        async with self._deploy_lock:
            operation = self.deploy_operations.get(operation_id)
            if not operation:
                return
            operation.update(changes)
            operation["updated_at"] = self._now_iso()

    async def _discover_k8s_model_services(self) -> tuple[list[dict[str, str]], str | None]:
        """
        Discover model-<slug> services using Kubernetes API from inside the pod.
        If discovery is not possible (RBAC, outside cluster, etc.), return a descriptive error.
        """
        try:
            base_url, headers, verify = self._resolve_k8s_auth()
        except Exception as exc:
            return [], str(exc)

        url = f"{base_url}/api/v1/namespaces/{self.model_namespace}/services"

        try:
            async with httpx.AsyncClient(timeout=5.0, verify=verify) as client:
                response = await client.get(url, headers=headers)
                response.raise_for_status()
                items = response.json().get("items", [])
        except Exception as exc:
            return [], f"Could not discover Kubernetes services: {exc}"

        prefix = f"{self.model_service_prefix}-"
        discovered: list[dict[str, str]] = []

        for item in items:
            service_name = item.get("metadata", {}).get("name", "")
            if not service_name.startswith(prefix):
                continue

            model_slug = service_name[len(prefix) :]
            inferred_model_name = self._slug_to_model_name(model_slug)
            base_url = self.model_url_template.format(
                service_name=service_name,
                model_name=inferred_model_name,
                model_slug=model_slug,
                namespace=self.model_namespace,
            )
            discovered.append(
                {
                    "service_name": service_name,
                    "model_slug": model_slug,
                    "inferred_model_name": inferred_model_name,
                    "base_url": base_url,
                    "predict_endpoint": f"{base_url}/predict",
                }
            )

        discovered.sort(key=lambda service: service["service_name"])
        return discovered, None

    def _build_job_manifest(
        self,
        job_name: str,
        namespace: str,
        model_name: str,
        model_version: str,
        model_uri: str,
        mlflow_tracking_uri: str,
        target_image: str,
        base_image: str,
        registry_secret: str,
        kaniko_image: str,
        insecure_registry_tls: bool,
        registry_server: str,
    ) -> dict[str, Any]:
        prepare_script = textwrap.dedent(
            """\
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
                requirements_path.write_text("\\n".join(filtered_requirements) + "\\n", encoding="utf-8")
            else:
                requirements_path.write_text("", encoding="utf-8")

            dockerfile_content = textwrap.dedent(
                \"\"\"\\
                ARG BASE_IMAGE
                FROM ${BASE_IMAGE}
                WORKDIR /app
                COPY model /models/model
                COPY extra_requirements.txt /tmp/extra_requirements.txt
                RUN if [ -s /tmp/extra_requirements.txt ]; then pip install --no-cache-dir -r /tmp/extra_requirements.txt; fi
                ENV MODEL_URI=/models/model
                \"\"\"
            )
            (workspace / "Dockerfile").write_text(dockerfile_content, encoding="utf-8")
            print("Prepared model build context at /workspace/context")
            PY
            """
        )

        kaniko_args = [
            "--dockerfile=/workspace/context/Dockerfile",
            "--context=dir:///workspace/context",
            f"--destination={target_image}",
            f"--build-arg=BASE_IMAGE={base_image}",
            "--snapshot-mode=redo",
            "--use-new-run",
        ]
        if insecure_registry_tls:
            kaniko_args.extend(["--skip-tls-verify", "--skip-tls-verify-pull"])
            if registry_server:
                kaniko_args.append(f"--skip-tls-verify-registry={registry_server}")

        return {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {
                "name": job_name,
                "namespace": namespace,
                "labels": {"app": "model-image-build"},
            },
            "spec": {
                "backoffLimit": 0,
                "ttlSecondsAfterFinished": 3600,
                "template": {
                    "metadata": {"labels": {"app": "model-image-build"}},
                    "spec": {
                        "restartPolicy": "Never",
                        "automountServiceAccountToken": False,
                        "securityContext": {"seccompProfile": {"type": "RuntimeDefault"}},
                        "imagePullSecrets": [{"name": registry_secret}],
                        "volumes": [
                            {"name": "workspace", "emptyDir": {}},
                            {
                                "name": "registry-creds",
                                "secret": {
                                    "secretName": registry_secret,
                                    "items": [{"key": ".dockerconfigjson", "path": "config.json"}],
                                },
                            },
                        ],
                        "initContainers": [
                            {
                                "name": "prepare-build-context",
                                "image": "python:3.10-slim",
                                "imagePullPolicy": "IfNotPresent",
                                "securityContext": {
                                    "allowPrivilegeEscalation": False,
                                    "capabilities": {"drop": ["ALL"]},
                                },
                                "resources": {
                                    "requests": {"cpu": "250m", "memory": "512Mi"},
                                    "limits": {"cpu": "1000m", "memory": "2Gi"},
                                },
                                "env": [
                                    {"name": "MLFLOW_TRACKING_URI", "value": mlflow_tracking_uri},
                                    {"name": "MODEL_NAME", "value": model_name},
                                    {"name": "MODEL_VERSION", "value": model_version},
                                    {"name": "MODEL_URI", "value": model_uri},
                                ],
                                "command": ["/bin/sh", "-c"],
                                "args": [prepare_script],
                                "volumeMounts": [{"name": "workspace", "mountPath": "/workspace"}],
                            }
                        ],
                        "containers": [
                            {
                                "name": "kaniko",
                                "image": kaniko_image,
                                "imagePullPolicy": "IfNotPresent",
                                "securityContext": {"allowPrivilegeEscalation": False},
                                "resources": {
                                    "requests": {"cpu": "500m", "memory": "1Gi"},
                                    "limits": {"cpu": "2000m", "memory": "4Gi"},
                                },
                                "args": kaniko_args,
                                "volumeMounts": [
                                    {"name": "workspace", "mountPath": "/workspace"},
                                    {
                                        "name": "registry-creds",
                                        "mountPath": "/kaniko/.docker",
                                        "readOnly": True,
                                    },
                                ],
                            }
                        ],
                    },
                },
            },
        }

    def _build_deployment_manifest(
        self,
        namespace: str,
        model_name: str,
        model_slug: str,
        model_image: str,
        replicas: int,
        tracking_uri: str,
        model_uri: str,
        model_version: str,
        registry_secret: str,
    ) -> dict[str, Any]:
        deployment_spec: dict[str, Any] = {
            "replicas": replicas,
            "selector": {
                "matchLabels": {
                    "app": "model-service",
                    "model-slug": model_slug,
                }
            },
            "template": {
                "metadata": {
                    "labels": {
                        "app": "model-service",
                        "model-slug": model_slug,
                    }
                },
                "spec": {
                    "containers": [
                        {
                            "name": "model-service",
                            "image": model_image,
                            "imagePullPolicy": "IfNotPresent",
                            "ports": [{"containerPort": 3000}],
                            "env": [
                                {"name": "MLFLOW_TRACKING_URI", "value": tracking_uri},
                                {"name": "MODEL_NAME", "value": model_name},
                                {"name": "MODEL_VERSION", "value": model_version},
                                {"name": "MODEL_URI", "value": model_uri},
                            ],
                            "readinessProbe": {
                                "httpGet": {"path": "/readyz", "port": 3000},
                                "initialDelaySeconds": 10,
                                "periodSeconds": 10,
                            },
                            "livenessProbe": {
                                "httpGet": {"path": "/livez", "port": 3000},
                                "initialDelaySeconds": 20,
                                "periodSeconds": 15,
                            },
                        }
                    ]
                },
            },
        }

        if registry_secret and "/" in model_image:
            deployment_spec["template"]["spec"]["imagePullSecrets"] = [  # type: ignore[index]
                {"name": registry_secret}
            ]

        return {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {
                "name": f"{self.model_service_prefix}-{model_slug}",
                "namespace": namespace,
                "labels": {"app": "model-service", "model-slug": model_slug},
            },
            "spec": deployment_spec,
        }

    def _build_service_manifest(self, namespace: str, model_slug: str) -> dict[str, Any]:
        return {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {
                "name": f"{self.model_service_prefix}-{model_slug}",
                "namespace": namespace,
                "labels": {"app": "model-service", "model-slug": model_slug},
            },
            "spec": {
                "type": "ClusterIP",
                "selector": {"app": "model-service", "model-slug": model_slug},
                "ports": [{"name": "http", "port": 3000, "targetPort": 3000}],
            },
        }

    async def _run_deploy_operation(self, operation_id: str) -> None:
        operation = self.deploy_operations.get(operation_id)
        if not operation:
            return

        try:
            payload = operation["request"]
            namespace = payload["namespace"]
            model_name = payload["model_name"]
            model_slug = payload["model_slug"]
            model_version = payload["model_version"]
            model_uri = payload["model_uri"]
            tracking_uri = payload["tracking_uri"]
            replicas = payload["replicas"]
            target_image = payload["target_image"]
            base_image = payload["base_image"]
            registry_secret = payload["registry_secret"]
            insecure_registry_tls = payload["insecure_registry_tls"]
            registry_server = payload["registry_server"]
            job_name = payload["job_name"]

            base_url, headers, verify = self._resolve_k8s_auth()
            await self._update_operation(operation_id, status="building", step="create_job")

            job_manifest = self._build_job_manifest(
                job_name=job_name,
                namespace=namespace,
                model_name=model_name,
                model_version=model_version,
                model_uri=model_uri,
                mlflow_tracking_uri=tracking_uri,
                target_image=target_image,
                base_image=base_image,
                registry_secret=registry_secret,
                kaniko_image=self.kaniko_image,
                insecure_registry_tls=insecure_registry_tls,
                registry_server=registry_server,
            )

            jobs_url = f"{base_url}/apis/batch/v1/namespaces/{namespace}/jobs"
            job_url = f"{jobs_url}/{job_name}"

            async with httpx.AsyncClient(timeout=20.0, verify=verify) as client:
                create_response = await client.post(jobs_url, headers=headers, json=job_manifest)
                if create_response.status_code not in {200, 201, 202}:
                    raise RuntimeError(
                        f"Job creation failed ({create_response.status_code}): {create_response.text}"
                    )

                start = asyncio.get_running_loop().time()
                pushed_image = False
                while asyncio.get_running_loop().time() - start < self.build_timeout_sec:
                    response = await client.get(job_url, headers=headers)
                    if response.status_code != 200:
                        await asyncio.sleep(4)
                        continue

                    status = response.json().get("status", {})
                    failed = int(status.get("failed", 0))
                    succeeded = int(status.get("succeeded", 0))

                    if failed > 0:
                        prepare_logs = await self._read_job_logs(
                            client=client,
                            base_url=base_url,
                            headers=headers,
                            namespace=namespace,
                            job_name=job_name,
                            container="prepare-build-context",
                        )
                        kaniko_logs = await self._read_job_logs(
                            client=client,
                            base_url=base_url,
                            headers=headers,
                            namespace=namespace,
                            job_name=job_name,
                            container="kaniko",
                        )
                        raise RuntimeError(
                            "Build Job failed.\n\n"
                            f"[prepare-build-context]\n{prepare_logs}\n\n"
                            f"[kaniko]\n{kaniko_logs}"
                        )

                    if succeeded > 0:
                        break

                    kaniko_logs = await self._read_job_logs(
                        client=client,
                        base_url=base_url,
                        headers=headers,
                        namespace=namespace,
                        job_name=job_name,
                        container="kaniko",
                    )
                    if "Pushing image to" in kaniko_logs and not pushed_image:
                        pushed_image = True
                        await self._update_operation(
                            operation_id,
                            status="pushing",
                            step="kaniko_push",
                        )
                    await asyncio.sleep(4)
                else:
                    raise RuntimeError("Build Job timeout.")

                await self._update_operation(
                    operation_id,
                    status="deploying",
                    step="apply_resources",
                )

                deployment_manifest = self._build_deployment_manifest(
                    namespace=namespace,
                    model_name=model_name,
                    model_slug=model_slug,
                    model_image=target_image,
                    replicas=replicas,
                    tracking_uri=tracking_uri,
                    model_uri="/models/model",
                    model_version="image",
                    registry_secret=registry_secret,
                )
                service_manifest = self._build_service_manifest(namespace, model_slug)

                deploy_name = f"{self.model_service_prefix}-{model_slug}"
                service_name = f"{self.model_service_prefix}-{model_slug}"
                deployments_url = f"{base_url}/apis/apps/v1/namespaces/{namespace}/deployments"
                deployment_url = f"{deployments_url}/{deploy_name}"
                services_url = f"{base_url}/api/v1/namespaces/{namespace}/services"
                service_url = f"{services_url}/{service_name}"

                await self._ensure_resource(
                    client=client,
                    headers=headers,
                    list_url=deployments_url,
                    resource_url=deployment_url,
                    manifest=deployment_manifest,
                )
                await self._ensure_resource(
                    client=client,
                    headers=headers,
                    list_url=services_url,
                    resource_url=service_url,
                    manifest=service_manifest,
                )

                await self._update_operation(operation_id, status="deploying", step="rollout")
                await self._wait_deployment_ready(
                    client=client,
                    headers=headers,
                    deployment_url=deployment_url,
                    replicas=replicas,
                    timeout_sec=self.rollout_timeout_sec,
                )

            await self._update_operation(
                operation_id,
                status="ready",
                step="completed",
                message="Deployment completed successfully.",
            )

        except Exception as exc:
            await self._update_operation(
                operation_id,
                status="failed",
                step="failed",
                error=str(exc),
            )

    @bentoml.api
    async def deploy_model(
        self,
        model_name: str,
        model_version: str = "latest",
        namespace: str = "",
        replicas: int = 1,
        run_id: str = "",
        tracking_uri: str = "",
        model_uri: str = "",
        target_image: str = "",
        insecure_registry_tls: bool = False,
    ) -> dict[str, Any]:
        if not self.auto_deploy_enabled:
            return {"error": "Automatic deploy is disabled on this gateway."}

        namespace = namespace.strip() or self.model_namespace
        model_slug = self._to_model_slug(model_name)
        model_version = (model_version or "latest").strip()
        run_id = run_id.strip()
        tracking_uri = tracking_uri.strip() or self.default_tracking_uri
        model_uri = model_uri.strip()
        target_image = target_image.strip()
        insecure_registry_tls = bool(insecure_registry_tls) or self.kaniko_skip_tls_verify_default

        if not model_slug:
            return {"error": "Invalid model_name. Could not generate slug."}
        if not self._is_valid_k8s_name(namespace):
            return {"error": f"Invalid namespace '{namespace}'."}
        if replicas < 1:
            return {"error": "replicas must be >= 1."}
        if run_id and not re.fullmatch(r"[A-Za-z0-9._-]{4,128}", run_id):
            return {"error": "Invalid run_id format."}
        if not tracking_uri and not model_uri:
            return {"error": "Set tracking_uri or model_uri."}

        registry_server = self.registry_server
        registry_org = self.registry_org
        if not target_image:
            if not registry_server or not registry_org:
                return {
                    "error": (
                        "Missing REGISTRY_SERVER/TU_ORG or pass target_image explicitly."
                    )
                }
            if not self._is_valid_registry_server(registry_server):
                return {"error": f"Invalid REGISTRY_SERVER '{registry_server}'."}
            target_image = self._build_target_image(
                model_slug=model_slug,
                model_version=model_version,
                run_id=run_id,
                registry_server=registry_server,
                org=registry_org,
            )

        if not self._is_valid_image_ref(target_image):
            return {"error": f"Invalid target_image '{target_image}'."}

        if self.model_base_image:
            base_image = self.model_base_image
        else:
            if not registry_server or not registry_org:
                return {
                    "error": "Missing MODEL_BASE_IMAGE and REGISTRY_SERVER/TU_ORG."
                }
            base_image = f"{registry_server}/{registry_org}/model-service-base:py310"

        if not self._is_valid_image_ref(base_image):
            return {"error": f"Invalid base image '{base_image}'."}

        operation_id = self._build_operation_id()
        job_name = f"build-model-{model_slug}-{operation_id[-6:]}"
        job_name = re.sub(r"[^a-z0-9-]+", "-", job_name.lower()).strip("-")[:63].rstrip("-")

        operation = {
            "operation_id": operation_id,
            "status": "queued",
            "step": "queued",
            "created_at": self._now_iso(),
            "updated_at": self._now_iso(),
            "model_name": model_name,
            "model_version": model_version,
            "namespace": namespace,
            "replicas": replicas,
            "image_ref": target_image,
            "k8s_job": job_name,
            "error": None,
            "message": None,
            "request": {
                "namespace": namespace,
                "model_name": model_name,
                "model_slug": model_slug,
                "model_version": model_version,
                "model_uri": model_uri,
                "tracking_uri": tracking_uri,
                "replicas": replicas,
                "target_image": target_image,
                "base_image": base_image,
                "registry_secret": self.registry_secret,
                "insecure_registry_tls": insecure_registry_tls,
                "registry_server": registry_server,
                "job_name": job_name,
            },
        }

        async with self._deploy_lock:
            self.deploy_operations[operation_id] = operation

        asyncio.create_task(self._run_deploy_operation(operation_id))

        return {
            "operation_id": operation_id,
            "status": "queued",
            "model_name": model_name,
            "namespace": namespace,
            "image_ref": target_image,
            "k8s_job": job_name,
        }

    @bentoml.api
    async def deploy_status(self, operation_id: str) -> dict[str, Any]:
        operation = self.deploy_operations.get(operation_id)
        if not operation:
            return {"error": "operation_id not found", "operation_id": operation_id}

        return {
            "operation_id": operation["operation_id"],
            "status": operation["status"],
            "step": operation["step"],
            "model_name": operation["model_name"],
            "model_version": operation["model_version"],
            "namespace": operation["namespace"],
            "image_ref": operation["image_ref"],
            "k8s_job": operation["k8s_job"],
            "error": operation["error"],
            "message": operation["message"],
            "created_at": operation["created_at"],
            "updated_at": operation["updated_at"],
        }

    @bentoml.api
    async def predict(
        self,
        model_name: str,
        data: dict[str, Any],
        model_url: str = "",
    ) -> dict[str, Any]:
        """
        Expected gateway input:
        {
          "model_name": "my_model",
          "data": { ... model payload ... }
        }
        """
        try:
            url, base_url, _ = self._resolve_predict_target(
                model_name=model_name,
                model_url=model_url,
            )
            self.recently_routed_models[model_name.lower()] = base_url

            async with httpx.AsyncClient(timeout=30.0) as client:
                # model_service exposes `predict(data: dict)`, so it expects {"data": {...}}
                response = await client.post(url, json={"data": data})
                response.raise_for_status()
                return response.json()

        except httpx.TimeoutException:
            return {"error": f"Timeout: {model_name}"}
        except ValueError as exc:
            return {
                "error": str(exc),
                "model": model_name,
                "hint": "Use model_url as http(s)://host[:port][/optional-base-path]",
            }
        except Exception as exc:
            return {
                "error": str(exc),
                "model": model_name,
                "configured_models": list(self.model_urls.keys()),
                "requested_model_url": model_url,
                "hint": "Verify that the model service is deployed and reachable",
            }

    @bentoml.api
    async def batch_predict(self, requests: list[dict]) -> dict[str, Any]:
        """Batch predictions."""
        results = []

        async with httpx.AsyncClient(timeout=30.0) as client:
            for request in requests:
                model_name_raw = request.get("model_name")
                model_name = str(model_name_raw).strip() if model_name_raw else ""
                data = request.get("data")
                model_url_raw = request.get("model_url")
                model_url = str(model_url_raw).strip() if model_url_raw else ""

                try:
                    if not model_name:
                        results.append({"error": "model_name is required"})
                        continue

                    url, base_url, _ = self._resolve_predict_target(
                        model_name=model_name,
                        model_url=model_url,
                    )
                    self.recently_routed_models[model_name.lower()] = base_url
                    response = await client.post(url, json={"data": data})
                    response.raise_for_status()
                    results.append(response.json())
                except ValueError as exc:
                    results.append(
                        {
                            "error": str(exc),
                            "model": model_name,
                            "model_url": model_url,
                        }
                    )
                except Exception as exc:
                    results.append(
                        {
                            "error": str(exc),
                            "model": model_name,
                            "model_url": model_url,
                        }
                    )

        return {"results": results}

    @bentoml.api
    async def health(self) -> dict[str, Any]:
        """Health check for models explicitly configured by env."""
        health_status = {}

        async with httpx.AsyncClient(timeout=5.0) as client:
            for model_name, base_url in self.model_urls.items():
                try:
                    response = await client.get(f"{base_url}/health")
                    health_status[model_name] = response.json()
                except Exception as exc:
                    health_status[model_name] = {
                        "status": "unhealthy",
                        "error": str(exc),
                    }

        return {
            "gateway": "healthy",
            "models": health_status,
            "dynamic_routing": {
                "enabled": True,
                "service_prefix": self.model_service_prefix,
                "url_template": self.model_url_template,
            },
        }

    @bentoml.api
    async def list_models(self) -> dict[str, Any]:
        """List available models from env config, recent traffic, and Kubernetes discovery."""
        configured_endpoints = {
            name: f"{url}/predict" for name, url in self.model_urls.items()
        }

        recently_routed_endpoints = {
            name: f"{url}/predict"
            for name, url in self.recently_routed_models.items()
        }

        discovered_services, discovery_error = await self._discover_k8s_model_services()
        discovered_endpoints = {
            service["inferred_model_name"]: service["predict_endpoint"]
            for service in discovered_services
        }

        endpoints: dict[str, str] = {}
        endpoints.update(configured_endpoints)
        endpoints.update(discovered_endpoints)
        endpoints.update(recently_routed_endpoints)

        operations_summary = [
            {
                "operation_id": op["operation_id"],
                "status": op["status"],
                "model_name": op["model_name"],
                "image_ref": op["image_ref"],
                "updated_at": op["updated_at"],
            }
            for op in self.deploy_operations.values()
        ]
        operations_summary.sort(key=lambda item: item["updated_at"], reverse=True)

        return {
            "configured_models": sorted(self.model_urls.keys()),
            "recently_routed_models": sorted(self.recently_routed_models.keys()),
            "discovered_models": discovered_services,
            "dynamic_routing": {
                "enabled": True,
                "service_prefix": self.model_service_prefix,
                "url_template": self.model_url_template,
                "namespace": self.model_namespace,
                "external_url_support": {
                    "enabled": True,
                    "field": "model_url",
                    "accepted_schemes": ["http", "https"],
                    "priority": "model_url > MODELO_*_URL > model-<slug> convention",
                },
                "how_to_call": (
                    "If model_name is not in configured_models, "
                    "it is resolved using model-<slug> convention."
                ),
            },
            "discovery": {
                "source": "kubernetes_services",
                "status": "ok" if discovery_error is None else "error",
                "error": discovery_error,
            },
            "deploy_operations": operations_summary[:20],
            "endpoints": endpoints,
        }
