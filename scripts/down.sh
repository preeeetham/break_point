#!/usr/bin/env bash
# Stop the Faultline sandbox. Volumes (MongoDB data, Grafana state) are kept.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT/deploy"

docker compose down

echo
echo "Stack stopped. Volumes were kept."
echo "To also delete the MongoDB data and Grafana state, run:"
echo "  (cd $ROOT/deploy && docker compose down -v)"
