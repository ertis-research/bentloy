#!/usr/bin/env python3
"""Bentloy CLI: build the images, deploy the gateway and manage models.

    python bentloy.py up                 # build/push images if needed + deploy gateway
    python bentloy.py up --dry-run       # show the plan and rendered manifests only
    python bentloy.py build              # only build/push images (e.g. CI), print refs
    python bentloy.py deploy my_model    # build + deploy a model through the gateway
    python bentloy.py status <op_id>     # deploy operation status
    python bentloy.py models             # list models known by the gateway
    python bentloy.py predict my_model x1=1 x2=2   # prediction (or a JSON object/list)
    python bentloy.py delete my_model    # delete a deployed model

Configuration comes from `.env` (see `.env_example`) and the environment, the
environment taking precedence. Only the Python standard library is used; `docker`
and `kubectl` must be on PATH for the commands that need them.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import socket
import string
import subprocess
import sys
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

ROOT = Path(__file__).resolve().parent
TEMPLATES_DIR = ROOT / "k8s" / "templates"

# Defaults for every optional setting (future Helm `values.yaml`).
DEFAULTS: dict[str, str] = {
    "K8S_NAMESPACE": "default",
    "MODEL_SERVICE_PREFIX": "bentloy-model",
    "MODEL_URL_TEMPLATE": "http://{service_name}:3000",
    "MLFLOW_TRACKING_URI": "",
    "REGISTRY_SERVER": "",
    "REGISTRY_NAMESPACE": "",
    "REGISTRY_USERNAME": "",
    "REGISTRY_PASSWORD": "",
    "MODEL_REGISTRY_SECRET": "bentloy-registry-creds",
    "GATEWAY_IMAGE": "",
    "MODEL_BASE_IMAGE": "",
    "GATEWAY_PORT": "8000",
    "GATEWAY_REPLICAS": "1",
    "GATEWAY_SERVICE_TYPE": "LoadBalancer",
    "GATEWAY_URL": "",
    "KANIKO_IMAGE": "gcr.io/kaniko-project/executor:v1.23.2-debug",
    "KANIKO_SKIP_TLS_VERIFY": "0",
    "KANIKO_CACHE": "1",
    "MODEL_PIP_EXTRA_INDEX_URL": "https://download.pytorch.org/whl/cpu",
    "AUTO_DEPLOY_ENABLED": "1",
    "DEPLOY_BUILD_TIMEOUT_SEC": "1800",
    "DEPLOY_ROLLOUT_TIMEOUT_SEC": "300",
}

# Images built from this repository. The tag is a hash of the files that end up in
# the image, so an image is only rebuilt when its content changes.
IMAGES: dict[str, dict[str, Any]] = {
    "gateway": {
        "context": ROOT / "gateway",
        "repository": "model-gateway",
        "tag_prefix": "",
        "override": "GATEWAY_IMAGE",
    },
    "model-base": {
        "context": ROOT / "model_service",
        "repository": "model-service-base",
        "tag_prefix": "py312-",
        "override": "MODEL_BASE_IMAGE",
    },
}
IMAGE_INPUTS = ("Dockerfile", "requirements.txt", "service.py", "app")

TERMINAL_STATUSES = {"ready", "failed"}
K8S_NAME_RE = re.compile(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?")
TRUE_VALUES = {"1", "true", "yes", "on"}


class CliError(Exception):
    """Expected failure: printed without a traceback."""


# --------------------------------------------------------------------------- config


def load_config(env_file: Path) -> dict[str, str]:
    """Merge defaults, `.env` and the process environment (highest priority)."""

    config = dict(DEFAULTS)
    if env_file.is_file():
        for raw in env_file.read_text(encoding="utf-8-sig").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = (part.strip() for part in line.split("=", 1))
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
                config[key] = value
    for key in set(config) | set(DEFAULTS):
        if os.environ.get(key):
            config[key] = os.environ[key]
    return config


def is_true(value: str) -> bool:
    return value.strip().lower() in TRUE_VALUES


def normalize_registry(value: str) -> str:
    value = value.strip().lower().removeprefix("https://").removeprefix("http://")
    return value.split("/", 1)[0]


def positive_int(config: dict[str, str], key: str) -> int:
    value = config[key].strip()
    if not value.isdigit() or int(value) < 1:
        raise CliError(f"{key} must be a positive integer (got '{value}').")
    return int(value)


def k8s_name(config: dict[str, str], key: str) -> str:
    value = config[key].strip()
    if not K8S_NAME_RE.fullmatch(value):
        raise CliError(f"{key} '{value}' is not a valid Kubernetes name.")
    return value


def to_model_slug(name: str) -> str:
    """Same slug rules as the gateway (gateway/app/domain/helpers.py)."""

    slug = re.sub(r"[^a-z0-9-]", "-", name.strip().lower().replace("_", "-"))
    return re.sub(r"-+", "-", slug).strip("-")


# --------------------------------------------------------------------------- images


def source_hash(context: Path) -> str:
    """Hash the files copied into the image (line endings normalized)."""

    digest = hashlib.sha256()
    files: list[Path] = []
    for name in IMAGE_INPUTS:
        path = context / name
        if path.is_dir():
            files.extend(
                p for p in path.rglob("*")
                if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"
            )
        elif path.is_file():
            files.append(path)
    for path in sorted(files, key=lambda p: p.relative_to(context).as_posix()):
        digest.update(path.relative_to(context).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes().replace(b"\r\n", b"\n"))
        digest.update(b"\0")
    return digest.hexdigest()[:12]


def image_repository(registry: str, namespace: str, repository: str) -> str:
    """Join registry host, optional namespace and repository (`host[/namespace]/repo`)."""

    return "/".join(part for part in (registry, namespace.strip().strip("/"), repository) if part)


def resolve_images(config: dict[str, str]) -> dict[str, dict[str, Any]]:
    """Return {image_key: {"ref": ..., "build": bool, "context": Path}}."""

    registry = normalize_registry(config["REGISTRY_SERVER"])
    namespace = config["REGISTRY_NAMESPACE"].strip().strip("/")
    resolved: dict[str, dict[str, Any]] = {}
    for key, spec in IMAGES.items():
        override = config[spec["override"]].strip()
        if override:
            resolved[key] = {"ref": override, "build": False, "context": spec["context"]}
            continue
        if not registry:
            raise CliError(
                f"Set REGISTRY_SERVER in .env (or {spec['override']}) "
                "to know where to push the images."
            )
        tag = spec["tag_prefix"] + source_hash(spec["context"])
        resolved[key] = {
            "ref": f"{image_repository(registry, namespace, spec['repository'])}:{tag}",
            "build": True,
            "context": spec["context"],
        }
    return resolved


# --------------------------------------------------------------------------- manifests


def quote(value: Any) -> str:
    """Render a value as a YAML double-quoted string (JSON strings are valid YAML)."""

    return json.dumps(str(value))


def render(template_name: str, values: dict[str, Any]) -> str:
    template = string.Template((TEMPLATES_DIR / template_name).read_text(encoding="utf-8"))
    try:
        return template.substitute(values)
    except KeyError as exc:
        raise CliError(f"Template {template_name} uses an unknown value: {exc}") from exc


def gateway_values(config: dict[str, str], images: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Values for k8s/templates/gateway.yaml (future Helm values)."""

    gateway_config = {
        "K8S_NAMESPACE": k8s_name(config, "K8S_NAMESPACE"),
        "MODEL_SERVICE_PREFIX": config["MODEL_SERVICE_PREFIX"],
        "MODEL_URL_TEMPLATE": config["MODEL_URL_TEMPLATE"],
        "MLFLOW_TRACKING_URI": config["MLFLOW_TRACKING_URI"],
        "REGISTRY_SERVER": normalize_registry(config["REGISTRY_SERVER"]),
        "REGISTRY_NAMESPACE": config["REGISTRY_NAMESPACE"].strip(),
        "MODEL_BASE_IMAGE": images["model-base"]["ref"],
        "MODEL_REGISTRY_SECRET": k8s_name(config, "MODEL_REGISTRY_SECRET"),
        "KANIKO_IMAGE": config["KANIKO_IMAGE"],
        "KANIKO_SKIP_TLS_VERIFY": config["KANIKO_SKIP_TLS_VERIFY"],
        "KANIKO_CACHE": config["KANIKO_CACHE"],
        "MODEL_PIP_EXTRA_INDEX_URL": config["MODEL_PIP_EXTRA_INDEX_URL"].strip(),
        "AUTO_DEPLOY_ENABLED": config["AUTO_DEPLOY_ENABLED"],
        "DEPLOY_BUILD_TIMEOUT_SEC": str(positive_int(config, "DEPLOY_BUILD_TIMEOUT_SEC")),
        "DEPLOY_ROLLOUT_TIMEOUT_SEC": str(positive_int(config, "DEPLOY_ROLLOUT_TIMEOUT_SEC")),
    }
    checksum = hashlib.sha256(json.dumps(gateway_config, sort_keys=True).encode()).hexdigest()
    service_type = config["GATEWAY_SERVICE_TYPE"].strip()
    if service_type not in {"LoadBalancer", "NodePort", "ClusterIP"}:
        raise CliError("GATEWAY_SERVICE_TYPE must be LoadBalancer, NodePort or ClusterIP.")

    return {
        "namespace": quote(gateway_config["K8S_NAMESPACE"]),
        "registry_secret": quote(gateway_config["MODEL_REGISTRY_SECRET"]),
        "gateway_image": quote(images["gateway"]["ref"]),
        "gateway_replicas": positive_int(config, "GATEWAY_REPLICAS"),
        "gateway_port": positive_int(config, "GATEWAY_PORT"),
        "gateway_service_type": quote(service_type),
        "model_service_prefix": quote(gateway_config["MODEL_SERVICE_PREFIX"]),
        "model_url_template": quote(gateway_config["MODEL_URL_TEMPLATE"]),
        "mlflow_tracking_uri": quote(gateway_config["MLFLOW_TRACKING_URI"]),
        "registry_server": quote(gateway_config["REGISTRY_SERVER"]),
        "registry_namespace": quote(gateway_config["REGISTRY_NAMESPACE"]),
        "model_base_image": quote(gateway_config["MODEL_BASE_IMAGE"]),
        "kaniko_image": quote(gateway_config["KANIKO_IMAGE"]),
        "kaniko_skip_tls_verify": quote(gateway_config["KANIKO_SKIP_TLS_VERIFY"]),
        "kaniko_cache": quote(gateway_config["KANIKO_CACHE"]),
        "model_pip_extra_index_url": quote(gateway_config["MODEL_PIP_EXTRA_INDEX_URL"]),
        "auto_deploy_enabled": quote(gateway_config["AUTO_DEPLOY_ENABLED"]),
        "deploy_build_timeout_sec": quote(gateway_config["DEPLOY_BUILD_TIMEOUT_SEC"]),
        "deploy_rollout_timeout_sec": quote(gateway_config["DEPLOY_ROLLOUT_TIMEOUT_SEC"]),
        "config_checksum": quote(checksum[:16]),
    }


def registry_secret_manifest(config: dict[str, str], redact: bool = False) -> str | None:
    """Render the registry Secret, or None when credentials are not configured."""

    username = config["REGISTRY_USERNAME"]
    password = config["REGISTRY_PASSWORD"]
    server = normalize_registry(config["REGISTRY_SERVER"])
    if not (server and username and password):
        return None
    if redact:
        encoded = "<redacted>"
    else:
        auth = base64.b64encode(f"{username}:{password}".encode()).decode()
        docker_config = {"auths": {server: {"username": username, "password": password, "auth": auth}}}
        encoded = base64.b64encode(json.dumps(docker_config).encode()).decode()
    return render(
        "registry-secret.yaml",
        {
            "namespace": quote(k8s_name(config, "K8S_NAMESPACE")),
            "registry_secret": quote(k8s_name(config, "MODEL_REGISTRY_SECRET")),
            "dockerconfigjson": quote(encoded),
        },
    )


# --------------------------------------------------------------------------- processes


def require_tool(name: str) -> None:
    if shutil.which(name) is None:
        raise CliError(f"'{name}' is not installed or not in PATH.")


def run(cmd: list[str], *, input_text: str | None = None, capture: bool = False) -> str:
    """Run a command, streaming its output unless `capture` is set."""

    print(f"$ {' '.join(cmd)}", flush=True)
    result = subprocess.run(
        cmd,
        input=input_text,
        text=True,
        capture_output=capture,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip() if capture else ""
        raise CliError(f"Command failed ({result.returncode}): {' '.join(cmd)}\n{detail}".rstrip())
    return result.stdout if capture else ""


def kubectl_apply(manifest: str) -> None:
    run(["kubectl", "apply", "-f", "-"], input_text=manifest)


def image_exists(ref: str, insecure: bool) -> bool:
    cmd = ["docker", "manifest", "inspect"] + (["--insecure"] if insecure else []) + [ref]
    return subprocess.run(cmd, capture_output=True, text=True).returncode == 0


# --------------------------------------------------------------------------- gateway API


@contextmanager
def gateway_connection(config: dict[str, str], explicit_url: str) -> Iterator[str]:
    """Yield the gateway base URL, port-forwarding the Service when no URL is given."""

    url = (explicit_url or config["GATEWAY_URL"]).strip().rstrip("/")
    if url:
        yield url
        return

    require_tool("kubectl")
    namespace = k8s_name(config, "K8S_NAMESPACE")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        local_port = sock.getsockname()[1]
    remote_port = positive_int(config, "GATEWAY_PORT")
    proc = subprocess.Popen(
        ["kubectl", "-n", namespace, "port-forward", "svc/bentloy-gateway", f"{local_port}:{remote_port}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 30
        while True:
            if proc.poll() is not None:
                raise CliError(
                    "Could not port-forward to svc/bentloy-gateway "
                    f"in namespace '{namespace}':\n{(proc.stderr.read() if proc.stderr else '').strip()}"
                )
            try:
                with socket.create_connection(("127.0.0.1", local_port), timeout=1):
                    break
            except OSError:
                if time.monotonic() > deadline:
                    raise CliError("Timed out waiting for kubectl port-forward to svc/bentloy-gateway.")
                time.sleep(0.5)
        yield f"http://127.0.0.1:{local_port}"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def call_gateway(base_url: str, endpoint: str, payload: dict[str, Any], retries: int = 0) -> Any:
    """POST a JSON payload to a BentoML endpoint and return the decoded response."""

    request = urllib.request.Request(
        f"{base_url}/{endpoint}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                body = response.read().decode() or "null"
            return json.loads(body)
        except urllib.error.HTTPError as exc:
            raise CliError(f"Gateway /{endpoint} returned {exc.code}: {exc.read().decode()}") from exc
        except (urllib.error.URLError, ConnectionError, TimeoutError) as exc:
            if attempt >= retries:
                raise CliError(f"Could not reach the gateway at {base_url}: {exc}") from exc
            time.sleep(3)
    return None


def require_docker_daemon() -> None:
    """Fail early (before any build) when the Docker engine is not running."""

    require_tool("docker")
    result = subprocess.run(
        ["docker", "info", "--format", "{{.ServerVersion}}"], capture_output=True, text=True
    )
    if result.returncode != 0 or not result.stdout.strip():
        detail = (result.stderr or "").strip().splitlines()
        raise CliError(
            "Docker is installed but its engine is not running. Start Docker Desktop "
            "(wait until it shows 'Engine running') and retry."
            + (f"\n{detail[0]}" if detail else "")
        )


def namespace_exists(namespace: str) -> bool:
    """Whether the namespace exists; fails clearly when the cluster is unreachable."""

    result = subprocess.run(
        ["kubectl", "get", "namespace", namespace], capture_output=True, text=True
    )
    if result.returncode == 0:
        return True
    if "NotFound" in (result.stderr or ""):
        return False
    raise CliError(
        "Cannot reach the Kubernetes cluster with the current kubectl context "
        f"(check `kubectl config current-context`):\n{(result.stderr or '').strip()}"
    )


def secret_exists(namespace: str, name: str) -> bool:
    result = subprocess.run(
        ["kubectl", "-n", namespace, "get", "secret", name], capture_output=True, text=True
    )
    return result.returncode == 0


def build_images(config: dict[str, str], images: dict[str, dict[str, Any]], force: bool) -> None:
    """Build and push the repo images that are not already in the registry."""

    to_build = [image for image in images.values() if image["build"]]
    if not to_build:
        return
    require_docker_daemon()
    if config["REGISTRY_USERNAME"] and config["REGISTRY_PASSWORD"]:
        run(
            ["docker", "login", normalize_registry(config["REGISTRY_SERVER"]),
             "-u", config["REGISTRY_USERNAME"], "--password-stdin"],
            input_text=config["REGISTRY_PASSWORD"],
            capture=True,
        )
    insecure = is_true(config["KANIKO_SKIP_TLS_VERIFY"])
    for image in to_build:
        if not force and image_exists(image["ref"], insecure):
            print(f"Image already in registry, skipping build: {image['ref']}")
            continue
        run(["docker", "build", "-t", image["ref"], str(image["context"])])
        run(["docker", "push", image["ref"]])


# --------------------------------------------------------------------------- commands


def cmd_build(args: argparse.Namespace, config: dict[str, str]) -> None:
    images = resolve_images(config)
    if not args.print_only:
        build_images(config, images, force=args.force_build)
    for key, image in images.items():
        print(f"{IMAGES[key]['override']}={image['ref']}")


def cmd_up(args: argparse.Namespace, config: dict[str, str]) -> None:
    namespace = k8s_name(config, "K8S_NAMESPACE")
    images = resolve_images(config)
    values = gateway_values(config, images)
    gateway_manifest = render("gateway.yaml", values)
    secret_manifest = registry_secret_manifest(config, redact=args.dry_run)

    print(f"{'Namespace:':<18}{namespace}")
    for key, image in images.items():
        origin = "built from repo" if image["build"] else "from .env override"
        print(f"{key + ' image:':<18}{image['ref']}  ({origin})")
    print(f"{'Registry secret:':<18}{config['MODEL_REGISTRY_SECRET']} "
          f"({'managed by bentloy' if secret_manifest else 'not managed: no REGISTRY_USERNAME/PASSWORD'})")
    if not config["MLFLOW_TRACKING_URI"]:
        print("Warning: MLFLOW_TRACKING_URI is empty; model deploys will need tracking_uri/model_uri.")

    if args.dry_run:
        print("\n# --- rendered manifests (dry run, nothing applied) ---")
        if secret_manifest:
            print(secret_manifest.rstrip() + "\n---")
        print(gateway_manifest)
        return

    # Check the cluster side first, so a problem there does not surface after a long build.
    require_tool("kubectl")
    ns_exists = namespace_exists(namespace)
    secret_name = config["MODEL_REGISTRY_SECRET"]
    if not secret_manifest and not (ns_exists and secret_exists(namespace, secret_name)):
        raise CliError(
            f"Registry secret '{secret_name}' does not exist in namespace '{namespace}' and "
            "REGISTRY_USERNAME/REGISTRY_PASSWORD are not set in .env, so the cluster could not "
            "pull the images. Set them in .env (bentloy creates the secret) or create the "
            "secret yourself:\n"
            f"  kubectl create namespace {namespace}\n"
            f"  kubectl -n {namespace} create secret docker-registry {secret_name} "
            f"--docker-server={normalize_registry(config['REGISTRY_SERVER'])} "
            "--docker-username=<user> --docker-password=<password>"
        )

    if not args.no_build:
        build_images(config, images, force=args.force_build)

    if not ns_exists:
        run(["kubectl", "create", "namespace", namespace])
    if secret_manifest:
        kubectl_apply(secret_manifest)
    kubectl_apply(gateway_manifest)

    if args.no_wait:
        return
    run(["kubectl", "-n", namespace, "rollout", "status", "deploy/bentloy-gateway", "--timeout=300s"])
    external = subprocess.run(
        ["kubectl", "-n", namespace, "get", "svc", "bentloy-gateway", "-o",
         "jsonpath={.status.loadBalancer.ingress[0].ip}{.status.loadBalancer.ingress[0].hostname}"],
        capture_output=True, text=True,
    ).stdout.strip()
    print("\nGateway ready.")
    if external:
        print(f"URL: http://{external}:{values['gateway_port']}  (set GATEWAY_URL to skip port-forwarding)")
    else:
        print("No external address yet; bentloy commands will port-forward svc/bentloy-gateway automatically.")


def cmd_deploy(args: argparse.Namespace, config: dict[str, str]) -> None:
    payload: dict[str, Any] = {
        "model_name": args.model_name,
        "model_version": args.version,
        "replicas": args.replicas,
        "deployed_name": args.name or "",
        "append_date": args.append_date,
    }
    if args.model_uri:
        payload["model_uri"] = args.model_uri
    if args.run_id:
        payload["run_id"] = args.run_id
    if args.experiment_name:
        payload["experiment_name"] = args.experiment_name

    with gateway_connection(config, args.gateway_url) as url:
        result = call_gateway(url, "deploy_model", payload)
        if not isinstance(result, dict) or "error" in result:
            raise CliError(f"Deploy rejected: {result}")
        operation_id = result["operation_id"]
        print(f"Operation:     {operation_id}")
        print(f"Deployed name: {result['deployed_name']}")
        print(f"Image:         {result['image_ref']}")
        if args.no_wait:
            print(f"Follow it with: python bentloy.py status {operation_id}")
            return

        timeout = (positive_int(config, "DEPLOY_BUILD_TIMEOUT_SEC")
                   + positive_int(config, "DEPLOY_ROLLOUT_TIMEOUT_SEC") + 120)
        deadline = time.monotonic() + timeout
        last = None
        while True:
            status = call_gateway(url, "deploy_status", {"operation_id": operation_id}, retries=3)
            current = (status.get("status"), status.get("step"))
            if current != last:
                print(f"  {time.strftime('%H:%M:%S')}  {current[0]} ({current[1]})", flush=True)
                last = current
            if status.get("status") in TERMINAL_STATUSES:
                break
            if time.monotonic() > deadline:
                raise CliError(f"Gave up waiting after {timeout}s; check `python bentloy.py status {operation_id}`.")
            time.sleep(5)

    if status["status"] == "failed":
        raise CliError(f"Deploy failed: {status.get('error') or status.get('message')}")
    print(f"\nReady. Call /predict with model_name=\"{status['deployed_name']}\".")


def cmd_status(args: argparse.Namespace, config: dict[str, str]) -> None:
    with gateway_connection(config, args.gateway_url) as url:
        print(json.dumps(call_gateway(url, "deploy_status", {"operation_id": args.operation_id}), indent=2))


def cmd_models(args: argparse.Namespace, config: dict[str, str]) -> None:
    payload = {
        "page": args.page,
        "page_size": args.page_size,
        "query": args.query,
        "experiment_name": args.experiment_name,
        "run_id": args.run_id,
    }
    with gateway_connection(config, args.gateway_url) as url:
        result = call_gateway(url, "list_models", payload)
    if args.json:
        print(json.dumps(result, indent=2))
        return
    endpoints = result.get("endpoints") or {}
    if not endpoints:
        print("No models found.")
    for name, endpoint in endpoints.items():
        print(f"{name:<40} {endpoint}")
    meta = (result.get("pagination") or {}).get("endpoints") or {}
    if meta.get("total_pages", 0) > 1:
        print(f"\nPage {meta['current_page']}/{meta['total_pages']}.")
    if (result.get("discovery") or {}).get("status") == "error":
        print(f"\nWarning: Kubernetes discovery failed: {result['discovery']['error']}")


def parse_predict_input(values: list[str], data_file: str) -> dict[str, Any]:
    """Build the /predict `data` from key=value pairs, a JSON argument or a JSON file.

    A JSON list is sent as {"features": [...]}; a JSON object or key=value pairs are
    sent as named columns.
    """

    if data_file:
        if values:
            raise CliError("Use either --file or inline input, not both.")
        try:
            raw: Any = json.loads(Path(data_file).read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as exc:
            raise CliError(f"Could not read JSON from {data_file}: {exc}") from exc
    elif len(values) == 1 and values[0].lstrip()[:1] in "[{":
        try:
            raw = json.loads(values[0])
        except ValueError as exc:
            raise CliError(f"Invalid JSON input: {exc}") from exc
    elif values:
        raw = {}
        for item in values:
            key, sep, value = item.partition("=")
            if not sep or not key.strip():
                raise CliError(f"Expected key=value, got '{item}'.")
            try:
                raw[key.strip()] = json.loads(value)
            except ValueError:
                raw[key.strip()] = value
    else:
        raise CliError("Give the input as key=value pairs, a JSON object/list or --file.")

    if isinstance(raw, list):
        return {"features": raw}
    if isinstance(raw, dict):
        return raw
    raise CliError("The input must be a JSON object (columns) or a list (features).")


def cmd_predict(args: argparse.Namespace, config: dict[str, str]) -> None:
    data = parse_predict_input(args.input, args.file)
    with gateway_connection(config, args.gateway_url) as url:
        result = call_gateway(url, "predict", {"model_name": args.name, "data": data})
    print(json.dumps(result, indent=2))
    if isinstance(result, dict) and result.get("error"):
        raise CliError(f"Prediction failed: {result['error']}")


def cmd_delete(args: argparse.Namespace, config: dict[str, str]) -> None:
    prefix = config["MODEL_SERVICE_PREFIX"]
    name = args.name.strip()
    service_name = name if name.startswith(f"{prefix}-") else f"{prefix}-{to_model_slug(name)}"
    with gateway_connection(config, args.gateway_url) as url:
        call_gateway(url, "delete_model", {"service_name": service_name})
    print(f"Delete requested for {service_name} (Deployment, Service and registry image).")


# --------------------------------------------------------------------------- entry point


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bentloy", description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--env-file", default=None, help="Path to the .env file (default: .env next to bentloy.py, optional)."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    up = sub.add_parser("up", help="Build/push images if needed and deploy the gateway.")
    up.add_argument("--dry-run", action="store_true", help="Print the plan and manifests; change nothing.")
    up.add_argument("--force-build", action="store_true", help="Rebuild images even if they exist.")
    up.add_argument("--no-build", action="store_true", help="Do not build; assume images exist.")
    up.add_argument("--no-wait", action="store_true", help="Do not wait for the gateway rollout.")
    up.set_defaults(func=cmd_up)

    build = sub.add_parser("build", help="Only build/push the images and print their references.")
    build.add_argument("--force-build", action="store_true", help="Rebuild images even if they exist.")
    build.add_argument("--print-only", action="store_true", help="Print the image references; build nothing.")
    build.set_defaults(func=cmd_build)

    def gateway_parser(name: str, help_text: str) -> argparse.ArgumentParser:
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--gateway-url", default="", help="Gateway URL (default: GATEWAY_URL or port-forward).")
        return p

    deploy = gateway_parser("deploy", "Build and deploy an MLflow model through the gateway.")
    deploy.add_argument("model_name", help="Registered MLflow model name.")
    deploy.add_argument("--version", default="latest", help="Model version (default: latest).")
    deploy.add_argument("--name", default="", help="Deployed name used in /predict (default: model name).")
    deploy.add_argument("--replicas", type=int, default=1)
    deploy.add_argument("--append-date", action="store_true", help="Append _YYYYMMDD to the deployed name.")
    deploy.add_argument("--model-uri", default="", help="Explicit MLflow model URI (e.g. runs:/<id>/model).")
    deploy.add_argument("--run-id", default="", help="MLflow run id of the version to deploy.")
    deploy.add_argument("--experiment-name", default="",
                        help="MLflow experiment the run belongs to (used to filter by model).")
    deploy.add_argument("--no-wait", action="store_true", help="Return right after queuing the deploy.")
    deploy.set_defaults(func=cmd_deploy)

    status = gateway_parser("status", "Show a deploy operation status.")
    status.add_argument("operation_id")
    status.set_defaults(func=cmd_status)

    models = gateway_parser("models", "List models known by the gateway.")
    models.add_argument("--query", default="", help="Substring of the deployed name.")
    models.add_argument("--experiment-name", default="", help="Only this MLflow experiment (exact).")
    models.add_argument("--run-id", default="", help="Only this MLflow run (exact).")
    models.add_argument("--page", type=int, default=1, help="Page number, starting at 1.")
    models.add_argument("--page-size", type=int, default=50)
    models.add_argument("--json", action="store_true", help="Print the full gateway response.")
    models.set_defaults(func=cmd_models)

    predict = gateway_parser("predict", "Get a prediction from a deployed model.")
    predict.add_argument("name", help="Deployed name (as shown by `models`).")
    predict.add_argument(
        "input", nargs="*",
        help="key=value pairs (x1=1 x2=2), a JSON object of columns or a JSON list of features.",
    )
    predict.add_argument("--file", default="", help="JSON file with the input instead of inline values.")
    predict.set_defaults(func=cmd_predict)

    delete = gateway_parser("delete", "Delete a deployed model (name or service name).")
    delete.add_argument("name", help="Deployed name (e.g. my_model) or service name (bentloy-model-my-model).")
    delete.set_defaults(func=cmd_delete)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.env_file is not None:
            env_file = Path(args.env_file)
            if not env_file.is_file():
                raise CliError(f"Env file not found: {env_file}")
        else:
            env_file = ROOT / ".env"
        args.func(args, load_config(env_file))
    except CliError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
