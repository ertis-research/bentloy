import bentoml
import mlflow
import mlflow.pyfunc
import os
import pandas as pd
from typing import Any

@bentoml.service(
    resources={"cpu": "2"},
    traffic={"timeout": 30}
)
class MLflowModelService:
    """
    Servicio genérico para cualquier modelo de MLflow.
    Se configura mediante variables de entorno.
    """
    
    def __init__(self):
        # Configuración desde variables de entorno
        mlflow_uri = os.getenv("MLFLOW_TRACKING_URI", "http://localhost:5000")
        model_name = os.getenv("MODEL_NAME")
        model_version = os.getenv("MODEL_VERSION", "latest")
        
        if not model_name:
            raise ValueError("MODEL_NAME no está configurado")
        
        mlflow.set_tracking_uri(mlflow_uri)
        
        try:
            # Cargar modelo desde MLflow
            if model_version == "latest":
                model_uri = f"models:/{model_name}/Production"
            else:
                model_uri = f"models:/{model_name}/{model_version}"
            
            print(f"[{model_name}] Cargando desde: {model_uri}")
            self.model = mlflow.pyfunc.load_model(model_uri)
            self.model_name = model_name
            print(f"[{model_name}] ✓ Modelo cargado")
            
        except Exception as e:
            print(f"[{model_name}] ✗ Error: {e}")
            self.model = None
            self.model_name = model_name
    
    @bentoml.api
    def predict(self, data: dict[str, Any]) -> dict[str, Any]:
        """
        Endpoint de predicción genérico.
        
        Input: 
          {"features": [1.0, 2.0, 3.0]} 
          o {"col1": 1.0, "col2": 2.0}
        
        Output: 
          {"model": "modelo_a", "predictions": [...]}
        """
        if self.model is None:
            return {"error": "Modelo no disponible"}
        
        try:
            # Convertir a DataFrame según el formato
            if "features" in data:
                df = pd.DataFrame([data["features"]])
            else:
                df = pd.DataFrame([data])
            
            predictions = self.model.predict(df)
            
            # Convertir a lista si es necesario
            if hasattr(predictions, 'tolist'):
                predictions = predictions.tolist()
            elif not isinstance(predictions, list):
                predictions = list(predictions)
            
            return {
                "model": self.model_name,
                "predictions": predictions
            }
            
        except Exception as e:
            return {"error": str(e), "model": self.model_name}
    
    @bentoml.api
    def health(self) -> dict[str, Any]:
        """Health check"""
        return {
            "status": "healthy" if self.model else "unhealthy",
            "model": self.model_name,
            "model_loaded": self.model is not None
        }