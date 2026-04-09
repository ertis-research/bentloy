import os
import re
from typing import Any

import bentoml
import httpx


@bentoml.service(resources={"cpu": "1"}, traffic={"timeout": 60})
class GatewayService:
    """API Gateway para enrutar predicciones a multiples modelos."""

    def __init__(self):
        # Mapeo explicito por variables de entorno tipo MODELO_FOO_URL=http://...:3000
        self.model_urls: dict[str, str] = {}
        for key, value in os.environ.items():
            if key.endswith("_URL") and key.startswith("MODELO_"):
                model_name = key.replace("MODELO_", "").replace("_URL", "").lower()
                self.model_urls[model_name] = value

        # Resolucion dinamica por convencion para servicios de Kubernetes.
        self.model_service_prefix = os.getenv("MODEL_SERVICE_PREFIX", "model")
        self.model_namespace = os.getenv("MODEL_NAMESPACE", "default")
        self.model_url_template = os.getenv(
            "MODEL_URL_TEMPLATE",
            "http://{service_name}:3000",
        )

        # Guarda modelos vistos en peticiones predict/batch_predict.
        self.recently_routed_models: dict[str, str] = {}

        print(f"Modelos configurados por env: {list(self.model_urls.keys())}")

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
        Descubre servicios tipo model-<slug> usando la API de Kubernetes desde dentro del pod.
        Si no se puede consultar (RBAC, fuera de cluster, etc.), devuelve error descriptivo.
        """
        kube_host = os.getenv("KUBERNETES_SERVICE_HOST")
        kube_port = os.getenv("KUBERNETES_SERVICE_PORT_HTTPS", "443")
        token_path = "/var/run/secrets/kubernetes.io/serviceaccount/token"
        ca_path = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"

        if not kube_host:
            return [], "KUBERNETES_SERVICE_HOST no disponible (fuera de cluster o sin DNS de servicio)."

        if not os.path.exists(token_path):
            return [], "Token de ServiceAccount no disponible para consultar la API de Kubernetes."

        with open(token_path, "r", encoding="utf-8") as file_handle:
            token = file_handle.read().strip()

        if not token:
            return [], "Token de ServiceAccount vacio."

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
            return [], f"No se pudo descubrir servicios en Kubernetes: {exc}"

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
        Input esperado por gateway:
        {
          "model_name": "mi_modelo",
          "data": { ... payload del modelo ... }
        }
        """
        try:
            base_url, _ = self._resolve_model_base_url(model_name)
            self.recently_routed_models[model_name.lower()] = base_url
            url = f"{base_url}/predict"

            async with httpx.AsyncClient(timeout=30.0) as client:
                # model_service expone `predict(data: dict)`, por lo que espera {"data": {...}}
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
                "hint": "Verifica que el servicio del modelo este desplegado y accesible",
            }

    @bentoml.api
    async def batch_predict(self, requests: list[dict]) -> dict[str, Any]:
        """Predicciones en lote."""
        results = []

        async with httpx.AsyncClient(timeout=30.0) as client:
            for request in requests:
                model_name = request.get("model_name")
                data = request.get("data")

                try:
                    if not model_name:
                        results.append({"error": "model_name es obligatorio"})
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
        """Health check de modelos configurados explicitamente por env."""
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
        """Lista modelos disponibles por tres fuentes: env, trafico reciente y discovery de Kubernetes."""
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
                "how_to_call": "Si model_name no esta en configured_models, se resuelve por convencion model-<slug>.",
            },
            "discovery": {
                "source": "kubernetes_services",
                "status": "ok" if discovery_error is None else "error",
                "error": discovery_error,
            },
            "endpoints": endpoints,
        }
