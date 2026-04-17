"""Template loading helpers for Kubernetes manifest generation."""

from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml

_TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"


def load_template_yaml(template_name: str) -> dict[str, Any]:
    """Load a YAML template file as a mutable mapping."""

    path = _TEMPLATES_DIR / template_name
    content = path.read_text(encoding="utf-8")
    data = yaml.safe_load(content)
    if not isinstance(data, dict):
        raise ValueError(f"Template {template_name} must be a YAML mapping.")
    return deepcopy(data)


def load_template_text(template_name: str) -> str:
    """Load a raw text template from the manifests template directory."""

    path = _TEMPLATES_DIR / template_name
    return path.read_text(encoding="utf-8")
