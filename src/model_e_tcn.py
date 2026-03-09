"""
Modelo E — Temporal Convolutional Network (TCN).

TCN con Conv1D causales dilatadas y conexiones residuales.
Sin dependencias recurrentes → paralelizable y sin vanishing gradient.

Funciones exportadas
--------------------
build_tcn(window, n_features, filters, kernel_size, dilations, dropout)
"""
import keras
from keras import layers, regularizers


def build_tcn(
    window: int,
    n_features: int,
    filters: int = 64,
    kernel_size: int = 3,
    dilations: tuple = (1, 2, 4, 8),
    dropout: float = 0.2,
) -> keras.Model:
    """
    TCN con Conv1D causales dilatadas y conexiones residuales.

    Bloque TCN (uno por cada tasa en `dilations`):
        residual = x
        x → Conv1D(causal, dil=d) → LayerNorm → ReLU → Dropout
          → Conv1D(causal, dil=d) → LayerNorm → ReLU → Dropout
          → Add(residual)

    Campo receptivo con kernel=3, dilations=(1,2,4,8):
        RF = 2 × kernel_size × sum(dilations) - 1 = 2×3×15 - 1 = 89 pasos
        Con paso de 30 min → ~44.5 h de contexto efectivo.

    Args:
        window     : longitud de la ventana temporal (pasos).
        n_features : número de features de entrada.
        filters    : número de filtros en cada Conv1D.
        kernel_size: tamaño del kernel en cada Conv1D.
        dilations  : tupla de tasas de dilatación (un bloque por tasa).
        dropout    : tasa de dropout aplicada tras cada activación.

    Returns:
        Modelo Keras compilado con Adam(lr=1e-3) y MSE.
    """
    inp = keras.Input(shape=(window, n_features), name='tcn_input')

    # Proyección inicial: n_features → filters para que el residual tenga
    # la misma dimensión desde el primer bloque.
    x = layers.Conv1D(filters, 1, padding='same', name='tcn_proj')(inp)

    for d in dilations:
        residual = x

        # Primera Conv causal dilatada
        x = layers.Conv1D(
            filters, kernel_size, padding='causal', dilation_rate=d,
            kernel_regularizer=regularizers.l2(1e-4),
            name=f'tcn_conv1_d{d}')(x)
        x = layers.LayerNormalization(name=f'tcn_ln1_d{d}')(x)
        x = layers.Activation('relu', name=f'tcn_act1_d{d}')(x)
        x = layers.Dropout(dropout, name=f'tcn_drop1_d{d}')(x)

        # Segunda Conv causal dilatada
        x = layers.Conv1D(
            filters, kernel_size, padding='causal', dilation_rate=d,
            kernel_regularizer=regularizers.l2(1e-4),
            name=f'tcn_conv2_d{d}')(x)
        x = layers.LayerNormalization(name=f'tcn_ln2_d{d}')(x)
        x = layers.Activation('relu', name=f'tcn_act2_d{d}')(x)
        x = layers.Dropout(dropout, name=f'tcn_drop2_d{d}')(x)

        # Conexión residual (misma dimensión: no se necesita proyección adicional)
        x = layers.Add(name=f'tcn_res_d{d}')([x, residual])

    x = layers.GlobalAveragePooling1D(name='tcn_gap')(x)
    x = layers.Dense(32, activation='relu',
                     kernel_regularizer=regularizers.l2(1e-4),
                     name='tcn_dense')(x)
    x = layers.Dropout(dropout, name='tcn_head_drop')(x)
    out = layers.Dense(1, name='tcn_out')(x)

    model = keras.Model(inp, out, name='E_TCN')
    model.compile(optimizer=keras.optimizers.Adam(1e-3),
                  loss='mse', metrics=['mae'])
    return model
