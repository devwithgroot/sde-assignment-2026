#!/usr/bin/env bash
# Phase 9 contract: ./run.sh <SIM_URL> starts the controller and runs until the
# scenario finishes (POST /sim/advance each cycle). It does not start the simulator.
set -euo pipefail
SIM_URL="${1:?usage: ./run.sh <SIM_URL>}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"
if [[ -x "$ROOT/.venv/Scripts/python.exe" ]]; then
  PY="$ROOT/.venv/Scripts/python.exe"
elif [[ -x "$ROOT/.venv/bin/python" ]]; then
  PY="$ROOT/.venv/bin/python"
else
  PY="${PYTHON:-python}"
fi
export PYTHONUTF8="${PYTHONUTF8:-1}"
export PYTHONUNBUFFERED="${PYTHONUNBUFFERED:-1}"
exec "$PY" -m controller.main --url "$SIM_URL"
