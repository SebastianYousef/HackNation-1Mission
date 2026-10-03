#!/usr/bin/env bash
# Offline checks for the infra configs (no Docker needed):
#   - haproxy -c on l4.cfg, l7.cfg (cert path pointed at a throwaway cert) and local-native.cfg
#   - client-IP trust: uvicorn ignores proxy headers (the app reads X-Forwarded-For itself,
#     TRUSTED_PROXY_HOPS from the right), compose pins TRUSTED_PROXY_HOPS=1, and the api
#     service is never published on the host (only the L7 may reach it)
#   infra/scripts/check-configs.sh          (from anywhere; HAPROXY=, PYTHON= to override)
set -euo pipefail
infra="$(cd "$(dirname "$0")/.." && pwd)"
hap="${HAPROXY:-haproxy}"
tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
fail=0
ok()  { echo "ok    $*"; }
bad() { echo "FAIL  $*"; fail=1; }

# --- haproxy -c -----------------------------------------------------------------
openssl req -x509 -newkey rsa:2048 -nodes -days 1 -subj /CN=localhost \
  -keyout "$tmp/k.pem" -out "$tmp/c.pem" >/dev/null 2>&1
cat "$tmp/c.pem" "$tmp/k.pem" > "$tmp/dev.pem"
sed "s#/usr/local/etc/haproxy/certs/#$tmp/#" "$infra/haproxy/l7.cfg" > "$tmp/l7.cfg"
for cfg in "$infra/haproxy/l4.cfg" "$tmp/l7.cfg" "$infra/haproxy/local-native.cfg"; do
  if out="$("$hap" -c -f "$cfg" 2>&1)"; then ok "haproxy -c $(basename "$cfg")"
  else bad "haproxy -c $(basename "$cfg")"; echo "$out"; fi
done

# --- client-IP trust ------------------------------------------------------------
df="$infra/api.Dockerfile"
if grep -Eq -- "^CMD .*--no-proxy-headers" "$df"; then ok "api.Dockerfile: uvicorn --no-proxy-headers"
else bad "api.Dockerfile: CMD must run uvicorn with --no-proxy-headers"; fi
if grep -Eq -- "forwarded-allow-ips|FORWARDED_ALLOW_IPS" "$df"; then bad "api.Dockerfile sets forwarded-allow-ips"
else ok "api.Dockerfile: no forwarded-allow-ips"; fi

"${PYTHON:-python3}" - "$infra/docker-compose.yml" <<'PY' || fail=1
import re, sys
text = open(sys.argv[1]).read()
problems = []
if not re.search(r'^\s+TRUSTED_PROXY_HOPS: "1"\s*(#.*)?$', text, re.M):
    problems.append('TRUSTED_PROXY_HOPS must be pinned to "1" (one L7 hop)')
try:
    import yaml
except ImportError:
    yaml = None
if yaml:
    api = yaml.safe_load(text)["services"]["api"]
    if api.get("ports"):
        problems.append("api publishes host ports; only the L7 may reach it")
    if "FORWARDED_ALLOW_IPS" in (api.get("environment") or {}):
        problems.append("api sets FORWARDED_ALLOW_IPS but the image runs --no-proxy-headers")
for p in problems:
    print("FAIL  compose:", p)
if not problems:
    print("ok    compose: TRUSTED_PROXY_HOPS=1, api not published",
          "" if yaml else "(PyYAML missing: ports not checked)")
sys.exit(1 if problems else 0)
PY

exit "$fail"
