import os
import sys
from zenml import pipeline
from zenml.integrations.mlflow.steps.mlflow_deployer import mlflow_model_registry_deployer_step
from mlflow.tracking.client import MlflowClient

# 1. Le decimos a tu ordenador dónde vive MLflow
os.environ["MLFLOW_TRACKING_URI"] = "http://mlflow.example.com:5000"

client = MlflowClient()
model_name = "nitrate_nowcasting_best_01"
try:
    # Obtener dinámicamente la última versión del modelo
    latest_versions = client.get_registered_model(model_name).latest_versions
    latest_version_str = max([v.version for v in latest_versions])
except Exception as e:
    print(f"Error accediendo a MLflow: {e}")
    latest_version_str = "1"

@pipeline
def deploy_nitrate_model_pipeline():
    """Pipeline de ZenML que descarga y despliega el modelo usando MLflow model registry."""
    # 2. Despliega la versión exacta del modelo alojado en MLflow
    mlflow_model_registry_deployer_step(
        registry_model_name=model_name,
        registry_model_version=latest_version_str,
        model_name="nitrate_gru_model",
        replace_existing=True,
    )

if __name__ == "__main__":
    print(f"📥 Conectando con MLflow (desplegando versión {latest_version_str}) y configurando despliegue con ZenML...")

    try:
        # 3. Construir y ejecutar el pipeline (reemplaza 'bentoml build' y 'push')
        deploy_nitrate_model_pipeline()
        print("\n✅ ¡Éxito! El modelo ha sido descargado y registrado para su despliegue con ZenML.")
        
        print("\n☁️ El servicio MLflow ahora es gestionado por ZenML.")
        print("👉 Para ver el estado del servidor de inferencia ejecuta:")
        print("uv run zenml model-deployer models list")
    
    except Exception as e:
        print(f"\n❌ Error al desplegar el modelo. Mensaje de error:\n{e}")
        sys.exit(1)
