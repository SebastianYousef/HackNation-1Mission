#!/usr/bin/env bash
# Copy the bridge (contract types + fixtures + frontend spec) into the frontend repo.
# usage: scripts/sync-contract.sh ../HackNation-1Mission-web
set -euo pipefail
FRONT="${1:?usage: $0 <path-to-frontend-repo>}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

mkdir -p "$FRONT/src/contract" "$FRONT/src/mocks/fixtures"
cp "$ROOT/contract/atlas.ts" "$FRONT/src/contract/atlas.ts"
rsync -a --delete "$ROOT/contract/fixtures/" "$FRONT/src/mocks/fixtures/"
cp "$ROOT/frontend/FRONTEND_SPEC.md" "$FRONT/FRONTEND_SPEC.md"
cp "$ROOT/frontend/CLAUDE.md" "$FRONT/CLAUDE.md"

echo "Synced contract $(grep -o 'CONTRACT_VERSION = "[^"]*"' "$ROOT/contract/atlas.ts") into $FRONT"
echo "Next: commit and push in the frontend repo."
