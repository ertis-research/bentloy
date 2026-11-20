# Runbook: MLflow Models on Kubernetes

Updated: 2026-11-19

This runbook describes the operational flow implemented in this repository. Every
step goes through `bentloy.py` (Python 3.12, standard library only).

Technical reference:

- `docs/gateway_technical.md`
- `k8s/README.md` (templates and values)

## 1) Prerequisites

| Component | Required | Notes |
|---|---|---|
| Python 3.12 | Yes | Runs `bentloy.py` |
| Kubernetes access (`kubectl`) | Yes | Correct context and RBAC permissions |
| Docker | Yes | Builds and pushes the gateway and model base images |
| Registry credentials | Yes | `REGISTRY_USERNAME`, `REGISTRY_PASSWORD` |
| MLflow Tracking URI | Yes | Reachable from the pods |

## 2) Configuration (`.env`)

Copy `.env_example` to `.env`. Required:

```env
MLFLOW_TRACKING_URI=http://mlflow-host:5000
REGISTRY_SERVER=registry.example.com
REGISTRY_NAMESPACE=my-project
REGISTRY_USERNAME=YOUR_REGISTRY_USER
REGISTRY_PASSWORD=YOUR_REGISTRY_TOKEN_OR_PASSWORD
K8S_NAMESPACE=default
```

Everything else is optional and documented in `.env_example`. Notes:

1. `REGISTRY_SERVER` must be only the registry host.
2. Registry credentials are also written to the cluster Secret, so on shared clusters
   prefer a robot/service account. If you leave them empty, the Secret
   (`MODEL_REGISTRY_SECRET`) must already exist in the namespace, or `up` stops
   before building anything.
3. Set `KANIKO_SKIP_TLS_VERIFY=1` only when the registry uses a self-signed certificate.
4. Do not set `GATEWAY_IMAGE` / `MODEL_BASE_IMAGE` unless you want to pin existing
   images: by default they are built from the repository.
5. Environment variables override `.env`; `--env-file` selects another file
   (e.g. one per cluster).

## 3) Deploy or update the gateway

```bash
python bentloy.py up --dry-run   # optional: review plan and manifests
python bentloy.py up
```

`up` is idempotent and runs, in order:

1. Computes the image tags from a hash of the files copied into each image:
   - `<REGISTRY_SERVER>[/<REGISTRY_NAMESPACE>]/model-gateway:<hash>`
   - `<REGISTRY_SERVER>[/<REGISTRY_NAMESPACE>]/model-service-base:py312-<hash>`
2. Checks first that the cluster is reachable and that the registry Secret can be
   created (credentials in `.env`) or already exists, so a problem there does not
   show up after a long build.
3. `docker login` with the `.env` credentials, then builds and pushes each image
   only if that tag is not in the registry yet. Fails with a clear message if the
   Docker engine is not running.
4. Creates the namespace if needed and applies the registry Secret
   (`MODEL_REGISTRY_SECRET`) when `REGISTRY_USERNAME/PASSWORD` are set.
5. Renders `k8s/templates/gateway.yaml` (ServiceAccount, RBAC, ConfigMap,
   Deployment, Service) and applies it.
6. Waits for the rollout and prints the external URL when the LoadBalancer has one.

Options: `--force-build` (rebuild even if the tags exist), `--no-build` (only apply),
`--no-wait`.

To only build and push the images (e.g. in CI) and get their references:

```bash
python bentloy.py build            # or --print-only to just compute the tags
```

## 4) Deploy a model

```bash
python bentloy.py deploy tiny_demo_model
python bentloy.py deploy tiny_demo_model --version 3 --name tiny_demo_prod --append-date
```

The command calls `POST /deploy_model` on the gateway and follows
`POST /deploy_status` until the operation ends. The gateway:

1. Runs a Kaniko Job that downloads the model from MLflow and builds
   `<REGISTRY_SERVER>[/<REGISTRY_NAMESPACE>]/model-<slug>:<tag>` on top of the model base image,
   installing the model's own requirements.
2. Creates the `bentloy-model-<deployed-name>` Deployment and Service and waits for the rollout.

Options:

| Option | Meaning |
|---|---|
| `--version` | MLflow model version (default `latest`: Production stage, else highest version) |
| `--name` | Deployed name; it is the `model_name` used in `/predict` |
| `--append-date` | Appends `_YYYYMMDD` to the deployed name |
| `--replicas` | Model replicas |
| `--model-uri` / `--run-id` | Explicit MLflow source instead of the registry |
| `--experiment-name` | MLflow experiment of the run; lets `list_models` filter deployments by model |
| `--no-wait` | Return after queuing; follow with `python bentloy.py status <operation_id>` |

Terminal statuses: `ready`, `failed` (intermediate: `queued`, `building`,
`pushing`, `deploying`).

### Other model operations

```bash
python bentloy.py models [--query demo] [--experiment-name <exp>] [--run-id <id>] [--json]
python bentloy.py predict tiny_demo_prod x1=5 x2=2 x3=3
python bentloy.py status <operation_id>
python bentloy.py delete tiny_demo_prod     # or the service name: bentloy-model-tiny-demo-prod
```

`predict` input forms: `column=value` pairs, a JSON object of columns, a JSON list
of features, or `--file input.json` (see 5.2 for the formats).

`delete` removes the Deployment, the Service and the model image from the registry.

### Reaching the gateway

The model commands use `GATEWAY_URL` when set; otherwise they open a temporary
`kubectl port-forward` to `svc/bentloy-gateway`. `--gateway-url` overrides both.

## 5) API calls

`<gateway_url>` is the LoadBalancer URL, or `http://localhost:8000` after
`kubectl -n <K8S_NAMESPACE> port-forward svc/bentloy-gateway 8000:8000`.

### 5.1 List models

```bash
curl -X POST "<gateway_url>/list_models" -H "Content-Type: application/json" \
  -d '{"page": 0, "page_size": 10, "query": ""}'
```

Add `"experiment_name": "<experiment>"` and/or `"run_id": "<run_id>"` to get only the
deployments and operations of one model or one version (exact match).

### 5.2 Predict

```bash
curl -X POST "<gateway_url>/predict" \
  -H "Content-Type: application/json" \
  -d '{"model_name": "tiny_demo_model", "data": {"x1": 1.0, "x2": 2.0, "x3": 3.0}}'
```

`data` formats:

| Format | Example | Use |
|---|---|---|
| Named columns | `{"x1": 1.0, "x2": 2.0}` | Tabular models (Ludwig, scikit-learn with a signature...). Recommended. |
| `features` list | `{"features": [1.0, 2.0]}` | For models with named inputs (MLflow signature or Ludwig schema) the values are mapped to the columns in order; `[[...], [...]]` sends several rows. For tensor models: 1D = one sample, 2D = one temporal sample, 3D = batch. |

The response is `{"model": ..., "predictions": [...]}`; Ludwig models return one
`<output>_predictions` field per output feature.

### 5.3 Deploy by endpoint

```bash
curl -X POST "<gateway_url>/deploy_model" -H "Content-Type: application/json" \
  -d '{"model_name": "tiny_demo_model", "model_version": "latest", "deployed_name": "tiny_demo_prod"}'
# One version (run) of an experiment:
curl -X POST "<gateway_url>/deploy_model" -H "Content-Type: application/json" \
  -d '{"model_name": "churn_v2", "run_id": "<run_id>", "model_uri": "runs:/<run_id>/ludwig-model", "experiment_name": "<experiment>"}'
curl -X POST "<gateway_url>/deploy_status" -H "Content-Type: application/json" \
  -d '{"operation_id": "<operation_id>"}'
```

## 6) What to rerun and when

| Change | Command |
|---|---|
| Gateway or model_service code/dependencies | `python bentloy.py up` (rebuilds only what changed) |
| `.env` values (MLflow, registry, timeouts...) | `python bentloy.py up` (restarts the gateway if its config changed) |
| Registry credentials | `python bentloy.py up` (re-applies the Secret) |
| New model or model version | `python bentloy.py deploy <model>` |
| Cluster/namespace recreated | `python bentloy.py up` |

Note: models already deployed keep the base image they were built with; redeploy
them to pick up a new model base image.

### Model build speed

The first build of a model with heavy dependencies (e.g. Ludwig -> PyTorch) takes
several minutes. Two settings keep it down (both on by default):

1. `MODEL_PIP_EXTRA_INDEX_URL` (default `https://download.pytorch.org/whl/cpu`):
   PyTorch is installed as CPU wheels, without the CUDA libraries (several GB less to
   build, push and pull). Set it empty on GPU clusters.
2. `KANIKO_CACHE=1`: the dependency layer is cached in `<REGISTRY_SERVER>[/<REGISTRY_NAMESPACE>]/kaniko-cache`,
   so later models/versions with the same requirements skip the `pip install`.
   The registry account needs push permission on that repository (same project).

## 7) Troubleshooting

### 7.1 `models` is empty

1. Check the model service name prefix (`model-...`).
2. Check `K8S_NAMESPACE` (the gateway only looks in its own namespace).
3. Check the gateway RBAC can list services/pods: `kubectl -n <ns> get role bentloy-gateway -o yaml`.

### 7.2 `Model not available` in `/predict`

1. Confirm the model Deployment is `Ready`.
2. Confirm the Service exists and answers `/predict`.
3. Check the routing target in `python bentloy.py models --json`.

### 7.3 Registry / TLS errors

1. Self-signed registry: set `KANIKO_SKIP_TLS_VERIFY=1` (also used for
   `docker manifest inspect --insecure` when checking existing images), and make
   sure the Docker daemon trusts the registry or lists it in `insecure-registries`.
2. Check the Secret exists: `kubectl -n <ns> get secret <MODEL_REGISTRY_SECRET>`.
3. Verify push permissions for `<REGISTRY_SERVER>[/<REGISTRY_NAMESPACE>]`.

### 7.4 A model image fails to build

Read the Kaniko Job logs (`python bentloy.py status <operation_id>` shows the error).
If a library is missing at runtime, make sure the model was logged to MLflow with its
requirements: the base image only contains `mlflow-skinny`, numpy, pandas and pyarrow.
