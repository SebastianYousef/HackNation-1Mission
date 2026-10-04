#!/usr/bin/env bash
# Auto-deploy for the live server: run ~/atlas/redeploy.sh whenever origin/main moves.
# Started every minute by the systemd --user timer atlas-autodeploy.timer (see install.sh).
# Guest rules apply: no sudo, stays inside $HOME, never prints secrets.
set -euo pipefail

APP="${ATLAS_APP:-$HOME/app}"                     # the server's clone of this repo
REDEPLOY="${ATLAS_REDEPLOY:-$HOME/atlas/redeploy.sh}"
STATE="${ATLAS_AUTODEPLOY_STATE:-$HOME/atlas/autodeploy}"
LOG="$STATE/autodeploy.log"
mkdir -p "$STATE"

# one run at a time; a deploy that is still running makes the next tick a no-op
exec 9>"$STATE/lock"
flock -n 9 || exit 0

log() { printf '%s %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*" >> "$LOG"; }

remote=$(git -C "$APP" ls-remote --quiet origin refs/heads/main | cut -f1)
[ -n "$remote" ] || { log "could not reach origin, will retry"; exit 0; }
deployed=$(cat "$STATE/deployed_sha" 2>/dev/null || true)
[ "$remote" = "$deployed" ] && exit 0

log "main moved: ${deployed:0:7} -> ${remote:0:7}, running redeploy"
if "$REDEPLOY" >> "$LOG" 2>&1; then
  echo "$remote" > "$STATE/deployed_sha"
  log "deployed ${remote:0:7}"
else
  # leave deployed_sha unchanged so the next tick retries; back off for 5 minutes after a failure
  log "redeploy FAILED for ${remote:0:7} (the previous version stays live)"
  sleep 300
fi

# keep the log small
tail -n 2000 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
