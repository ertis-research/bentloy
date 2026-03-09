import pandas as pd
import numpy as np
import tensorflow as tf
import mlflow
import mlflow.tensorflow
from sklearn.preprocessing import StandardScaler, MinMaxScaler
from sklearn.metrics import r2_score, mean_absolute_error
from tensorflow.keras.models import Sequential
from tensorflow.keras.layers import LSTM, Dense, Dropout

# ── MLflow: experimento de producción ─────────────────────────────────────────
mlflow.set_tracking_uri("mlruns")
mlflow.set_experiment("nitrate_nowcasting_production")

# TF autolog registra arquitectura, curvas de loss y el modelo automáticamente
mlflow.tensorflow.autolog(log_models=True, log_datasets=False, silent=True)

# 1. Cargar y limpiar datos
df = pd.read_csv("YEGUAS_data.csv")
df['_time'] = pd.to_datetime(df['_time'])
df = df.set_index('_time').sort_index()

# EXIGENCIA DE NEGOCIO: Eliminar todas las columnas de NITRATE excepto el Target actual (Y)
cols_to_drop = [col for col in df.columns if 'NITRATE' in col and col != 'NITRATE_nitrate_no3_mg_l']
df = df.drop(columns=cols_to_drop)

# Limpiar Nulos
df = df.dropna(subset=['NITRATE_nitrate_no3_mg_l'])
df = df.interpolate(method='linear').dropna()

# 2. SEPARAR "Y" DE "X" (Nitrato NUNCA entra en la X)
y_series = df['NITRATE_nitrate_no3_mg_l'].values
X_df = df.drop(columns=['NITRATE_nitrate_no3_mg_l'])

# 3. ESCALADO (Vital para que TensorFlow converja)
X_scaler = StandardScaler()
X_scaled = X_scaler.fit_transform(X_df)

y_scaler = MinMaxScaler() # Escalamos Y también para facilitar el aprendizaje
y_scaled = y_scaler.fit_transform(y_series.reshape(-1, 1)).flatten()

# 4. CREAR VENTANAS DE 24 HORAS (48 pasos de 30 min)
window_size = 48
X_seq, y_seq = [], []

# Recorremos los datos agrupándolos en bloques de 48 mediciones
for i in range(window_size - 1, len(X_scaled)):
    window = X_scaled[i - window_size + 1 : i + 1] # 24h de historia de los OTROS sensores
    X_seq.append(window)
    y_seq.append(y_scaled[i]) # Nitrato actual

X_seq = np.array(X_seq)
y_seq = np.array(y_seq)

# 5. DIVIDIR DATOS (80% Entrenamiento / 20% Prueba cronológico)
split_idx = int(len(X_seq) * 0.8)
X_train, X_test = X_seq[:split_idx], X_seq[split_idx:]
y_train, y_test = y_seq[:split_idx], y_seq[split_idx:]

# 6. CREAR EL MODELO TENSORFLOW (LSTM)
tf.random.set_seed(42)

model = Sequential([
    # LSTM procesa la ventana de tiempo: [Batch, 48 pasos temporales, N variables]
    LSTM(64, activation='tanh', input_shape=(X_train.shape[1], X_train.shape[2])),
    Dropout(0.2), # Apaga el 20% de neuronas para evitar sobreajuste (Overfitting)
    Dense(32, activation='relu'),
    Dense(1) # Salida: 1 solo número (El nitrato actual estimado)
])

# Compilador con Adam optimizer (muy estándar en DL)
optimizer = tf.keras.optimizers.Adam(learning_rate=0.001)
model.compile(optimizer=optimizer, loss='mse', metrics=['mae'])

# ─────────────────────────────────────────────────────────────────────────────
with mlflow.start_run(run_name="LSTM_baseline") as run:
    # 7. Registrar hiperparámetros manualmente (complementa autolog)
    mlflow.log_params({
        "model_type":        "LSTM",
        "window_size":       window_size,
        "lstm_units":        64,
        "dropout":           0.2,
        "dense_units":       32,
        "epochs":            100,
        "batch_size":        32,
        "learning_rate":     0.001,
        "early_stop_patience": 15,
        "train_size":        split_idx,
        "test_size":         len(X_seq) - split_idx,
        "n_features":        X_scaled.shape[1],
    })

    # 8. ENTRENAMIENTO (con parada temprana para no quemar ciclos si ya no mejora)
    early_stop = tf.keras.callbacks.EarlyStopping(monitor='val_loss', patience=15, restore_best_weights=True)
    print("Entrenando Red Neuronal TensorFlow...")
    history = model.fit(
        X_train, y_train,
        epochs=100,
        batch_size=32,
        validation_split=0.15,
        callbacks=[early_stop],
        verbose=1
    )

    # 9. PREDECIR Y EVALUAR
    y_pred_scaled = model.predict(X_test).flatten()
    y_pred_real = y_scaler.inverse_transform(y_pred_scaled.reshape(-1, 1)).flatten()
    y_test_real = y_scaler.inverse_transform(y_test.reshape(-1, 1)).flatten()

    test_mae = mean_absolute_error(y_test_real, y_pred_real)
    test_r2  = r2_score(y_test_real, y_pred_real)

    # Registrar métricas finales de producción
    mlflow.log_metrics({
        "test_mae": float(test_mae),
        "test_r2":  float(test_r2),
    })

    print(f"Error MAE Final en Producción: {test_mae:.2f} mg/L")
    print(f"R²: {test_r2:.4f}")
    print(f"MLflow Run ID: {run.info.run_id}")
    print("Para visualizar: ejecuta `mlflow ui` en la terminal")