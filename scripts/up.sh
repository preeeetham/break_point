#!/usr/bin/env bash
# Bring up the selected target's stack (BREAK_POINT_TARGET in .env) and wait until it reports healthy.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${PY:-$ROOT/.venv/bin/python}"
[[ -x "$PY" ]] || PY=python3
cd "$ROOT"
vars="$("$PY" -m break_point target --shell)" || { echo "$vars" >&2; exit 1; }
eval "$vars"

cd "$(dirname "$COMPOSE_FILE")"
if [[ -f .env.example && ! -f .env ]]; then
  echo "No $(pwd)/.env found - creating it from .env.example"
  cp .env.example .env
fi

docker compose -p "$COMPOSE_PROJECT" -f "$COMPOSE_FILE" up -d

echo
echo "Waiting for ${BREAK_POINT_TARGET:-the target} at ${HEALTH_URL} (up to 10 minutes)..."
cd "$ROOT"
if ! "$PY" -m break_point target --wait 600; then
  echo "Check: docker compose -p $COMPOSE_PROJECT -f $COMPOSE_FILE ps / logs" >&2
  exit 1
fi
echo
"$PY" -m break_point target
