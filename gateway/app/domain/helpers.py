import re
from typing import Mapping


def as_bool(value: str | None, default: bool = False) -> bool:
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


def positive_int_from_env(env: Mapping[str, str], key: str, default: int) -> int:
    raw = env.get(key)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def normalize_registry_server(value: str) -> str:
    normalized = value.strip().lower()
    if normalized.startswith("http://"):
        normalized = normalized[7:]
    elif normalized.startswith("https://"):
        normalized = normalized[8:]
    if "/" in normalized:
        normalized = normalized.split("/", 1)[0]
    return normalized


def is_valid_registry_server(value: str) -> bool:
    return bool(re.fullmatch(r"[a-z0-9.-]+(?::[0-9]+)?", value))


def is_valid_k8s_name(value: str) -> bool:
    return bool(re.fullmatch(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?", value))


def is_valid_image_ref(value: str) -> bool:
    return bool(re.fullmatch(r"[a-z0-9./:_-]+", value))


def to_model_slug(model_name: str) -> str:
    slug = model_name.strip().lower().replace("_", "-")
    slug = re.sub(r"[^a-z0-9-]", "-", slug)
    slug = re.sub(r"-+", "-", slug).strip("-")
    return slug


def slug_to_model_name(model_slug: str) -> str:
    return model_slug.replace("-", "_")
