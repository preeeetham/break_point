#!/usr/bin/env bash
# Show container state and live resource usage for the selected target's stack.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${PY:-$ROOT/.venv/bin/python}"
[[ -x "$PY" ]] || PY=python3
cd "$ROOT"
vars="$("$PY" -m break_point target --shell)" || { echo "$vars" >&2; exit 1; }
eval "$vars"

echo "== docker compose ps =="
docker compose -p "$COMPOSE_PROJECT" -f "$COMPOSE_FILE" ps

echo
echo "== docker stats (one-shot) =="
ids="$(docker compose -p "$COMPOSE_PROJECT" -f "$COMPOSE_FILE" ps -q 2>/dev/null || true)"
if [[ -z "$ids" ]]; then
  echo "No running containers for project '$COMPOSE_PROJECT'."
  exit 0
fi
# shellcheck disable=SC2086
docker stats --no-stream $ids
