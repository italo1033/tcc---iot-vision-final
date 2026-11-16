#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

if [[ ! -f .env ]]; then
  echo "Erro: arquivo .env nao encontrado. Copie .env.example para .env e ajuste as variaveis."
  exit 1
fi

set -a
source .env
set +a

pio run -e twatch-s3 -t upload --upload-port "${ESP32_USB_PORT}"
