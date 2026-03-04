# 🧪 NITRATE_model

> **Nowcasting de nitratos en agua** mediante redes neuronales LSTM a partir de sensores multiparámetro en tiempo real.

---

## 🎯 Objetivo

Construir un modelo de **Nowcasting** capaz de **estimar la concentración de nitratos** (NO₃⁻ en mg/L) en la estación de **Yeguas** usando únicamente las lecturas simultáneas de los **demás sensores fisicoquímicos** (turbidez, conductividad, pH, temperatura, oxígeno disuelto…).

La idea es que, ante una medición puntual de todos los parámetros, el modelo sea capaz de inferir el nivel de nitrato **sin necesidad del sensor de nitratos**, aprovechando las correlaciones entre variables.

```
Sensores (X)  ──►  [ LSTM Nowcasting ]  ──►  NO₃⁻ estimado (ŷ)
temperatura
conductividad
turbidez
pH / OD / ...
```

---

## 🔬 Arquitectura del modelo

| Componente | Detalle |
|---|---|
| Tipo de modelo | LSTM (Long Short-Term Memory) |
| Ventana temporal | 24 h → 48 pasos de 30 min |
| Variable objetivo | `NITRATE_nitrate_no3_mg_l` |
| Escalado features | `StandardScaler` |
| Escalado target | `MinMaxScaler` |
| Regularización | Dropout 20 % |
| Split | 80 % train / 20 % test (cronológico) |

---

## 📋 Registro de trabajo

### ✅ Hecho

- [x] **Exploración inicial de datos** — Carga y análisis del CSV raw de la estación Yeguas (`2026-03-04`).
- [x] **Pipeline de limpieza** (`csv_cleaner.py`) — Eliminación de picos con ventana móvil (±3σ), interpolación lineal, corte de datos tras el 17-02-2026.
- [x] **Primer modelo LSTM** (`model_trainning.py`) — Ventanas de 48 pasos, escalado dual X/Y, arquitectura LSTM → Dropout → Dense → Dense(1).

### 🔄 En progreso

- [ ] Evaluación de métricas del modelo (R², MAE, RMSE).
- [ ] Visualización de predicciones vs valores reales.

### 🗓️ Pendiente

- [ ] Análisis de importancia de variables (permutation importance / SHAP).
- [ ] Optimización de hiperparámetros (nº capas, window size, unidades LSTM).
- [ ] Validación cruzada temporal (walk-forward validation).
- [ ] Exportación del modelo entrenado (`.keras` / ONNX).
- [ ] Documentar dependencias en `requirements.txt`.
