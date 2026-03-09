"""
Modelo C — 1D-CNN + LSTM (baseline) y su variante HPO para Optuna.

Funciones exportadas
--------------------
build_cnn_lstm(window, n_features)
    Arquitectura fija de referencia.

build_cnn_lstm_hpo(window, n_features, conv_filters, kernel_size,
                   lstm_units, dropout, lr)
    Variante totalmente parametrizable para búsqueda Bayesiana (Optuna/TPE).
"""
import keras
from keras import layers, regularizers


def build_cnn_lstm(window: int, n_features: int) -> keras.Model:
    """
    CNN causal + LSTM + Self-Attention.

    Arquitectura fija (baseline pre-HPO):
        Conv1D(32, k=6) → Conv1D(64, k=6) → MaxPool → Dropout
        → LSTM(64, return_seq) → Dropout
        → MultiHeadAttention(heads=2, key_dim=32) + residual + LayerNorm
        → GlobalAvgPool → Dense(32) → Dropout → Dense(1)

    Campo receptivo CNN con kernel=6: 6 pasos × 30 min = 3 h de patrones locales.
    """
    inp = keras.Input(shape=(window, n_features))

    # CNN: detecta patrones locales dentro de la ventana
    x = layers.Conv1D(32, kernel_size=6, activation='relu', padding='causal')(inp)
    x = layers.Conv1D(64, kernel_size=6, activation='relu', padding='causal')(x)
    x = layers.MaxPooling1D(pool_size=2)(x)
    x = layers.Dropout(0.1)(x)

    # LSTM con return_sequences para alimentar la capa de atención
    x = layers.LSTM(64, return_sequences=True,
                    kernel_regularizer=regularizers.l2(1e-4))(x)
    x = layers.Dropout(0.2)(x)

    # Self-Attention: ponderar qué pasos temporales son más relevantes
    attn_out = layers.MultiHeadAttention(num_heads=2, key_dim=32, dropout=0.1)(x, x)
    x = layers.Add()([x, attn_out])
    x = layers.LayerNormalization()(x)

    x = layers.GlobalAveragePooling1D()(x)
    x = layers.Dense(32, activation='relu',
                     kernel_regularizer=regularizers.l2(1e-4))(x)
    x = layers.Dropout(0.2)(x)
    out = layers.Dense(1)(x)

    model = keras.Model(inp, out, name='C_CNN_LSTM')
    model.compile(optimizer=keras.optimizers.Adam(1e-3), loss='mse', metrics=['mae'])
    return model


def build_cnn_lstm_hpo(
    window: int,
    n_features: int,
    conv_filters: int,
    kernel_size: int,
    lstm_units: int,
    dropout: float,
    lr: float,
) -> keras.Model:
    """
    CNN + LSTM + Self-Attention totalmente parametrizable (superset de build_cnn_lstm).

    key_dim=16 fijo: compatible con lstm_units ∈ {32, 64, 128} y num_heads=2.
    Diseñado para ser usado como función objetivo en un estudio de Optuna.
    """
    inp = keras.Input(shape=(window, n_features))

    x = layers.Conv1D(conv_filters, kernel_size=kernel_size,
                      activation='relu', padding='causal')(inp)
    x = layers.Conv1D(conv_filters * 2, kernel_size=kernel_size,
                      activation='relu', padding='causal')(x)
    x = layers.MaxPooling1D(pool_size=2)(x)
    x = layers.Dropout(dropout * 0.5)(x)

    x = layers.LSTM(lstm_units, return_sequences=True,
                    kernel_regularizer=regularizers.l2(1e-4))(x)
    x = layers.Dropout(dropout)(x)

    attn = layers.MultiHeadAttention(num_heads=2, key_dim=16,
                                     dropout=dropout * 0.5)(x, x)
    x = layers.Add()([x, attn])
    x = layers.LayerNormalization()(x)

    x = layers.GlobalAveragePooling1D()(x)
    x = layers.Dense(32, activation='relu',
                     kernel_regularizer=regularizers.l2(1e-4))(x)
    x = layers.Dropout(dropout)(x)
    out = layers.Dense(1)(x)

    m = keras.Model(inp, out, name='C_CNN_LSTM_HPO')
    m.compile(optimizer=keras.optimizers.Adam(lr), loss='mse', metrics=['mae'])
    return m
