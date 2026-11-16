#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

if [[ ! -f .env ]]; then
  echo "Erro: arquivo .env nao encontrado. Copie .env.example para .env e ajuste as variaveis."
  exit 1
fi

pio run -e twatch-s3
