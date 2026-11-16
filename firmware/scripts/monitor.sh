#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."
pio device monitor --baud 115200
