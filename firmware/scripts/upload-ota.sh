#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

if [[ ! -f .env ]]; then
  echo "Erro: arquivo .env nao encontrado. Copie .env.example para .env e ajuste as variaveis."
  exit 1
fi

set -a
# shellcheck disable=SC1091
source .env
set +a

if [[ -z "${ESP32_OTA_HOST:-}" ]]; then
  echo "Erro: ESP32_OTA_HOST nao foi definido no .env."
  exit 1
fi

if command -v ping >/dev/null 2>&1; then
  if ping -c 1 -W 2 "$ESP32_OTA_HOST" >/dev/null 2>&1; then
    echo "Dispositivo respondeu ao ping em $ESP32_OTA_HOST."
  else
    echo "Aviso: $ESP32_OTA_HOST nao respondeu ao ping. Continuando, pois algumas redes/dispositivos bloqueiam ICMP."
  fi
else
  echo "Aviso: comando ping indisponivel. Continuando sem teste de conectividade."
fi

PLATFORMIO_UPLOAD_FLAGS="--auth=${OTA_PASSWORD}" pio run -e twatch-s3-ota -t upload
