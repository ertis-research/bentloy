import bentoml
import httpx
import os
import re
from typing import Any

@bentoml.service(
    resources={"cpu": "1"},
    traffic={"timeout": 60}
)
class GatewayService:
    """API Gateway para enrutar a múltiples modelos"""
    
    def __init__(self):
        # Leer URLs de modelos desde variables de entorno
        # Formato: MODELO_A_URL=http://modelo-a:3000
        self.model_urls = {}
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
        
        print(f"Modelos registrados: {list(self.model_urls.keys())}")

    def _to_model_slug(self, model_name: str) -> str:
        slug = model_name.strip().lower().replace("_", "-")
        slug = re.sub(r"[^a-z0-9-]", "-", slug)
        slug = re.sub(r"-+", "-", slug).strip("-")
        return slug

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
    
    @bentoml.api
    async def predict(self, model_name: str, data: dict[str, Any]) -> dict[str, Any]:
        """
        Enruta predicciones al modelo correspondiente.
        
        Input:
        {
            "model_name": "modelo_a",
            "data": {"feature1": 1.0, "feature2": 2.0}
        }
        """
        try:
            base_url, _ = self._resolve_model_base_url(model_name)
            url = f"{base_url}/predict"
            
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(url, json=data)
                response.raise_for_status()
                return response.json()
                
        except httpx.TimeoutException:
            return {"error": f"Timeout: {model_name}"}
        except Exception as e:
            return {
                "error": str(e),
                "model": model_name,
                "configured_models": list(self.model_urls.keys()),
                "hint": "Verifica que el servicio del modelo este desplegado y accesible",
            }
    
    @bentoml.api
    async def batch_predict(self, requests: list[dict]) -> dict[str, Any]:
        """Predicciones en lote"""
        results = []
        
        async with httpx.AsyncClient(timeout=30.0) as client:
            for req in requests:
                model_name = req.get("model_name")
                data = req.get("data")
                
                try:
                    if not model_name:
                        results.append({"error": "model_name es obligatorio"})
                        continue

                    base_url, _ = self._resolve_model_base_url(model_name)
                    url = f"{base_url}/predict"
                    response = await client.post(url, json=data)
                    response.raise_for_status()
                    results.append(response.json())
                except Exception as e:
                    results.append({"error": str(e), "model": model_name})
        
        return {"results": results}
    
    @bentoml.api
    async def health(self) -> dict[str, Any]:
        """Health check de todos los modelos"""
        health_status = {}
        
        async with httpx.AsyncClient(timeout=5.0) as client:
            for model_name, base_url in self.model_urls.items():
                try:
                    response = await client.get(f"{base_url}/health")
                    health_status[model_name] = response.json()
                except Exception as e:
                    health_status[model_name] = {
                        "status": "unhealthy",
                        "error": str(e)
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
    def list_models(self) -> dict[str, Any]:
        """Lista modelos disponibles"""
        return {
            "configured_models": list(self.model_urls.keys()),
            "dynamic_routing": {
                "enabled": True,
                "service_prefix": self.model_service_prefix,
                "url_template": self.model_url_template,
                "how_to_call": "Si model_name no esta en configured_models, se intentara resolver por convencion",
            },
            "endpoints": {
                name: f"{url}/predict" 
                for name, url in self.model_urls.items()
            }
        }