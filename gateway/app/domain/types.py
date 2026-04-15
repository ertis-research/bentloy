from typing import TypedDict


class DeployOperationRequest(TypedDict):
    namespace: str
    model_name: str
    model_slug: str
    model_version: str
    model_uri: str
    tracking_uri: str
    replicas: int
    target_image: str
    base_image: str
    registry_secret: str
    insecure_registry_tls: bool
    registry_server: str
    job_name: str


class DeployOperationRecord(TypedDict):
    operation_id: str
    status: str
    step: str
    created_at: str
    updated_at: str
    model_name: str
    model_version: str
    namespace: str
    replicas: int
    image_ref: str
    k8s_job: str
    error: str | None
    message: str | None
    request: DeployOperationRequest
