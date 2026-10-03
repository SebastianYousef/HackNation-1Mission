#!/usr/bin/env bash
# Install/update the Atlas backend on the GPU workstation.
#
# Everything lives in ONE directory (default ~/atlas-server) plus Docker objects
# named "atlas-*". It reads nothing else in the owner's home, installs no system
# packages and changes no system settings. Removal: deploy/gpu-server/teardown.sh.
#
#   curl -fsSL https://raw.githubusercontent.com/SebastianYousef/HackNation-1Mission/main/deploy/gpu-server/setup.sh | bash
#   # or: bash setup.sh [--gpu] [--public]
#     --gpu     also start Ollama on the GPU and pull the open-weight OpenAI model gpt-oss:20b
#     --public  expose the stack on the internet via Tailscale Funnel (https://<machine>.<tailnet>.ts.net)
set -euo pipefail

ROOT="${ATLAS_HOME:-$HOME/atlas-server}"
REPO_URL="${REPO_URL:-https://github.com/SebastianYousef/HackNation-1Mission.git}"
GPU=0; PUBLIC=0
for a in "$@"; do case "$a" in --gpu) GPU=1 ;; --public) PUBLIC=1 ;; *) echo "unknown flag $a"; exit 2 ;; esac; done

say() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
need() { command -v "$1" >/dev/null || { echo "missing prerequisite: $1 ($2)"; exit 1; }; }

# ---- prerequisites (checked, never installed by this script) -------------------------
need git "install git"
need docker "install Docker Engine / Docker Desktop (WSL2 on Windows)"
docker compose version >/dev/null 2>&1 || { echo "missing: docker compose v2"; exit 1; }
need tailscale "install Tailscale and log in to the team tailnet"
TS_IP="$(tailscale ip -4 | head -1)"
if [ "$GPU" = 1 ]; then
  docker info 2>/dev/null | grep -qi nvidia || \
    echo "warning: Docker has no NVIDIA runtime; install nvidia-container-toolkit for --gpu (skipping GPU check)"
fi

# ---- workspace ---------------------------------------------------------------------
mkdir -p "$ROOT/web-dist"
if [ -d "$ROOT/repo/.git" ]; then
  say "updating $ROOT/repo"; git -C "$ROOT/repo" pull --ff-only
else
  say "cloning into $ROOT/repo"; git clone --depth 1 "$REPO_URL" "$ROOT/repo"
fi
if [ ! -f "$ROOT/.env" ]; then
  say "creating $ROOT/.env (edit it to add OPENAI_API_KEY / BRIGHTDATA_API_KEY)"
  sed -e "s|^TS_IP=.*|TS_IP=$TS_IP|" \
      -e "s|^POSTGRES_PASSWORD=.*|POSTGRES_PASSWORD=$(head -c 24 /dev/urandom | base64 | tr -dc A-Za-z0-9)|" \
      -e "s|^WEB_DIST=.*|WEB_DIST=$ROOT/web-dist|" \
      "$ROOT/repo/deploy/gpu-server/env.example" > "$ROOT/.env"
  chmod 600 "$ROOT/.env"
fi
ln -sf "$ROOT/.env" "$ROOT/repo/backend/.env"            # api/worker env_file
[ -f "$ROOT/web-dist/index.html" ] || echo '<!doctype html><title>Atlas</title><p>Frontend not deployed yet. API: <a href="/api/v1/meta">/api/v1/meta</a>' > "$ROOT/web-dist/index.html"
[ -f "$ROOT/repo/infra/certs/dev.pem" ] || (cd "$ROOT/repo/infra" && bash scripts/gen-dev-cert.sh)

# ---- start -------------------------------------------------------------------------
DC=(docker compose --env-file "$ROOT/.env" -p atlas -f "$ROOT/repo/infra/docker-compose.yml"
    -f "$ROOT/repo/deploy/gpu-server/docker-compose.gpu.yml")
[ "$GPU" = 1 ] && DC+=(--profile gpu)
say "building and starting the stack (L4 -> L7 x2 -> api replicas, worker, web, redis, db)"
"${DC[@]}" up -d --build
if [ "$GPU" = 1 ]; then
  say "pulling gpt-oss:20b into Ollama (~13 GB, one time)"
  "${DC[@]}" exec -T ollama ollama pull gpt-oss:20b
fi

for i in $(seq 1 60); do curl -fsk "https://$TS_IP/api/v1/meta" >/dev/null 2>&1 && break; sleep 2; done
say "tailnet URL:  https://$TS_IP/api/v1/meta   (self-signed cert inside the tailnet)"
say "Postgres for the pipeline: postgresql://atlas:<POSTGRES_PASSWORD from $ROOT/.env>@$TS_IP:5432/atlas"
[ "$GPU" = 1 ] && say "gpt-oss (OpenAI-compatible): OPENAI_BASE_URL=http://$TS_IP:11434/v1"

if [ "$PUBLIC" = 1 ]; then
  say "publishing via Tailscale Funnel (HTTPS on :443 -> 127.0.0.1:8081 edge)"
  tailscale funnel --bg 8081
  tailscale funnel status
fi
echo "Stop: ${DC[*]} down    Remove everything: bash $ROOT/repo/deploy/gpu-server/teardown.sh"
