#!/usr/bin/env bash
# Native load-balancing demo (no Docker): 3 API replicas + HAProxy round robin,
# then a graceful drain of one replica. KEEP=1 leaves everything running.
#   infra/scripts/lb-native.sh            (from the repo root, or via `make lb-native`)
set -euo pipefail
root="$(cd "$(dirname "$0")/../.." && pwd)"
py="${PYTHON:-$root/backend/api/.venv/bin/python}"
logs="${TMPDIR:-/tmp}/atlas-lb-native"; mkdir -p "$logs"
pids=()
cleanup() { for p in "${pids[@]}"; do kill "$p" 2>/dev/null || true; done; wait 2>/dev/null || true; }
[[ "${KEEP:-0}" == 1 ]] || trap cleanup EXIT

for i in 1 2 3; do
  (cd "$root/backend/api" && INSTANCE_ID="api-$i" DATA_MODE="${DATA_MODE:-fixtures}" SHUTDOWN_GRACE_SECONDS=3 \
     exec "$py" -m uvicorn atlas_api.main:app --host 127.0.0.1 --port "800$i" --log-level warning \
     >"$logs/api-$i.log" 2>&1) &
  pids+=($!)
done
haproxy -f "$root/infra/haproxy/local-native.cfg" >"$logs/haproxy.log" 2>&1 &
pids+=($!)

lb=http://127.0.0.1:8088
up() { [[ "$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:800$1/readyz" || true)" == 200 ]]; }
for _ in $(seq 100); do  # wait until all 3 replicas are ready ...
  up 1 && up 2 && up 3 && break
  sleep 0.2
done
sleep 2.5                # ... and HAProxy has seen 2 passing checks (rise 2, inter 1s)

hit() { curl -s -o /dev/null -D - "$lb/api/v1/meta" | tr -d '\r' | awk -F': ' 'tolower($1)=="x-served-by"{s=$2} tolower($1)=="x-request-id"{r=$2} END{printf "%s  (request %s)\n", s, r}'; }

echo "== 9 requests through the LB (round robin):"
for _ in $(seq 9); do hit; done

echo "== SIGTERM api-2: its /readyz turns 503 for 3 s while the LB drains it"
kill -TERM "${pids[1]}"
sleep 0.3
echo "api-2 /readyz -> $(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8002/readyz)"
sleep 2.5
echo "== 6 requests after the drain (api-2 gone, no errors):"
for _ in $(seq 6); do hit; done
echo "HAProxy stats: http://127.0.0.1:8405/stats   logs: $logs"
if [[ "${KEEP:-0}" == 1 ]]; then echo "KEEP=1: still running (pids ${pids[*]})"; fi
