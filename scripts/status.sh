#!/usr/bin/env bash
# Show container state and live resource usage for the Faultline sandbox.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT/deploy"

echo "== docker compose ps =="
docker compose ps

echo
echo "== docker stats (one-shot) =="
ids="$(docker compose ps -q 2>/dev/null || true)"
if [[ -z "$ids" ]]; then
  echo "No running containers for project 'faultline'."
  exit 0
fi
# shellcheck disable=SC2086
docker stats --no-stream $ids
