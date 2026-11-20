# Kubernetes manifests

`k8s/templates/` holds the manifests that `python bentloy.py up` renders and applies.
They are templates, not static manifests: do not `kubectl apply` them directly.

| Template | Resources |
|---|---|
| `templates/gateway.yaml` | ServiceAccount, Role, RoleBinding, ConfigMap (`bentloy-gateway-config`), Deployment and Service `bentloy-gateway` |
| `templates/registry-secret.yaml` | `kubernetes.io/dockerconfigjson` Secret for the registry (only when `REGISTRY_USERNAME/PASSWORD` are set) |

Model Deployments, Services and Kaniko build Jobs are **not** here: the gateway creates
them at runtime from `gateway/app/manifests/templates/` when `/deploy_model` is called.

Preview what would be applied:

```bash
python bentloy.py up --dry-run
```

## Values (and future Helm mapping)

Placeholders use Python `string.Template` syntax. Each one comes from a `.env` variable
and is meant to become one Helm value, so the chart port is a find/replace of
placeholders in these two files plus a `values.yaml` built from this table.

| Placeholder | `.env` variable | Default | Suggested Helm value |
|---|---|---|---|
| `namespace` | `K8S_NAMESPACE` | `default` | `.Release.Namespace` |
| `gateway_image` | `GATEWAY_IMAGE` | built: `<registry>[/<namespace>]/model-gateway:<hash>` | `gateway.image` |
| `gateway_replicas` | `GATEWAY_REPLICAS` | `1` | `gateway.replicas` |
| `gateway_port` | `GATEWAY_PORT` | `8000` | `gateway.service.port` |
| `gateway_service_type` | `GATEWAY_SERVICE_TYPE` | `LoadBalancer` | `gateway.service.type` |
| `registry_secret` | `MODEL_REGISTRY_SECRET` | `bentloy-registry-creds` | `registry.secretName` |
| `dockerconfigjson` | `REGISTRY_SERVER` + `REGISTRY_USERNAME` + `REGISTRY_PASSWORD` | — | `registry.server/username/password` (or an existing secret) |
| `registry_server` | `REGISTRY_SERVER` | — | `registry.server` |
| `registry_namespace` | `REGISTRY_NAMESPACE` | — (optional; empty = no namespace) | `registry.namespace` |
| `model_base_image` | `MODEL_BASE_IMAGE` | built: `<registry>[/<namespace>]/model-service-base:py312-<hash>` | `models.baseImage` |
| `mlflow_tracking_uri` | `MLFLOW_TRACKING_URI` | — | `mlflow.trackingUri` |
| `model_service_prefix` | `MODEL_SERVICE_PREFIX` | `bentloy-model` | `models.servicePrefix` |
| `model_url_template` | `MODEL_URL_TEMPLATE` | `http://{service_name}:3000` | `models.urlTemplate` |
| `kaniko_image` | `KANIKO_IMAGE` | `gcr.io/kaniko-project/executor:v1.23.2-debug` | `kaniko.image` |
| `kaniko_skip_tls_verify` | `KANIKO_SKIP_TLS_VERIFY` | `0` | `kaniko.skipTlsVerify` |
| `kaniko_cache` | `KANIKO_CACHE` | `1` (cache in `<registry>[/<namespace>]/kaniko-cache`) | `kaniko.cache` |
| `model_pip_extra_index_url` | `MODEL_PIP_EXTRA_INDEX_URL` | `https://download.pytorch.org/whl/cpu` | `models.pipExtraIndexUrl` |
| `auto_deploy_enabled` | `AUTO_DEPLOY_ENABLED` | `1` | `deploy.autoDeployEnabled` |
| `deploy_build_timeout_sec` | `DEPLOY_BUILD_TIMEOUT_SEC` | `1800` | `deploy.buildTimeoutSec` |
| `deploy_rollout_timeout_sec` | `DEPLOY_ROLLOUT_TIMEOUT_SEC` | `300` | `deploy.rolloutTimeoutSec` |
| `config_checksum` | (computed) | hash of the ConfigMap data | `{{ include (print $.Template.BasePath "/configmap.yaml") . \| sha256sum }}` |

Notes for the Helm port:

1. String values are inserted already quoted by `bentloy.py`; in Helm use `{{ .Values.x | quote }}`.
2. Image building stays outside Helm: `python bentloy.py build` builds/pushes only what
   changed and prints `GATEWAY_IMAGE=...` / `MODEL_BASE_IMAGE=...`, which become the
   chart's image values (`--print-only` just computes them).
3. The `checksum/config` pod annotation is the standard Helm trick to restart the
   gateway when its ConfigMap changes; keep it.
