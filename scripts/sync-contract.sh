#!/usr/bin/env bash
# Copy the bridge (contract types + fixtures + Lovable context) into the Lovable frontend repo.
# usage: scripts/sync-contract.sh ../HackNation-1Mission-web
set -euo pipefail
FRONT="${1:?usage: $0 <path-to-frontend-repo>}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

mkdir -p "$FRONT/src/contract" "$FRONT/src/mocks/fixtures"
cp "$ROOT/contract/atlas.ts" "$FRONT/src/contract/atlas.ts"
rsync -a --delete "$ROOT/contract/fixtures/" "$FRONT/src/mocks/fixtures/"
cp "$ROOT/frontend/LOVABLE.md" "$FRONT/LOVABLE.md"
cp "$ROOT/frontend/CLAUDE.md" "$FRONT/CLAUDE.md"

echo "Synced contract $(grep -o 'CONTRACT_VERSION = "[^"]*"' "$ROOT/contract/atlas.ts") into $FRONT"
echo "Next: commit in the frontend repo; Lovable picks it up via GitHub sync."
