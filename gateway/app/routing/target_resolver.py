from urllib.parse import urlparse

from ..domain.helpers import to_model_slug


class PredictTargetResolver:
    """Resolve where a prediction request should be routed."""

    def __init__(
        self,
        model_urls: dict[str, str],
        model_service_prefix: str,
        model_namespace: str,
        model_url_template: str,
    ) -> None:
        # Keep normalized keys for stable lookup.
        self.model_urls = {k.lower(): v for k, v in model_urls.items()}
        self.model_service_prefix = model_service_prefix
        self.model_namespace = model_namespace
        self.model_url_template = model_url_template

    def resolve_model_base_url(self, model_name: str) -> tuple[str, bool]:
        model_key = model_name.lower()
        if model_key in self.model_urls:
            return self.model_urls[model_key], True

        model_slug = to_model_slug(model_name)
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

    def normalize_external_model_url(self, model_url: str) -> str:
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

    def resolve_predict_target(
        self,
        model_name: str,
        model_url: str = "",
    ) -> tuple[str, str, str]:
        explicit_url = (model_url or "").strip()
        if explicit_url:
            normalized = self.normalize_external_model_url(explicit_url)
            if normalized.endswith("/predict"):
                base_url = normalized[: -len("/predict")]
                predict_url = normalized
            else:
                base_url = normalized
                predict_url = f"{normalized}/predict"
            return predict_url, base_url, "external_url"

        base_url, from_config = self.resolve_model_base_url(model_name)
        source = "env_mapping" if from_config else "k8s_convention"
        return f"{base_url}/predict", base_url, source
