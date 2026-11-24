"""Tests for the thin Kubernetes API client."""

import httpx

from gateway.app.kubernetes.client import KubernetesApiClient
from tests.gateway.fakes import NS, FakeCluster, make_pod

JOB = "bentloy-build-churn-v2-abc123"


async def test_get_job_pod_name_and_read_job_logs_unchanged(k8s_cluster: FakeCluster) -> None:
    """The newest Job pod is found, its logs are read, and a listing error yields no pod."""
    k8s_cluster.pods = [
        make_pod("a", "2026-01-01T00:00:00Z"),
        make_pod("b", "2026-01-02T00:00:00Z"),
    ]
    k8s_cluster.logs["kaniko"] = (200, "text")
    client = KubernetesApiClient()

    async with httpx.AsyncClient(transport=httpx.MockTransport(k8s_cluster.handler)) as http:
        name = await client.get_job_pod_name(http, "https://k8s", {}, NS, JOB)
        logs = await client.read_job_logs(http, "https://k8s", {}, NS, JOB, "kaniko")
        k8s_cluster.pods_status = 500
        failed = await client.get_job_pod_name(http, "https://k8s", {}, NS, JOB)

    assert name == "b"
    assert logs == "text"
    assert failed is None
