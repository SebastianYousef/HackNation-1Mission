# Running the backend on the team's GPU workstation

A teammate's friend lends a powerful PC with a dedicated GPU, on the team's Tailscale network (`100.87.219.50`). It hosts the **entire backend**: L4 → L7 → N API replicas, worker, Redis, Postgres. It can also host **gpt-oss** on the GPU. The internet reaches it via **Tailscale Funnel**: no router port-forwarding, no public IP and no firewall changes.

## Ground rules: we are guests on this machine
- Everything lives in **one directory, `~/atlas-server/`**, plus Docker containers and volumes prefixed `atlas`. Nothing else in the owner's home is read or written.
- The script **installs nothing and changes no system settings**. Docker, the NVIDIA container toolkit and Tailscale must already be there; it only checks for them.
- **CPU and memory limits** on every container keep the PC usable. Ollama only runs with `--gpu`.
- Nothing listens on the LAN or the internet directly. Ports bind to `127.0.0.1` (tunnel) or the Tailscale IP (team only).
- `teardown.sh` removes everything: containers, volumes, the Funnel and `~/atlas-server`.
- The owner runs the script themselves, or gives us a separate user account / Tailscale SSH access. We never log in with their personal account.

## Architecture on one machine

```
Internet ── https://<machine>.<tailnet>.ts.net ──► Tailscale Funnel (public TLS edge, on the PC)
                                                         │ plain HTTP, 127.0.0.1:8081
Teammates (tailnet) ── https://100.87.219.50 ──┐         ▼
                                               └──► [L4 HAProxy] ── TCP + PROXY v2 ──► [L7 HAProxy ×2]
                                                                                         │ /api/*  leastconn
                                                                       ┌─────────────────┼──────────────┐
                                                                    [api-1] [api-2] [api-3] [api-4]   [web ×2]
                                                                       └──── Redis ──── Postgres ──── worker ──► Bright Data
                                                                                           ▲
Teammates' laptops: pipeline `make load` ── 100.87.219.50:5432 ┘
                    `make extract` with OPENAI_BASE_URL=http://100.87.219.50:11434/v1 ──► [Ollama: gpt-oss on GPU]
```

- **Funnel = the public L4/TLS edge.** Tailscale's relays carry the TCP stream to the PC. `tailscaled` terminates TLS with a real certificate for `*.ts.net` and hands plain HTTP to our L4 on the dedicated edge port 8081.
  - The L7 trusts `X-Forwarded-For` on that port only, so per-client rate limits still work.
  - Funnel only allows ports 443/8443/10000 and must be allowed for the node in the tailnet policy. `tailscale funnel` prints a link if an admin has to approve it.
- **Horizontal scaling:**
  - **on one box:** `API_REPLICAS=8` in `.env`, then re-run the script. The stateless replicas are discovered by the L7 through Docker DNS.
  - **across boxes:** any other tailnet machine can run `api` containers. Add them to the L7 as `server api-remote-1 100.x.y.z:8000 check` (Tailscale gives every node a stable 100.x IP, so the LB balances across machines exactly as across containers).
  - **sticky sessions** stay off, because all state lives in Postgres/Redis.

## What the GPU is for
Our backend doesn't *need* a GPU: search, similarity, clustering and paths are CPU work on a small graph. The GPU becomes valuable for **running OpenAI's open-weight model `gpt-oss-20b` locally** with Ollama:
- **Bulk extraction for free.** The pipeline's `extract` stage over thousands of abstracts, with no API cost and no rate limits. The `openai` SDK honours `OPENAI_BASE_URL`, so no code changes: `OPENAI_BASE_URL=http://100.87.219.50:11434/v1 OPENAI_API_KEY=ollama OPENAI_MODEL=gpt-oss:20b make extract`.
- **It is still an OpenAI model**, which supports the prize-track story ("hosted GPT for live explanations, gpt-oss on our own GPU for bulk extraction"). Confirm with the organizers that open-weight gpt-oss counts as "leveraging OpenAI's models".
- **Needs ~16 GB of GPU memory** for `gpt-oss:20b`. With less, keep hosted OpenAI for everything.
- **Verify Structured Outputs** through Ollama's OpenAI-compatible endpoint with `make extract ARGS="--limit 5"`. The quote-substring check drops bad output either way.
- **Keep the live `/explain` on hosted OpenAI** (fast and reliable for the demo).

## Setup (owner or teammate, ~15 min + model download)
Prerequisites on the PC: Docker (Linux: Docker Engine + compose v2; Windows: Docker Desktop with WSL2 and run everything inside WSL), Tailscale logged into the team tailnet, and for `--gpu` the NVIDIA driver + nvidia-container-toolkit (Windows: recent NVIDIA driver; WSL2 GPU support is built in).

```bash
curl -fsSLO https://raw.githubusercontent.com/SebastianYousef/HackNation-1Mission/main/deploy/gpu-server/setup.sh
less setup.sh                      # read it first, it's short
bash setup.sh --gpu                # tailnet only; add --public to open the Funnel URL
nano ~/atlas-server/.env           # add OPENAI_API_KEY, BRIGHTDATA_API_KEY, then: bash setup.sh again
```
On Linux, `tailscale funnel` needs root or a Tailscale operator. The owner decides whether to run `sudo tailscale set --operator=$USER` once, or to run the funnel step with sudo.

**Loading data from a teammate's laptop:**
```bash
export DATABASE_URL="postgresql://atlas:<POSTGRES_PASSWORD>@100.87.219.50:5432/atlas"
cd backend/pipeline && make load          # the API serves the new dataset immediately (cache keyed on version)
```

**Deploying the frontend:** build the Lovable repo (`npm run build`) and copy `dist/*` into `~/atlas-server/web-dist/`. It's served at `/` behind the same LB, same origin as `/api`. Set `VITE_API_BASE=""` for that build.

**Operations:**

| Task | Command |
|---|---|
| status / logs | `docker compose -p atlas ps`, `docker compose -p atlas logs -f api` |
| scale | edit `API_REPLICAS`, re-run `setup.sh` |
| LB demo | `for i in $(seq 10); do curl -sk https://100.87.219.50/api/v1/meta -o /dev/null -D - \| grep -i x-served-by; done` |
| stop | `docker compose -p atlas down` (data kept) |
| remove all | `bash ~/atlas-server/repo/deploy/gpu-server/teardown.sh` |

## Reaching the PC from a dev laptop
The laptop must be on the same tailnet:
1. Install Tailscale.
   - On immutable distros without root, use userspace mode: `tailscaled --tun=userspace-networking --socks5-server=localhost:1055 &`, then `tailscale up`.
2. Check the connection with `tailscale ping 100.87.219.50`.
3. Shell access needs the owner's consent: Tailscale SSH enabled by them in the tailnet policy, or an SSH account they created for the team.

## Risks
| Risk | Mitigation |
|---|---|
| PC sleeps, reboots or is needed by its owner during judging | containers `restart: unless-stopped`; ask the owner to disable sleep for the judging window; keep the cloudflared / Lovable + `?mock=1` snapshot fallback from `docs/ARCHITECTURE.md` §8 |
| Home upload bandwidth | static assets are tiny; API responses are cached; Funnel is fine for demo traffic |
| Funnel not allowed in the tailnet policy | `cloudflared tunnel --url http://127.0.0.1:8081` gives an instant public HTTPS URL to the same edge port |
