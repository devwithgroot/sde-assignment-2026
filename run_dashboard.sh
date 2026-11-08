#!/usr/bin/env bash
# Read-only monitor. Does not advance the simulator or change config.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"
DB="${1:-data/controller.sqlite}"
SIM_URL="${2:-}"
if [[ -x "$ROOT/.venv/Scripts/python.exe" ]]; then
  PY="$ROOT/.venv/Scripts/python.exe"
elif [[ -x "$ROOT/.venv/bin/python" ]]; then
  PY="$ROOT/.venv/bin/python"
else
  PY="${PYTHON:-python}"
fi
if [[ -n "$SIM_URL" ]]; then
  exec "$PY" -m dashboard --db "$DB" --sim-url "$SIM_URL"
else
  exec "$PY" -m dashboard --db "$DB"
fi
