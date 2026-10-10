#!/usr/bin/env bash
# Bring up the Faultline sandbox (Ditto + monitoring) and wait until the gateway is healthy.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT/deploy"

if [[ ! -f .env ]]; then
  echo "No deploy/.env found - creating it from .env.example"
  cp .env.example .env
fi

# shellcheck disable=SC1091
set -a; source .env; set +a
PORT="${DITTO_PORT:-8080}"
HEALTH_URL="http://127.0.0.1:${PORT}/health"

docker compose up -d

echo
echo "Waiting for Ditto gateway at ${HEALTH_URL} (up to 10 minutes)..."
deadline=$(( $(date +%s) + 600 ))
until body="$(curl -fsS --max-time 5 "$HEALTH_URL" 2>/dev/null)" \
      && grep -Eq '"status"[[:space:]]*:[[:space:]]*"UP"' <<<"$body"; do
  if (( $(date +%s) >= deadline )); then
    echo "Timed out waiting for Ditto to become healthy. Last response:" >&2
    echo "${body:-<none>}" >&2
    echo "Check: docker compose -f $ROOT/deploy/docker-compose.yml ps / logs" >&2
    exit 1
  fi
  printf '.'
  sleep 5
done
echo
echo "Ditto is UP."
echo
echo "  Ditto API    : http://127.0.0.1:${PORT}/api/2/things"
echo "  Ditto UI     : http://127.0.0.1:${PORT}/ui/"
echo "  Health       : ${HEALTH_URL}"
echo "  Prometheus   : http://127.0.0.1:9090"
echo "  Grafana      : http://127.0.0.1:3000  (anonymous viewer; admin / ${GRAFANA_ADMIN_PASSWORD:-admin})"
echo
echo "  API credentials: ditto / ditto   (devops: devops / ${DEVOPS_PASSWORD:-foobar})"
