#!/bin/bash

set -euo pipefail

MODEL_NAME=$1
MODEL_VERSION=${2:-latest}
PORT=${3:-$(shuf -i 3001-3999 -n 1)}  # Puerto aleatorio si no se especifica

if [ -z "$MODEL_NAME" ]; then
    echo "❌ Uso: ./deploy_model.sh <nombre_modelo> [version] [puerto]"
    exit 1
fi

echo "🚀 Deployando: $MODEL_NAME:$MODEL_VERSION en puerto $PORT"

if [ ! -f docker-compose.yml ]; then
  echo "❌ Error: No existe docker-compose.yml en el directorio actual"
  exit 1
fi

if docker compose version >/dev/null 2>&1; then
  COMPOSE_CMD=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then
  COMPOSE_CMD=(docker-compose)
else
  echo "❌ Error: No se encontró 'docker compose' ni 'docker-compose'"
  exit 1
fi

# Verificar que el modelo existe en MLflow
echo "🔍 Verificando modelo en MLflow..."
curl -s "http://mlflow.example.com:5000/api/2.0/mlflow/registered-models/get?name=$MODEL_NAME" | grep -q "RESOURCE_DOES_NOT_EXIST" && {
    echo "❌ Error: Modelo '$MODEL_NAME' no encontrado en MLflow"
    exit 1
}

# Generar nombre de servicio válido para Docker
SERVICE_NAME=$(echo "$MODEL_NAME" | tr '_' '-' | tr '[:upper:]' '[:lower:]')

if ! grep -Eq '^services:[[:space:]]*$' docker-compose.yml; then
  echo "❌ Error: docker-compose.yml no contiene una sección 'services:' válida"
  exit 1
fi

if awk -v s="$SERVICE_NAME" '
BEGIN { in_services = 0 }
/^services:[[:space:]]*$/ { in_services = 1; next }
in_services && /^[[:alnum:]_.-]+:[[:space:]]*$/ { in_services = 0 }
in_services && $0 ~ "^  " s ":[[:space:]]*$" { found = 1; exit }
END { exit(found ? 0 : 1) }
' docker-compose.yml; then
  echo "⚠️ El servicio '$SERVICE_NAME' ya existe en docker-compose.yml. No se añadirá de nuevo."
else
  tmp_file=$(mktemp)
  awk \
    -v service_name="$SERVICE_NAME" \
    -v port="$PORT" \
    -v model_name="$MODEL_NAME" \
    -v model_version="$MODEL_VERSION" '
BEGIN {
  in_services = 0
  inserted = 0
}

function print_service_block() {
  print ""
  print "  " service_name ":"
  print "    build:"
  print "      context: ./model_service"
  print "    container_name: " service_name "-service"
  print "    ports:"
  print "      - \"" port ":3000\""
  print "    environment:"
  print "      - MLFLOW_TRACKING_URI=http://mlflow.example.com:5000"
  print "      - MODEL_NAME=" model_name
  print "      - MODEL_VERSION=" model_version
  print "    depends_on:"
  print "      mlflow:"
  print "        condition: service_healthy"
  print "    networks:"
  print "      - ml-network"
  print "    restart: unless-stopped"
}

{
  if ($0 ~ /^services:[[:space:]]*$/) {
    in_services = 1
    print
    next
  }

  if (in_services && !inserted && $0 ~ /^[[:alnum:]_.-]+:[[:space:]]*$/) {
    print_service_block()
    inserted = 1
    in_services = 0
  }

  print
}

END {
  if (in_services && !inserted) {
    print_service_block()
    inserted = 1
  }

  if (!inserted) {
    exit 2
  }
}
' docker-compose.yml > "$tmp_file"

  mv "$tmp_file" docker-compose.yml
  echo "✅ Servicio '$SERVICE_NAME' añadido en la sección services"
fi

# Actualizar gateway
UPPER_NAME=$(echo $MODEL_NAME | tr '[:lower:]' '[:upper:]' | tr '-' '_')

if grep -Eq "^[[:space:]]*-[[:space:]]*MODELO_${UPPER_NAME}_URL=" docker-compose.yml; then
  echo "ℹ️ Variable de entorno MODELO_${UPPER_NAME}_URL ya existe en gateway"
else
  tmp_file=$(mktemp)
  if awk -v env_var="MODELO_${UPPER_NAME}_URL=http://${SERVICE_NAME}:3000" '
BEGIN {
  in_gateway = 0
  inserted = 0
}

{
  print

  if ($0 ~ /^  gateway:[[:space:]]*$/) {
    in_gateway = 1
    next
  }

  if (in_gateway && $0 ~ /^    environment:[[:space:]]*$/ && !inserted) {
    print "      - " env_var
    inserted = 1
    in_gateway = 0
    next
  }

  if (in_gateway && $0 ~ /^  [[:alnum:]_.-]+:[[:space:]]*$/) {
    in_gateway = 0
  }
}

END {
  if (!inserted) {
    exit 2
  }
}
' docker-compose.yml > "$tmp_file"; then
    mv "$tmp_file" docker-compose.yml
    echo "✅ Gateway actualizado con MODELO_${UPPER_NAME}_URL"
  else
    rm -f "$tmp_file"
    echo "⚠️ No se pudo insertar MODELO_${UPPER_NAME}_URL en gateway (revisa sección gateway.environment)"
  fi
fi

if ! "${COMPOSE_CMD[@]}" -f docker-compose.yml config >/dev/null 2>&1; then
  echo "❌ Error: docker-compose.yml inválido tras la actualización"
  exit 1
fi

echo "✅ Configuración añadida"
echo "🚢 Desplegando servicio..."

"${COMPOSE_CMD[@]}" up -d ${SERVICE_NAME}
sleep 5
"${COMPOSE_CMD[@]}" restart gateway

echo "✅ Deploy completado!"
echo "📊 Health check:"
curl -s http://localhost:8000/health | jq ".models.${MODEL_NAME}"