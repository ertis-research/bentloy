import os
import re
from typing import Any

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

        # Stores models observed in predict/batch_predict calls.
        self.recently_routed_models: dict[str, str] = {}

        print(f"Configured models from env: {list(self.model_urls.keys())}")

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

    async def _discover_k8s_model_services(self) -> tuple[list[dict[str, str]], str | None]:
        """
        Discover model-<slug> services using Kubernetes API from inside the pod.
        If discovery is not possible (RBAC, outside cluster, etc.), return a descriptive error.
        """
        kube_host = os.getenv("KUBERNETES_SERVICE_HOST")
        kube_port = os.getenv("KUBERNETES_SERVICE_PORT_HTTPS", "443")
        token_path = "/var/run/secrets/kubernetes.io/serviceaccount/token"
        ca_path = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"

        if not kube_host:
            return [], "KUBERNETES_SERVICE_HOST is not available (outside cluster or missing service DNS)."

        if not os.path.exists(token_path):
            return [], "ServiceAccount token is not available to query the Kubernetes API."

        with open(token_path, "r", encoding="utf-8") as file_handle:
            token = file_handle.read().strip()

        if not token:
            return [], "ServiceAccount token is empty."

        verify: str | bool = ca_path if os.path.exists(ca_path) else False
        base_url = f"https://{kube_host}:{kube_port}"
        url = f"{base_url}/api/v1/namespaces/{self.model_namespace}/services"
        headers = {"Authorization": f"Bearer {token}"}

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

    @bentoml.api
    async def predict(self, model_name: str, data: dict[str, Any]) -> dict[str, Any]:
        """
        Expected gateway input:
        {
          "model_name": "my_model",
          "data": { ... model payload ... }
        }
        """
        try:
            base_url, _ = self._resolve_model_base_url(model_name)
            self.recently_routed_models[model_name.lower()] = base_url
            url = f"{base_url}/predict"

            async with httpx.AsyncClient(timeout=30.0) as client:
                # model_service exposes `predict(data: dict)`, so it expects {"data": {...}}
                response = await client.post(url, json={"data": data})
                response.raise_for_status()
                return response.json()

        except httpx.TimeoutException:
            return {"error": f"Timeout: {model_name}"}
        except Exception as exc:
            return {
                "error": str(exc),
                "model": model_name,
                "configured_models": list(self.model_urls.keys()),
                "hint": "Verify that the model service is deployed and reachable",
            }

    @bentoml.api
    async def batch_predict(self, requests: list[dict]) -> dict[str, Any]:
        """Batch predictions."""
        results = []

        async with httpx.AsyncClient(timeout=30.0) as client:
            for request in requests:
                model_name = request.get("model_name")
                data = request.get("data")

                try:
                    if not model_name:
                        results.append({"error": "model_name is required"})
                        continue

                    base_url, _ = self._resolve_model_base_url(model_name)
                    self.recently_routed_models[model_name.lower()] = base_url
                    url = f"{base_url}/predict"
                    response = await client.post(url, json={"data": data})
                    response.raise_for_status()
                    results.append(response.json())
                except Exception as exc:
                    results.append({"error": str(exc), "model": model_name})

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

        return {
            "configured_models": sorted(self.model_urls.keys()),
            "recently_routed_models": sorted(self.recently_routed_models.keys()),
            "discovered_models": discovered_services,
            "dynamic_routing": {
                "enabled": True,
                "service_prefix": self.model_service_prefix,
                "url_template": self.model_url_template,
                "namespace": self.model_namespace,
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
            "endpoints": endpoints,
        }
