import os
from typing import Mapping


def resolve_k8s_auth(
    env: Mapping[str, str] | None = None,
    token_path: str = "/var/run/secrets/kubernetes.io/serviceaccount/token",
    ca_path: str = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt",
) -> tuple[str, dict[str, str], str | bool]:
    source = env if env is not None else os.environ
    kube_host = source.get("KUBERNETES_SERVICE_HOST")
    kube_port = source.get("KUBERNETES_SERVICE_PORT_HTTPS", "443")

    if not kube_host:
        raise RuntimeError("KUBERNETES_SERVICE_HOST is not available.")
    if not os.path.exists(token_path):
        raise RuntimeError("ServiceAccount token is not available.")

    with open(token_path, "r", encoding="utf-8") as token_file:
        token = token_file.read().strip()
    if not token:
        raise RuntimeError("ServiceAccount token is empty.")

    verify: str | bool = ca_path if os.path.exists(ca_path) else False
    base_url = f"https://{kube_host}:{kube_port}"
    headers = {"Authorization": f"Bearer {token}"}
    return base_url, headers, verify
