"""Gateway log endpoints: model pod logs and build Job logs, read from Kubernetes."""

from typing import Any, Callable

import httpx

from ..deploy.errors import truncate
from ..deploy.operation_store import DeployOperationStore
from ..kubernetes.client import KubernetesApiClient, KubernetesApiError

MIN_TAIL_LINES = 1
MAX_TAIL_LINES = 5000
MODEL_CONTAINER = "model-service"
# Build containers in execution order: (name, is_init_container).
BUILD_CONTAINERS = (("prepare-build-context", True), ("kaniko", False))
ERROR_TEXT_CHARS = 200


class LogsRequestError(Exception):
    """A log request that cannot be served; carries the HTTP status to answer with."""

    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def _default_client_factory(verify: str | bool) -> httpx.AsyncClient:
    """Build the default async HTTP client used for Kubernetes log reads."""

    return httpx.AsyncClient(timeout=10.0, verify=verify)


def _validate_tail_lines(tail_lines: Any) -> None:
    """Reject a `tail_lines` outside [1, 5000] (booleans and non-integers included)."""

    if (
        isinstance(tail_lines, bool)
        or not isinstance(tail_lines, int)
        or not MIN_TAIL_LINES <= tail_lines <= MAX_TAIL_LINES
    ):
        raise LogsRequestError(
            f"tail_lines must be between {MIN_TAIL_LINES} and {MAX_TAIL_LINES}."
        )


def _waiting_reason(statuses: list[dict[str, Any]], container: str | None = None) -> str | None:
    """Return the first `state.waiting.reason` among container statuses (optionally by name)."""

    for status in statuses:
        if container is not None and status.get("name") != container:
            continue
        reason = (status.get("state") or {}).get("waiting", {}).get("reason")
        if reason:
            return reason
    return None


def _kubernetes_error(exc: Exception) -> LogsRequestError:
    """Map a Kubernetes/transport failure to a 502 answer."""

    status = getattr(exc, "status_code", 0) or ""
    text = truncate(str(exc), ERROR_TEXT_CHARS)
    detail = f"{status} {text}".strip()
    return LogsRequestError(f"Kubernetes API error while reading logs: {detail}", 502)


class LogsHandler:
    """Model pod and build Job log request handling."""

    def __init__(
        self,
        resolve_k8s_auth: Callable[[], tuple[str, dict[str, str], str | bool]],
        k8s_client: KubernetesApiClient,
        operation_store: DeployOperationStore,
        model_namespace: str,
        model_service_prefix: str,
        http_client_factory: Callable[[str | bool], httpx.AsyncClient] | None = None,
    ):
        self.resolve_k8s_auth = resolve_k8s_auth
        self.k8s_client = k8s_client
        self.operation_store = operation_store
        self.model_namespace = model_namespace
        self.model_service_prefix = model_service_prefix
        self.http_client_factory = http_client_factory or _default_client_factory

    async def deployment_logs(
        self, service_name: str, tail_lines: int = 500, previous: bool = False
    ) -> dict[str, Any]:
        """Return the latest logs of the newest pod of a model Deployment."""

        _validate_tail_lines(tail_lines)
        prefix = f"{self.model_service_prefix}-"
        slug = service_name[len(prefix):] if service_name.startswith(prefix) else ""
        if not slug:
            raise LogsRequestError(f"Model service '{service_name}' not found.", 404)

        result: dict[str, Any] = {
            "service_name": service_name,
            "pod_name": None,
            "pod_phase": None,
            "container_ready": False,
            "restart_count": 0,
            "available": False,
            "reason": None,
            "logs": "",
        }
        namespace = self.model_namespace
        try:
            base_url, headers, verify = self.resolve_k8s_auth()
            async with self.http_client_factory(verify) as client:
                deployment = await self.k8s_client.get_resource(
                    client,
                    headers,
                    f"{base_url}/apis/apps/v1/namespaces/{namespace}/deployments/{service_name}",
                )
                if deployment is None:
                    raise LogsRequestError(f"Model service '{service_name}' not found.", 404)
                if deployment.get("spec", {}).get("replicas") == 0:
                    result["reason"] = "The deployment is scaled to 0 replicas."
                    return result

                pods = await self.k8s_client.list_pods(
                    client,
                    base_url,
                    headers,
                    namespace,
                    f"app=model-service,model-slug={slug}",
                )
                pods = [p for p in pods if not p.get("metadata", {}).get("deletionTimestamp")]
                if not pods:
                    result["reason"] = "No running pod for this model yet."
                    return result

                pod = pods[0]
                pod_name = pod.get("metadata", {}).get("name")
                statuses = pod.get("status", {}).get("containerStatuses") or []
                model_status = next(
                    (s for s in statuses if s.get("name") == MODEL_CONTAINER), {}
                )
                result["pod_name"] = pod_name
                result["pod_phase"] = pod.get("status", {}).get("phase")
                result["container_ready"] = bool(model_status.get("ready", False))
                result["restart_count"] = int(model_status.get("restartCount", 0))

                log = await self.k8s_client.read_pod_log(
                    client,
                    base_url,
                    headers,
                    namespace,
                    pod_name,
                    MODEL_CONTAINER,
                    tail_lines,
                    previous=previous,
                )
        except LogsRequestError:
            raise
        except (KubernetesApiError, httpx.HTTPError, RuntimeError) as exc:
            raise _kubernetes_error(exc) from exc

        if log.status_code == 200:
            result.update(available=True, logs=log.text)
        elif log.status_code == 400:
            if previous:
                result["reason"] = (
                    "No previous container: the container has not restarted."
                    if result["restart_count"] == 0
                    else "The previous container's logs are not available."
                )
            else:
                reason = "The model pod is starting."
                waiting = _waiting_reason(statuses)
                result["reason"] = f"{reason} ({waiting})" if waiting else reason
        elif log.status_code == 404:
            result["reason"] = "The model pod no longer exists."
        else:
            raise _kubernetes_error(
                KubernetesApiError(f"Read pod log failed: {log.text}", log.status_code)
            )
        return result

    async def build_logs(self, operation_id: str, tail_lines: int = 500) -> dict[str, Any]:
        """Return the logs of both containers of an operation's build Job pod."""

        _validate_tail_lines(tail_lines)
        operation = await self.operation_store.get(operation_id)
        if operation is None:
            raise LogsRequestError(f"Operation '{operation_id}' not found.", 404)

        step = operation.get("step")
        job_name = operation.get("k8s_job") or None
        result: dict[str, Any] = {
            "operation_id": operation_id,
            "job_name": job_name,
            "pod_name": None,
            "pod_phase": None,
            "step": step,
            "available": False,
            "reason": None,
            "logs": "",
        }
        if not job_name or step == "queued":
            result["reason"] = "The build job has not been created yet."
            return result

        namespace = operation.get("namespace") or self.model_namespace
        try:
            base_url, headers, verify = self.resolve_k8s_auth()
            async with self.http_client_factory(verify) as client:
                job = await self.k8s_client.get_resource(
                    client,
                    headers,
                    f"{base_url}/apis/batch/v1/namespaces/{namespace}/jobs/{job_name}",
                )
                if job is None:
                    if step in ("queued", "create_job"):
                        # The name is stored at queue time: the Job is about to be created.
                        result["reason"] = "The build job has not been created yet."
                    else:
                        result["reason"] = (
                            "The build job no longer exists (it is removed 1 hour after "
                            "finishing); its logs are not kept."
                        )
                    return result

                pods = await self.k8s_client.list_pods(
                    client, base_url, headers, namespace, f"job-name={job_name}"
                )
                if not pods:
                    result["reason"] = "The build pod has not been created yet."
                    return result

                pod = pods[0]
                pod_name = pod.get("metadata", {}).get("name")
                pod_status = pod.get("status", {})
                result["pod_name"] = pod_name
                result["pod_phase"] = pod_status.get("phase")

                sections: list[str] = []
                any_logs = False
                for container, is_init in BUILD_CONTAINERS:
                    statuses = pod_status.get(
                        "initContainerStatuses" if is_init else "containerStatuses"
                    ) or []
                    log = await self.k8s_client.read_pod_log(
                        client, base_url, headers, namespace, pod_name, container, tail_lines
                    )
                    if log.status_code == 200:
                        any_logs = True
                        body = log.text if log.text.endswith("\n") else log.text + "\n"
                    elif log.status_code == 400:
                        waiting = _waiting_reason(statuses, container)
                        body = f"(not started yet: {waiting})\n" if waiting else "(not started yet)\n"
                    elif log.status_code == 404:
                        body = "(logs not available)\n"
                    else:
                        raise KubernetesApiError(
                            f"Read pod log failed: {log.text}", log.status_code
                        )
                    sections.append(f"===== {container} =====\n{body}")
                if not any_logs:
                    all_statuses = [
                        *(pod_status.get("initContainerStatuses") or []),
                        *(pod_status.get("containerStatuses") or []),
                    ]
                    waiting = _waiting_reason(all_statuses)
                    reason = "The build pod is starting."
                    result["reason"] = f"{reason} ({waiting})" if waiting else reason
                    return result
        except LogsRequestError:
            raise
        except (KubernetesApiError, httpx.HTTPError, RuntimeError) as exc:
            raise _kubernetes_error(exc) from exc

        result.update(available=True, logs="\n".join(sections))
        return result
