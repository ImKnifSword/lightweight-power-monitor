#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Create a writable runtime dir for optional state/logs if needed later
mkdir -p "${HOME}/.local/share/lightweight-power-monitor" || true

# Run using system Python; fallback to python3 if available
PYTHON_BIN="$(command -v python3 || true)"
if [ -z "${PYTHON_BIN}" ]; then
  echo "python3 not found" >&2
  exit 1
fi

exec "${PYTHON_BIN}" "${SCRIPT_DIR}/power_monitor.py"
