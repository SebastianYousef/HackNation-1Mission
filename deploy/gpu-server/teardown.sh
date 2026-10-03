#!/usr/bin/env bash
# Remove every trace of the Atlas backend from the workstation: containers, images built here,
# volumes (database, Ollama models), the Funnel, and the ~/atlas-server directory.
set -euo pipefail
ROOT="${ATLAS_HOME:-$HOME/atlas-server}"
read -r -p "Delete the Atlas stack, its data volumes and $ROOT? [y/N] " ok
[ "$ok" = y ] || exit 0
tailscale funnel --bg 8081 off 2>/dev/null || true
if [ -f "$ROOT/repo/infra/docker-compose.yml" ]; then
  docker compose --env-file "$ROOT/.env" -p atlas -f "$ROOT/repo/infra/docker-compose.yml" \
    -f "$ROOT/repo/deploy/gpu-server/docker-compose.gpu.yml" --profile gpu down -v --rmi local
fi
rm -rf "$ROOT"
echo "Atlas removed."
