import pandas as pd
import numpy as np

# 1. Cargar los datos
file_path = "RAW DATA - TABLE - YEGUAS-data-2026-03-04 11_30_42.csv"
df = pd.read_csv(file_path)

# 2. Asegurarnos de que el tiempo es formato datetime y ordenar
df['_time'] = pd.to_datetime(df['_time'])
df = df.set_index('_time')
df.sort_index(inplace=True)

# Eliminar datos posteriores al 16-02-2026
df = df[df.index <= '2026-02-17']

# Eliminar columnas de NITRATE excepto NITRATE_nitrate_no3_mg_l
nitrate_cols_to_drop = [col for col in df.columns if 'NITRATE' in col and col != 'NITRATE_nitrate_no3_mg_l']
df.drop(columns=nitrate_cols_to_drop, inplace=True)

# 3. Función para limpiar picos excesivos
def remove_peaks(series, window=15, n_std=3):
    # Calcular la media y la desviación estándar en una ventana móvil
    rolling_mean = series.rolling(window=window, center=True, min_periods=1).mean()
    rolling_std = series.rolling(window=window, center=True, min_periods=1).std()
    
    # Identificar picos (valores fuera del rango media ± n_std * desviación)
    is_outlier = np.abs(series - rolling_mean) > (n_std * rolling_std)
    
    # Reemplazar los picos por NaN y luego rellenarlos usando interpolación
    cleaned = series.copy()
    cleaned[is_outlier] = np.nan
    cleaned = cleaned.interpolate(method='linear')
    return cleaned

# 4. Aplicar la función a todas las columnas numéricas del dataframe
cleaned_df = pd.DataFrame(index=df.index)
for col in df.columns:
    if pd.api.types.is_numeric_dtype(df[col]):
        cleaned_df[col] = remove_peaks(df[col], window=15, n_std=3)
    else:
        cleaned_df[col] = df[col]

# 5. Guardar los datos limpios
cleaned_df.reset_index(inplace=True)
cleaned_df.to_csv('YEGUAS_data.csv', index=False)