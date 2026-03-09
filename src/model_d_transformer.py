"""
Modelo D — Transformer (Atención Temporal).

Estado: PAUSED — en espera de HPO para CNN+LSTM.

Funciones exportadas
--------------------
build_transformer(window, n_features, num_heads, key_dim, ff_dim, dropout)
"""
import keras
from keras import layers, regularizers


def build_transformer(
    window: int,
    n_features: int,
    num_heads: int = 4,
    key_dim: int = 16,
    ff_dim: int = 64,
    dropout: float = 0.2,
) -> keras.Model:
    """
    Transformer encoder (un solo bloque) para series temporales.

    Flujo:
        Input(window, n_features)
        → Dense(key_dim * num_heads)          # proyección lineal
        → MultiHeadAttention + residual + LN  # auto-atención temporal
        → FFN (Dense→Dropout→Dense) + residual + LN
        → GlobalAvgPool → Dense(32) → Dropout → Dense(1)
    """
    inp = keras.Input(shape=(window, n_features))

    # Proyección lineal al espacio de embedding
    x = layers.Dense(key_dim * num_heads)(inp)

    # Bloque de atención multi-cabeza
    attn_out = layers.MultiHeadAttention(
        num_heads=num_heads, key_dim=key_dim, dropout=dropout)(x, x)
    x = layers.Add()([x, attn_out])
    x = layers.LayerNormalization()(x)

    # Feed-Forward Network
    ff = layers.Dense(ff_dim, activation='relu')(x)
    ff = layers.Dropout(dropout)(ff)
    ff = layers.Dense(key_dim * num_heads)(ff)
    x  = layers.Add()([x, ff])
    x  = layers.LayerNormalization()(x)

    # Cabeza de regresión
    x = layers.GlobalAveragePooling1D()(x)
    x = layers.Dense(32, activation='relu',
                     kernel_regularizer=regularizers.l2(1e-4))(x)
    x = layers.Dropout(dropout)(x)
    out = layers.Dense(1)(x)

    model = keras.Model(inp, out, name='D_Transformer')
    model.compile(optimizer=keras.optimizers.Adam(5e-4), loss='mse', metrics=['mae'])
    return model
