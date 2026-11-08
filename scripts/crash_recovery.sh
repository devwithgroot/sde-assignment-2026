#!/usr/bin/env bash
# Phase 8 crash recovery helper.
# Reconciles controller SQLite with the simulator after an unexpected stop.
# Does NOT advance simulated time.
set -euo pipefail
SIM_URL="${1:?usage: ./scripts/crash_recovery.sh <SIM_URL> [DB_PATH]}"
DB_PATH="${2:-data/controller.sqlite}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
if [[ -x "$ROOT/.venv/Scripts/python.exe" ]]; then
  PY="$ROOT/.venv/Scripts/python.exe"
elif [[ -x "$ROOT/.venv/bin/python" ]]; then
  PY="$ROOT/.venv/bin/python"
else
  PY="${PYTHON:-python}"
fi
echo "crash_recovery: url=$SIM_URL db=$DB_PATH"
exec "$PY" -m controller.recovery --url "$SIM_URL" --db "$DB_PATH"
