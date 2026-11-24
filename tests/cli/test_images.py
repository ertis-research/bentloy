"""Tests for image reference resolution and source hashing."""

import re
from collections.abc import Callable
from pathlib import Path

import pytest

import bentloy


def test_tags_are_content_hashes(make_config: Callable[..., dict[str, str]]) -> None:
    """Image tags are content hashes and are stable across calls."""
    images = bentloy.resolve_images(make_config())
    gateway_ref = images["gateway"]["ref"]
    base_ref = images["model-base"]["ref"]
    assert re.search(r"^registry\.example\.com/my-org/model-gateway:[0-9a-f]{12}$", gateway_ref)
    assert re.search(
        r"^registry\.example\.com/my-org/model-service-base:py312-[0-9a-f]{12}$", base_ref
    )
    assert images == bentloy.resolve_images(make_config())


def test_refs_without_namespace(make_config: Callable[..., dict[str, str]]) -> None:
    """Without a registry namespace the refs sit directly under the registry host."""
    images = bentloy.resolve_images(make_config(REGISTRY_NAMESPACE=""))
    assert re.search(
        r"^registry\.example\.com/model-gateway:[0-9a-f]{12}$", images["gateway"]["ref"]
    )
    assert re.search(
        r"^registry\.example\.com/model-service-base:py312-[0-9a-f]{12}$",
        images["model-base"]["ref"],
    )


def test_registry_is_required_but_namespace_is_not(make_config: Callable[..., dict[str, str]]) -> None:
    """A missing REGISTRY_SERVER fails with a message pointing at .env."""
    with pytest.raises(bentloy.CliError, match="Set REGISTRY_SERVER in .env"):
        bentloy.resolve_images(make_config(REGISTRY_SERVER=""))


def test_overrides_are_not_built(make_config: Callable[..., dict[str, str]]) -> None:
    """An image override is used as is and is not built."""
    images = bentloy.resolve_images(make_config(GATEWAY_IMAGE="reg/x/gw:1"))
    assert images["gateway"] == {
        "ref": "reg/x/gw:1",
        "build": False,
        "context": bentloy.IMAGES["gateway"]["context"],
    }
    assert images["model-base"]["build"]


def test_missing_registry_fails(make_config: Callable[..., dict[str, str]]) -> None:
    """Resolving images without a registry raises CliError."""
    with pytest.raises(bentloy.CliError):
        bentloy.resolve_images(make_config(REGISTRY_SERVER=""))


def test_source_hash_ignores_caches_and_line_endings(tmp_path: Path) -> None:
    """The source hash ignores caches, tests and line endings but reacts to content changes."""
    ctx = tmp_path
    (ctx / "app").mkdir()
    (ctx / "Dockerfile").write_bytes(b"FROM x\nRUN y\n")
    (ctx / "app" / "m.py").write_bytes(b"a = 1\n")
    first = bentloy.source_hash(ctx)

    (ctx / "Dockerfile").write_bytes(b"FROM x\r\nRUN y\r\n")
    (ctx / "app" / "__pycache__").mkdir()
    (ctx / "app" / "__pycache__" / "m.cpython-312.pyc").write_bytes(b"junk")
    (ctx / "tests").mkdir()
    (ctx / "tests" / "t.py").write_bytes(b"not in image")
    assert bentloy.source_hash(ctx) == first

    (ctx / "app" / "m.py").write_bytes(b"a = 2\n")
    assert bentloy.source_hash(ctx) != first
