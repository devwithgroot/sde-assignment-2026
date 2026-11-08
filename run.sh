#!/usr/bin/env bash
# Phase 1 contract: ./run.sh <SIM_URL> reads the simulator once and exits.
# It does not advance time, submit approvals, or change workflow config.
# Later phases extend this script so it runs until the scenario finishes.
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
exec "$PY" -m controller.main --url "$SIM_URL"
