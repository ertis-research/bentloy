from typing import Any, Callable

import httpx

from ..routing.target_resolver import PredictTargetResolver


def _default_client_factory(timeout: float) -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=timeout)


class PredictHandler:
    """Prediction request handling for single and batch requests."""

    def __init__(
        self,
        target_resolver: PredictTargetResolver,
        model_urls: dict[str, str],
        recently_routed_models: dict[str, str],
        client_factory: Callable[[float], httpx.AsyncClient] | None = None,
    ):
        self.target_resolver = target_resolver
        self.model_urls = model_urls
        self.recently_routed_models = recently_routed_models
        self.client_factory = client_factory or _default_client_factory

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
            url, base_url, _ = self.target_resolver.resolve_predict_target(
                model_name=model_name,
                model_url=model_url,
            )
            self.recently_routed_models[model_name.lower()] = base_url

            async with self.client_factory(30.0) as client:
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

    async def batch_predict(self, requests: list[dict[str, Any]]) -> dict[str, Any]:
        """Batch predictions."""
        results: list[dict[str, Any]] = []

        async with self.client_factory(30.0) as client:
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

                    url, base_url, _ = self.target_resolver.resolve_predict_target(
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
