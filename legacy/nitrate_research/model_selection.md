# 🔍 Investigación: Selección de Modelo para Nowcasting de Nitratos

> Fecha de análisis: 2026-03-04
> Dataset: `YEGUAS_data.csv` — Estación Yeguas

---

## 📊 Características del Dataset

| Propiedad | Valor |
|---|---|
| Filas | 3 119 |
| Features (X) | 13 sensores fisicoquímicos |
| Target (Y) | `NITRATE_nitrate_no3_mg_l` |
| Frecuencia de muestreo | 30 minutos |
| Rango temporal | 2025-12-10 → 2026-02-16 |
| Nulos tras limpieza | 0 |

### Variables disponibles (X)

| Grupo | Variables |
|---|---|
| Conductividad | `C4E_conductivity`, `C4E_salinity`, `C4E_tds`, `C4E_temperature` |
| Turbidez | `NTU_fnu`, `NTU_mgL`, `NTU_ntu`, `NTU_temperature` |
| Oxígeno disuelto | `OPTOD_mgL`, `OPTOD_ppm`, `OPTOD_saturation`, `OPTOD_temperature` |

### Correlaciones con NO₃⁻

| Variable | Correlación |
|---|---|
| `C4E_tds` | **-0.811** |
| `C4E_conductivity` | **-0.807** |
| `C4E_salinity` | **-0.807** |
| `OPTOD_mgL` | -0.348 |
| `OPTOD_ppm` | -0.348 |
| `OPTOD_saturation` | -0.347 |
| `NTU_mgL` | -0.104 |
| `NTU_fnu` / `NTU_ntu` | -0.089 |
| `C4E_temperature` | -0.054 |
| `NTU_temperature` | -0.052 |
| `OPTOD_temperature` | +0.036 |

> ⚠️ La correlación muy alta con conductividad/TDS indica una relación casi lineal dominante,
> pero hay varianza residual que justifica un modelo no lineal.

---

## 🎯 Definición del problema

**Nowcasting** = estimar $\hat{y}(t)$ usando $\mathbf{X}(t)$ (mismo instante).

$$\hat{y}(t) = f\bigl(X_1(t),\, X_2(t),\, \ldots,\, X_{13}(t)\bigr)$$

No es forecasting (no predecimos el futuro), por tanto la ventana temporal no es
estrictamente necesaria, aunque añadir **lags cortos** puede mejorar la estimación.

**Restricción**: el modelo **no puede** ser una regresión lineal
(Ridge, Lasso, Regresión Múltiple, etc.) — así lo exige el cliente.

---

## 🏆 Ranking de Modelos Evaluados

### 🥇 XGBoost / LightGBM ← Recomendado principal

- Maneja muy bien datasets pequeños (~3 k filas) sin overfitting severo.
- Captura la relación no lineal residual sobre la dominante conductividad/TDS.
- Lag features sencillas (1–4 pasos = 30 min–2 h) añaden contexto temporal sin
  necesitar arquitecturas complejas.
- Entrenamiento en segundos; fácil ajuste de hiperparámetros.
- **Interpretable con SHAP** → requisito habitual en calidad de agua.
- ❌ No es regresión lineal → cumple la restricción.

```python
# Ejemplo de lag features
for lag in [1, 2, 4]:
    df[f'conductivity_lag{lag}'] = df['C4E_conductivity'].shift(lag)
    df[f'tds_lag{lag}']          = df['C4E_tds'].shift(lag)
```

### 🥈 Temporal Fusion Transformer (TFT)

- Mecanismo de atención temporal → aprende qué instantes pasados importan.
- Más potente que LSTM para series no estacionarias.
- Riesgo de overfitting con sólo 3 k filas; requiere dropout agresivo y
  validación walk-forward estricta.
- Mayor coste computacional y complejidad de implementación.
- ❌ No es regresión lineal → cumple la restricción.

### 🥉 LSTM (modelo actual en `model_trainning.py`)

- Válido, pero la **ventana de 48 pasos (24 h) es excesiva** para Nowcasting
  donde la correlación dominante es contemporánea.
- Recomendación: reducir ventana a **4–12 pasos (2–6 h)**.
- Dataset pequeño limita la capacidad de generalización de redes recurrentes.
- ❌ No es regresión lineal → cumple la restricción.

---

## ❌ Modelos descartados

| Modelo | Razón de descarte |
|---|---|
| Ridge / Lasso / Regresión Múltiple | **Regresión lineal — excluida por requisito del cliente** |
| ARIMA / SARIMA | Solo univariante; no usa los 13 sensores |
| Prophet | Diseñado para forecasting con tendencia/estacionalidad; no nowcasting multisensor |
| MLP puro (sin contexto temporal) | Ignora completamente la estructura temporal |

---

## 📋 Plan de acción recomendado

```
1. XGBoost baseline    →  X(t) + lag 1-4 pasos, sin ventana temporal
2. Comparativa LSTM    →  ventana reducida a 6-12 pasos (3-6 h)
3. Métricas comunes    →  R², MAE, RMSE en split cronológico 80/20
4. Interpretabilidad   →  SHAP values sobre el modelo XGBoost
5. (Opcional)          →  TFT si se consiguen más datos históricos
```

---

## 🧠 Plan TensorFlow — Exploración Completa

> **Requisito del cliente**: el modelo final debe estar implementado en TensorFlow.
> Este plan sustituye la elección de XGBoost como producción y eleva TensorFlow
> a primera línea, manteniendo la restricción de no usar modelos lineales.

### Arquitecturas a comparar (de menor a mayor complejidad)

| # | Modelo | Ventana | Parámetros aprox. | Riesgo overfitting |
|---|--------|---------|-------------------|--------------------|
| A | **MLP Wide & Deep** | lag 1–4 pasos (sin secuencia) | ~5 k | 🟢 Bajo |
| B | **GRU** | 6–12 pasos (3–6 h) | ~20 k | 🟡 Medio |
| C | **1D-CNN + LSTM** | 12 pasos (6 h) | ~35 k | 🟡 Medio |
| D | **Transformer (Atención Temporal)** | 12–24 pasos | ~60 k | 🔴 Alto |

#### A · MLP Wide & Deep (baseline TF)

El componente **Wide** recibe las features del instante $t$ directamente
(explota la correlación lineal dominante de conductividad/TDS).
El componente **Deep** recibe lags de 1–4 pasos (30 min–2 h) para añadir
contexto temporal sin necesidad de arquitecturas recurrentes.

```
Wide input  ──────────────────────────────┐
                                          ├──► Concat ──► Dense(16) ──► Output
Deep input → Dense(64) → Dense(32) ──────┘
```

**Ventajas**: convergencia rápida, interpretable con Integrated Gradients,
mínimo riesgo de overfitting con 3 k filas.

#### B · GRU (Gated Recurrent Unit)

Alternativa al LSTM con **menos parámetros** y equivalente desempeño en
datasets pequeños. Ventana recomendada: **8 pasos (4 h)**.

```python
GRU(64, return_sequences=False) → Dropout(0.2) → Dense(32) → Dense(1)
```

Correcciones respecto al LSTM actual en `model_trainning.py`:
- Ventana reducida de 48 → **8 pasos**
- Añadir `kernel_regularizer=l2(1e-4)` para combatir overfitting
- `BatchNormalization` antes de la capa Dense final

#### C · 1D-CNN + LSTM

CNN extrae patrones locales (ciclos diurnos, picos de turbidez);
LSTM captura dependencias más largas.

```
Input(12, n_features)
  → Conv1D(32, kernel=3, relu) → MaxPool1D(2)
  → Conv1D(64, kernel=3, relu)
  → LSTM(32)
  → Dense(16, relu) → Dense(1)
```

**Por qué funciona bien aquí**: la turbidez presenta picos abruptos que
la CNN detecta como patrones locales antes de pasarlos al LSTM.

#### D · Transformer con Atención Temporal

Mecanismo `MultiHeadAttention` nativo de Keras (TF 2.x) que aprende
qué instantes de la ventana son más relevantes para el nitrato actual.

```
Input(12, n_features)
  → MultiHeadAttention(num_heads=4, key_dim=16)
  → LayerNorm + Add (residual)
  → Conv1D(64, 1) feed-forward
  → GlobalAveragePooling1D
  → Dense(32, relu) → Dense(1)
```

> ⚠️ Con 3 k filas existe riesgo de overfitting. Mitigar con:
> dropout=0.3, reducción de `key_dim` y walk-forward validation.

---

### 🔁 Protocolo de Validación

Se utilizará **Walk-Forward Validation** (también llamada expanding window)
en lugar del split fijo 80/20, más adecuada para series temporales cortas:

```
Fold 1: Train [0..1900]      Test [1900..2100]
Fold 2: Train [0..2100]      Test [2100..2300]
Fold 3: Train [0..2300]      Test [2300..2500]
Fold 4: Train [0..2500]      Test [2500..2700]
Fold 5: Train [0..2700]      Test [2700..fin]
```

**Métricas por fold**:

$$\text{MAE} = \frac{1}{n}\sum|y_i - \hat{y}_i|, \quad
  \text{RMSE} = \sqrt{\frac{1}{n}\sum(y_i - \hat{y}_i)^2}, \quad
  R^2 = 1 - \frac{\text{SS}_{res}}{\text{SS}_{tot}}$$

---

### 🔍 Interpretabilidad: Integrated Gradients (nativo TF)

`tf.GradientTape` permite calcular **Integrated Gradients** sin dependencias
externas. Para cada predicción se obtiene la contribución de cada feature:

$$\text{IG}_i(x) = (x_i - x'_i) \times \int_0^1
  \frac{\partial F(x' + \alpha(x - x'))}{\partial x_i}\, d\alpha$$

donde $x'$ es el *baseline* (ceros) y $x$ la observación.

```python
def integrated_gradients(model, baseline, inputs, steps=50):
    alphas = tf.linspace(0.0, 1.0, steps + 1)
    interpolated = baseline + alphas[:, None, None] * (inputs - baseline)
    with tf.GradientTape() as tape:
        tape.watch(interpolated)
        preds = model(interpolated)
    grads = tape.gradient(preds, interpolated)
    avg_grads = tf.reduce_mean(grads, axis=0)
    ig = (inputs - baseline) * avg_grads
    return ig
```

---

### 📁 Estructura del notebook de experimentación

```
tf_experiments.ipynb
├── 0 · Setup & Carga de datos
├── 1 · Preprocesado (escalado, lag features, ventanas)
├── 2 · Modelo A — MLP Wide & Deep
├── 3 · Modelo B — GRU
├── 4 · Modelo C — 1D-CNN + LSTM
├── 5 · Modelo D — Transformer
├── 6 · Walk-Forward Validation (todos los modelos)
├── 7 · Tabla comparativa de métricas
└── 8 · Integrated Gradients — mejor modelo
```

---

### ✅ Criterios de selección del modelo ganador

| Prioridad | Criterio |
|-----------|----------|
| 1 | **MAE walk-forward** < 1.5 mg/L en los últimos 3 folds |
| 2 | **R² walk-forward** > 0.70 |
| 3 | Mínima diferencia train-val loss (señal de no overfitting) |
| 4 | Tiempo de inferencia < 10 ms por muestra |
| 5 | Interpretabilidad vía Integrated Gradients |

---

## 📋 Plan de acción recomendado (actualizado — TensorFlow)

```
Paso 1  Notebook tf_experiments.ipynb
        → Implementar los 4 modelos (A-D)
        → Walk-forward validation común
        → Tabla comparativa automática

Paso 2  Ajuste de hiperparámetros del modelo ganador
        → Keras Tuner (RandomSearch) sobre ventana, unidades, dropout

Paso 3  Integrated Gradients sobre el modelo ganador
        → Identificar las features más influyentes por rango horario

Paso 4  Reescribir model_trainning.py
        → Solo el modelo ganador, código de producción limpio
        → Exportar como SavedModel para despliegue
```

---

## 📝 Notas adicionales

- La alta colinealidad entre `C4E_conductivity`, `C4E_salinity` y `C4E_tds`
  (miden básicamente lo mismo) puede causar inestabilidad. Considerar quedarse
  sólo con `C4E_conductivity` o aplicar PCA.
- Las temperaturas de los tres sensores también están correlacionadas entre sí.
- Walk-forward validation es preferible al split fijo para series temporales cortas.
