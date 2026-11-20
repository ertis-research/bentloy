"""Domain-level helper functions for validation and name normalization."""

import re
from typing import Mapping


def as_bool(value: str | None, default: bool = False) -> bool:
    """Parse a boolean-like string and return a fallback when invalid."""

    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


def positive_int_from_env(env: Mapping[str, str], key: str, default: int) -> int:
    """Read a positive integer setting from a mapping with safe fallback."""

    raw = env.get(key)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def image_repository(registry_server: str, namespace: str, repository: str) -> str:
    """Join registry host, optional namespace and repository (`host[/namespace]/repo`)."""

    parts = [registry_server.strip().strip("/"), namespace.strip().strip("/"), repository.strip("/")]
    return "/".join(part for part in parts if part)


def normalize_registry_server(value: str) -> str:
    """Normalize a registry server input to host[:port] format."""

    normalized = value.strip().lower()
    if normalized.startswith("http://"):
        normalized = normalized[7:]
    elif normalized.startswith("https://"):
        normalized = normalized[8:]
    if "/" in normalized:
        normalized = normalized.split("/", 1)[0]
    return normalized


def is_valid_registry_server(value: str) -> bool:
    """Return whether the value matches a registry host[:port] pattern."""

    return bool(re.fullmatch(r"[a-z0-9.-]+(?::[0-9]+)?", value))


def is_valid_k8s_name(value: str) -> bool:
    """Return whether the value is a valid Kubernetes DNS label."""

    return bool(re.fullmatch(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?", value))


def is_valid_image_ref(value: str) -> bool:
    """Return whether the value only contains allowed image-ref characters."""

    return bool(re.fullmatch(r"[a-z0-9./:_-]+", value))


def to_model_slug(model_name: str) -> str:
    """Convert a model name into a Kubernetes-safe slug."""

    slug = model_name.strip().lower().replace("_", "-")
    slug = re.sub(r"[^a-z0-9-]", "-", slug)
    slug = re.sub(r"-+", "-", slug).strip("-")
    return slug


_LABEL_VALUE_RE = re.compile(r"([A-Za-z0-9]([A-Za-z0-9._-]{0,61}[A-Za-z0-9])?)?")


def is_valid_label_value(value: str) -> bool:
    """Return whether the value is a valid Kubernetes label value (empty allowed)."""

    return bool(_LABEL_VALUE_RE.fullmatch(value))


def to_label_value(value: str) -> str:
    """Convert free text into a valid Kubernetes label value (slug, max 63 chars)."""

    return to_model_slug(value)[:63].strip("-")


def slug_to_model_name(model_slug: str) -> str:
    """Convert a slug back to the model naming convention with underscores."""

    return model_slug.replace("-", "_")
