"""Kubernetes label/annotation keys that link model-service resources to MLflow."""

# Labels (valid label values only; usable with `kubectl -l`).
RUN_ID_LABEL = "bentloy.io/run-id"
EXPERIMENT_LABEL = "bentloy.io/experiment"

# Annotations (free text, exact values).
MODEL_URI_ANNOTATION = "bentloy.io/model-uri"
EXPERIMENT_NAME_ANNOTATION = "bentloy.io/experiment-name"
# Operation that last applied a model-service Deployment/Service; delete_operation only
# removes resources it owns.
OPERATION_ID_ANNOTATION = "bentloy.io/operation-id"
