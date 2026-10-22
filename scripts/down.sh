#!/usr/bin/env bash
# Stop the selected target's stack. Volumes (datastore data, dashboards) are kept.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${PY:-$ROOT/.venv/bin/python}"
[[ -x "$PY" ]] || PY=python3
cd "$ROOT"
vars="$("$PY" -m break_point target --shell)" || { echo "$vars" >&2; exit 1; }
eval "$vars"

docker compose -p "$COMPOSE_PROJECT" -f "$COMPOSE_FILE" down

echo
echo "Stack stopped. Volumes were kept."
echo "To also delete them, run:"
echo "  docker compose -p $COMPOSE_PROJECT -f $COMPOSE_FILE down -v"
