#!/usr/bin/env bash
# Self-signed TLS cert for localhost -> infra/certs/dev.pem (cert + key, HAProxy format).
set -euo pipefail
dir="$(cd "$(dirname "$0")/.." && pwd)/certs"
mkdir -p "$dir"
[[ -f "$dir/dev.pem" && "${1:-}" != "--force" ]] && { echo "exists: $dir/dev.pem (use --force)"; exit 0; }
openssl req -x509 -newkey rsa:2048 -nodes -days 365 -subj "/CN=localhost" \
  -addext "subjectAltName=DNS:localhost,IP:127.0.0.1" \
  -keyout "$dir/dev.key" -out "$dir/dev.crt" 2>/dev/null
cat "$dir/dev.crt" "$dir/dev.key" > "$dir/dev.pem"
chmod 644 "$dir/dev.pem"   # read by the non-root haproxy user in the container; dev only
echo "wrote $dir/dev.pem"
