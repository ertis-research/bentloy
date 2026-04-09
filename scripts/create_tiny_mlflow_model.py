from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict, dataclass

import mlflow
import mlflow.sklearn
import numpy as np
from mlflow.tracking import MlflowClient
from sklearn.linear_model import LinearRegression


@dataclass
class TinyModelResult:
    tracking_uri: str
    experiment_name: str
    run_id: str
    model_uri: str
    registered_model_name: str | None
    registered_version: str | None
    stage: str | None


def build_tiny_regression() -> LinearRegression:
    # Dataset pequeno y deterministico para demo.
    # y = 2*x1 + 0.5*x2 - x3 + 3
    x = np.array(
        [
            [0.0, 0.0, 0.0],
            [1.0, 2.0, 1.0],
            [2.0, 0.5, 3.0],
            [3.0, 1.5, 0.5],
            [4.0, 0.0, 2.0],
            [5.0, 3.0, 1.0],
        ],
        dtype=np.float32,
    )
    y = 2.0 * x[:, 0] + 0.5 * x[:, 1] - 1.0 * x[:, 2] + 3.0

    model = LinearRegression()
    model.fit(x, y)
    return model


def transition_stage(
    client: MlflowClient,
    model_name: str,
    version: str,
    stage: str,
) -> None:
    # API legacy pero compatible con MLflow Registry actual.
    client.transition_model_version_stage(
        name=model_name,
        version=version,
        stage=stage,
        archive_existing_versions=True,
    )


def create_and_register_tiny_model(
    tracking_uri: str,
    experiment_name: str,
    run_name: str,
    artifact_path: str,
    registered_model_name: str | None,
    stage: str | None,
) -> TinyModelResult:
    mlflow.set_tracking_uri(tracking_uri)
    mlflow.set_experiment(experiment_name)
    client = MlflowClient()

    model = build_tiny_regression()

    with mlflow.start_run(run_name=run_name) as run:
        mlflow.log_params(
            {
                "model_type": "LinearRegression",
                "n_features": 3,
                "dataset_size": 6,
            }
        )
        mlflow.sklearn.log_model(sk_model=model, artifact_path=artifact_path)

        run_id = run.info.run_id
        model_uri = f"runs:/{run_id}/{artifact_path}"

    registered_version: str | None = None
    effective_stage: str | None = None

    if registered_model_name:
        created = mlflow.register_model(model_uri=model_uri, name=registered_model_name)
        registered_version = str(created.version)

        if stage:
            transition_stage(
                client=client,
                model_name=registered_model_name,
                version=registered_version,
                stage=stage,
            )
            effective_stage = stage

    return TinyModelResult(
        tracking_uri=tracking_uri,
        experiment_name=experiment_name,
        run_id=run_id,
        model_uri=model_uri,
        registered_model_name=registered_model_name,
        registered_version=registered_version,
        stage=effective_stage,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Crea un modelo tiny de regresion, lo guarda en MLflow y opcionalmente lo "
            "registra en MLflow Model Registry."
        )
    )
    parser.add_argument(
        "--tracking-uri",
        default=os.getenv("MLFLOW_TRACKING_URI", ""),
        help="URI de MLflow Tracking. Si no se pasa, usa MLFLOW_TRACKING_URI.",
    )
    parser.add_argument(
        "--experiment-name",
        default="tiny-model-demo",
        help="Nombre del experimento MLflow.",
    )
    parser.add_argument(
        "--run-name",
        default="tiny-linear-regression",
        help="Nombre del run MLflow.",
    )
    parser.add_argument(
        "--artifact-path",
        default="model",
        help="Ruta de artefacto dentro del run (normalmente 'model').",
    )
    parser.add_argument(
        "--registered-model-name",
        default="tiny_demo_model",
        help="Nombre de modelo en Registry. Usa '' para no registrar.",
    )
    parser.add_argument(
        "--stage",
        default="Production",
        help="Stage al que mover la version registrada. Usa '' para no mover stage.",
    )
    return parser.parse_args()


def main() -> None:
    # Evita errores de encoding en terminales Windows cp1252 cuando MLflow imprime emojis.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    args = parse_args()

    if not args.tracking_uri:
        raise SystemExit(
            "Falta --tracking-uri y no existe MLFLOW_TRACKING_URI en entorno."
        )

    registered_model_name = args.registered_model_name.strip() or None
    stage = args.stage.strip() or None

    result = create_and_register_tiny_model(
        tracking_uri=args.tracking_uri,
        experiment_name=args.experiment_name,
        run_name=args.run_name,
        artifact_path=args.artifact_path,
        registered_model_name=registered_model_name,
        stage=stage,
    )

    print(json.dumps(asdict(result), indent=2))

    print("\nSiguiente paso sugerido para deploy en k8s:")
    if result.registered_model_name:
        print(
            f"./k8s/deploy_model.sh {result.registered_model_name} latest api-models 1"
        )
    else:
        print(f"export MODEL_URI={result.model_uri}")
        print("./k8s/deploy_model.sh tiny_demo_model latest api-models 1")


if __name__ == "__main__":
    main()
