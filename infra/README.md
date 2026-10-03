# Infra: load balancing and horizontal scaling

The stack is two tiers of load balancers in front of N stateless API replicas. Shared state lives only in Postgres and Redis, so replicas can be added or removed at any time.

```
                     Internet
                        │  DNS: atlas.example.org  A  203.0.113.10   (public LB IP)
                        ▼
  ┌───────────────────────────────────────────────┐  L4 / TCP  (HAProxy mode tcp | OCI Network LB)
  │ l4   203.0.113.10 :80 :443                     │  balance source (hash of client IP)
  │      TLS passthrough, no HTTP parsing          │  sends PROXY protocol v2 (client ip:port)
  └──────────────┬──────────────────────┬─────────┘
                 │ TCP + PROXY v2       │
                 ▼                      ▼
  ┌──────────────────────┐  ┌──────────────────────┐  L7 / HTTP  (HAProxy mode http | OCI Flexible LB)
  │ l7-a   10.0.1.11     │  │ l7-b   10.0.1.12     │  TLS termination, accept-proxy
  │ :8443 TLS  :8404 mon │  │ :8443 TLS  :8404 mon │  X-Forwarded-For/Proto, X-Request-Id
  └───┬──────────────┬───┘  └───┬──────────────┬───┘  per-IP rate limit (stick-table)
      │ /api/*       │ else     │              │      health checks GET /readyz
      ▼              ▼          ▼              ▼
  ┌────────────────────────────┐   ┌───────────────────┐
  │ api ×N  10.0.1.21-2N :8000 │   │ web ×2  :8080     │   private subnet 10.0.1.0/24
  │ FastAPI/uvicorn, stateless │   │ nginx, SPA dist/  │   (Docker: bridge net 172.18.0.0/16,
  └──────┬───────────────┬─────┘   └───────────────────┘    names resolved by 127.0.0.11)
         │               │
         ▼               ▼
  ┌──────────────┐  ┌──────────────────────────┐      ┌──────────────────────────┐
  │ Postgres     │  │ Redis 10.0.1.30:6379     │◄─────│ worker ×M (gap-search →  │
  │ 16 + pgvector│  │ cache · rate counters ·  │ BRPOP│ Bright Data SERP API)    │
  │ :5432        │  │ job queue + job status   │      └──────────────────────────┘
  └──────────────┘  └──────────────────────────┘
```

## What each layer sees

| OSI layer | Where | What happens |
|---|---|---|
| **L3 (network)** | Internet → public IP; VCN/VPC routing; Docker bridge | Packets are routed by IP. Only the LB owns a public IP. App servers live in the private subnet `10.0.1.0/24`, and firewall rules (OCI security lists / Docker networks) let only the LB tier reach them. |
| **L4 (transport)** | `l4` | The LB sees the TCP 5-tuple (src IP, src port, dst IP, dst port, TCP) and nothing else. It cannot read paths, cookies or hostnames because TLS is passed through. `balance source` + `hash-type consistent` hashes the client IP, so one client keeps hitting the same L7 node (good for TLS session reuse and per-client rate limits), and adding or removing an L7 node remaps only about 1/N of clients. After this hop the TCP source address is the L4's own IP, so the L4 **prepends a PROXY protocol v2 header** carrying the original client IP and port. The L7 reads it with `accept-proxy`. |
| **L5/6 (session/presentation)** | `l7` | **TLS terminates here** (`ssl crt dev.pem`, TLS ≥ 1.2, ALPN h2/http1.1). Traffic from the L7 to the API runs inside the private network as plain HTTP. |
| **L7 (application)** | `l7` | The LB reads HTTP. Path routing: `/api/*` → api pool, everything else → web pool. `/healthz` and `/readyz` are blocked from outside. It replaces any client-sent `X-Forwarded-For` with exactly one entry, the real client IP (from the PROXY header, or the edge's last hop on `:8081`), and adds `X-Forwarded-Proto`, `X-Request-Id` (if the client didn't send one) and `X-LB-L7` (which L7 node answered). Rate limit: more than 200 req/10 s per client IP gets a 429 with the ApiError JSON body. Errors the LB generates itself (502/503/504: no replica up, replica reset or slower than `timeout server 30s`) also get an ApiError JSON body (`upstream_unavailable`) with the request id. Cookie stickiness is available but off (see below). |
| **App** | `api` | Uvicorn runs with `--no-proxy-headers`, so `request.client.host` is the socket peer (the L7). The app takes the client IP for logs and rate limits from the rightmost `X-Forwarded-For` entry (`TRUSTED_PROXY_HOPS=1`), which only the L7 writes, so clients can't choose their rate-limit bucket. Every response carries `X-Served-By` (replica hostname) and the propagated `X-Request-Id`, so one request can be traced through every hop's logs. |

## Client IP and forwarded headers, hop by hop
The client's address survives two proxies without the API ever trusting a header a client could have written:

| Hop | Client address comes from | Header handling |
|---|---|---|
| client → `l4` | TCP source address | none (TCP only, TLS passthrough) |
| `l4` → `l7` | **PROXY protocol v2** header that `l4` prepends (`send-proxy-v2` on every `server-template` line). `l7` binds `:8080`, `:8443` and `:8081` with `accept-proxy`, so `src` is the real client. Health checks go to `:8404` without a PROXY header. | none |
| edge → `l4:8081` → `l7` (Tailscale Funnel / cloudflared, see `deploy/gpu-server`) | The PROXY source is the tunnel (127.0.0.1), so on `:8081` only, `l7` takes the rightmost `X-Forwarded-For` entry the edge appended (`set-src hdr_ip(x-forwarded-for,-1)`) | the edge already terminated TLS |
| `l7` → `api` | `l7` **replaces** `X-Forwarded-For` with exactly one entry, `%[src]` (no `option forwardfor`, which would append to whatever the client sent). It overwrites `X-Forwarded-Proto: https`, `X-Forwarded-Port`, and sets `X-Request-Id` if absent. | client-sent `X-Forwarded-For` / `X-Forwarded-Proto` never reach the API |
| inside `api` | `routes.client_ip()`: the `X-Forwarded-For` entry `TRUSTED_PROXY_HOPS` hops from the right, validated as an IP (anything else falls back to the socket peer, so garbage never becomes a Redis key) | uvicorn `--no-proxy-headers`: no header rewrites `request.client` or the scheme |

**`TRUSTED_PROXY_HOPS`** = the number of proxies between the client and the API that each append one `X-Forwarded-For` entry. It is pinned to `"1"` in `docker-compose.yml` (the L7), and is not read from `backend/.env`, so a `TRUSTED_PROXY_HOPS=0` meant for a bare `make api-dev` can't make every client share the L7's rate-limit bucket. Use `0` only when clients connect to uvicorn directly. Raise it if you put another appending proxy (a CDN, a cloud L7) in front of `l7`, or use the platform's hop count on Cloud Run or Render.

**Why `--no-proxy-headers` and not `--proxy-headers`:** the app builds no absolute URLs. It sets `redirect_slashes=False`, so `/api/v1/meta/` is a 404 ApiError, not a 307 to an absolute `http://` URL, and its only `Location` header (`POST /gap-search` → `/api/v1/jobs/{id}`) is relative. The scheme uvicorn sees (`http`, from the L7) therefore never reaches a client, and nothing needs `X-Forwarded-Proto`. If the app ever needs absolute URLs, switch the image to `--proxy-headers` with `FORWARDED_ALLOW_IPS` set to the L7's private subnet only. Never use `'*'`, because uvicorn would then rewrite `request.client` from a header for any peer. `infra/scripts/check-configs.sh` fails if the image drops `--no-proxy-headers`, if it sets forwarded-allow-ips, or if compose stops pinning `TRUSTED_PROXY_HOPS` or publishes the api port.

## Errors the load balancer generates itself
Every error body follows the contract's `ApiError` shape (`{"error":{"code","message","request_id"}}`, `Content-Type: application/json`), so the frontend never has to parse an HAProxy HTML page. The request id is JSON-escaped (`json(utf8s)`), and for 502/503/504 it is kept in `txn.rid` and echoed in an `X-Request-Id` header.

| Status | `code` | When |
|---|---|---|
| 404 | `not_found` | `/healthz` or `/readyz` requested from outside (replica probes stay internal) |
| 429 | `rate_limited` | more than 200 requests per 10 s from one client IP (`Retry-After: 10`) |
| 502 | `upstream_unavailable` | a replica reset the connection or sent an invalid response |
| 503 | `upstream_unavailable` | no replica is UP (all failing `/readyz`, or none started yet) |
| 504 | `upstream_unavailable` | a replica took longer than `timeout server 30s`. The API caps its own OpenAI calls below 25 s and answers with its own 503 first, so in practice this means a hung replica. |

`local-native.cfg` uses the same bodies. The API's own errors (including 413 for bodies over `MAX_BODY_BYTES`, 64 KiB) come from the app.

## Health checks and draining
- **L4 → L7:** `GET /healthz` on the L7 monitor port 8404, every 2 s. Two failures mark the node DOWN; two passes mark it UP again. These checks are sent without a PROXY header (`check port` disables it).
- **L7 → api:** `GET /readyz` every 2 s with `fall 2 rise 2 slowstart 10s`. `/readyz` returns **503 only when the replica is draining or its data source is down** (Postgres in `DATA_MODE=db`/mixed, the fixtures directory in `DATA_MODE=fixtures`), so the LB sends no traffic to a replica that can't serve.
  - **Redis is not a readiness condition.** It is shared by every replica, so failing on it would pull the *whole* pool out of the LB at once. The app fails open without it (no response cache, rate limits not enforced). A Redis outage shows up as `200` with `"degraded": {"redis": true}`. Only gap search (`POST /api/v1/gap-search` and polling `GET /api/v1/jobs/{id}`) needs Redis: the POST answers its own 503 `upstream_unavailable` ("job queue unavailable") while Redis is down, and job status lives only in Redis, so polling a job answers 404 `not_found` until Redis is back. Watch `degraded` in monitoring, not the LB state.
  - Body: `{"ready": true, "instance": "<hostname>", "checks": {"accepting": true, "db": true}, "degraded": {"redis": false}}` (`checks.fixtures` instead of `checks.db` in fixtures mode, `degraded` is `{}` without `REDIS_URL`). It is always `Cache-Control: no-store`.
- **Worker:** no HTTP server, so compose disables the image's `/healthz` healthcheck for it (`healthcheck: disable: true`). `restart: unless-stopped` covers crashes.
- **Draining on deploy or scale-down:**
  1. Docker/Kubernetes sends SIGTERM.
  2. The API flips `/readyz` to 503 straight away and keeps serving for `SHUTDOWN_GRACE_SECONDS` (5 s in compose).
  3. HAProxy marks the replica DOWN after 2 failed checks (about 4 s) and stops sending it new requests.
  4. Uvicorn shuts down gracefully, finishing in-flight requests (`--timeout-graceful-shutdown 20`, `stop_grace_period: 30s`, which must stay above 5 s + 20 s).
  5. **Workers** get `stop_grace_period: 60s`. On SIGTERM a worker takes no new job, and the running gap search gets time to finish rather than being SIGKILLed and left `running` in Redis. If one is killed anyway, the API's `running` status expires after 120 s and polls get a 404 instead of hanging for an hour. Keep the API's total Bright Data budget per job below 60 s.

  Result: no failed requests. This was measured natively (`make lb-native`): HAProxy logged *"api-2 is DOWN, reason: Layer7 wrong status, code: 503"* before the process exited, and the next requests all went to api-1 and api-3.

## Horizontal scaling
```bash
make up                 # l4, 2× l7, 3× api, worker, 2× web, redis, db
make scale N=6          # = docker compose up -d --no-recreate --scale api=6
make lb-demo            # 10 requests → prints api=<container id> l7=<l7 node>
```
The L7 uses `server-template api- 20 api:8000 ... resolvers docker`. HAProxy re-resolves the Docker DNS name `api` every few seconds (`hold valid 5s`) and fills or empties up to 20 server slots, so new replicas get traffic within about 5 s and removed ones disappear, with no LB reload. To go beyond 20 replicas, raise the template count. Workers scale the same way: `docker compose up -d --scale worker=3` (BRPOP hands each job to exactly one worker).

Scaling is safe because **the API is stateless**:
- Cache, rate-limit counters, the job queue and job status live in Redis.
- Data, cached explanations and submissions live in Postgres.
- No sessions and no per-replica memory that matters.

Without `REDIS_URL` the API falls back to in-process cache and jobs. That mode is for single-process dev only.

### Sticky sessions: off on purpose
Since any replica can answer any request, stickiness would only add uneven load and failover pain. To turn it on, uncomment these two lines in `be_api` (`haproxy/l7.cfg`):
```
dynamic-cookie-key atlas-demo
cookie SERVERID insert indirect nocache httponly secure dynamic
```
HAProxy then sets a `SERVERID` cookie with a per-server hash. `dynamic` is what makes this work with `server-template`. It makes sense once a replica holds per-user state that is expensive to rebuild, such as a WebSocket or SSE session, an in-process conversation context for a chat-style explainer, or a large per-user warm cache. A commented `be_api_explain` backend shows the alternative: `balance uri` + consistent hashing for `POST /api/v1/explain`. We don't need it because the explanation cache is in Postgres and shared by all replicas.

### DB connection budget
Each uvicorn worker process has its own psycopg pool:

```
connections = api_replicas × WEB_CONCURRENCY × DB_POOL_SIZE  (+ worker processes × 1, + pipeline)
            = 3 × 2 × 5 = 30
```
Keep that number far below Postgres `max_connections` when you connect directly (no deployment uses Supabase). If you put a **transaction pooler** in front (PgBouncer, or Supavisor on a managed Postgres), keep it below the pooler's client limit instead. A transaction pooler multiplexes many client connections onto a few server connections, which is why the API disables server-side prepared statements (`prepare_threshold=None`). When scaling to N replicas, lower `DB_POOL_SIZE` so that N × 2 × pool stays inside the budget. Most GETs are Redis cache hits anyway.

## Files
| File | Purpose |
|---|---|
| `docker-compose.yml` | Services: l4, l7 (×2), api (×3), worker, web (×2), redis, db (pgvector/pg16, runs `backend/db/migrations` on first init; local only) |
| `haproxy/l4.cfg` | TCP tier: `balance source`, `send-proxy-v2`, checks L7 `:8404/healthz` |
| `haproxy/l7.cfg` | HTTP tier: TLS, `accept-proxy`, routing, headers, stick-table rate limit, `/readyz` checks, `server-template` + Docker DNS, sticky cookie (commented) |
| `haproxy/local-native.cfg` | No-Docker demo LB: `:8088` → `127.0.0.1:8001-8003` |
| `nginx/web.conf` | SPA server: `/assets/` cached forever, everything else falls back to `index.html`, `/nginx-health` |
| `api.Dockerfile` | Multi-stage, non-root (uid 10001), `uvicorn --no-proxy-headers --workers $WEB_CONCURRENCY`; also used for the worker (compose disables the HTTP healthcheck there and gives it `stop_grace_period: 60s` to finish a running job) |
| `scripts/gen-dev-cert.sh` | Self-signed `localhost` cert → `certs/dev.pem` (gitignored) |
| `scripts/lb-native.sh` | `make lb-native`: 3 replicas + native HAProxy, round robin, then a graceful drain |
| `scripts/check-configs.sh` | Offline checks: `haproxy -c` on all three configs (l7 with a throwaway cert), plus the client-IP trust rules above |

**Frontend build for `web`:** run `npm run build` in the Lovable repo, then either copy `dist/` to `<repo>/frontend-dist/` or `export WEB_DIST=/abs/path/to/dist` before `make up`. If the frontend is hosted on Lovable instead, `web` is only needed for a single-origin demo. The API's CORS setting already allows `*.lovable.app`.

**Validating configs without Docker:** `infra/scripts/check-configs.sh`. It needs `haproxy` and `openssl` on PATH. Set `PYTHON=` to an interpreter with PyYAML to also check that the api port is not published. It runs `haproxy -c` on all three configs. `l7.cfg` is checked as a copy whose cert path points at a throwaway cert, because the real path only exists inside the container. The `resolvers docker` section (127.0.0.11) passes `-c` as is, because `init-addr none` delays resolution to runtime.

## Production on a free tier

### Primary: Oracle Cloud Always Free
Verify the current Always Free limits before relying on them. As of writing they include 1 Flexible LB (10 Mbps), 1 Network LB, 2 AMD micro VMs and Ampere A1 capacity of up to 4 OCPU / 24 GB in total.

| Our tier | OCI resource |
|---|---|
| `l4` | **Network Load Balancer** (L3/L4, public IP 203.0.113.10). Listeners TCP 80/443 → backend set = the Flexible LB's private IP, or the VMs directly if you skip L7. Enable *preserve source IP* or PROXY v2, whichever is available. |
| `l7` | **Flexible Load Balancer** (HTTP/HTTPS) in a public subnet. TLS certificate on the HTTPS listener. Path route rules `/api/*` → `api` backend set (ports 8000 on each VM), default → `web` backend set (8080). Health check HTTP `GET /readyz`, expect 200. Session persistence: disabled. |
| `api`, `worker`, `web` | 2 Ampere A1 VMs (e.g. 2 OCPU / 12 GB each) in **private subnet 10.0.1.0/24**. Each runs `docker compose up -d api worker web redis` without the l4, l7 and db services. The compose file only `expose`s 8000 and 8080, so publish them on the VM with a compose override file (`ports: ["8000:8000"]` on `api`, `["8080:8080"]` on `web`, with one replica of each, since two replicas can't share a fixed host port); `docker compose -p` sets the project name, it is not a port flag. Put Redis on one VM, or use a managed Redis free tier (e.g. Upstash), and point both VMs' `REDIS_URL` at it. |
| Postgres | A managed Postgres 16+ with pg_trgm and pgvector (transaction pooler URI in `DATABASE_URL`), or the compose `db` service on one VM |
| Firewall | **Security list / NSG:** the private subnet allows ingress only from the LB subnet on 8000/8080, plus SSH from a bastion or your IP. There is no public IP on the VMs. Egress goes through a NAT gateway (OpenAI, Bright Data, a managed Postgres if used). |

Scaling there: add a VM, then add it to both backend sets. Each VM can also run `--scale api=2` on two published ports.

### Fallback: Google Cloud Run or Render
Push `infra/api.Dockerfile` (Cloud Run: `gcloud run deploy --source .`, Render: Docker web service). Their managed L7 front ends do TLS, routing and autoscaling of stateless containers, which fits this API. Set `WEB_CONCURRENCY=1` and let the platform scale instances. The trade-offs: you get no L4 control (no PROXY protocol or source hashing, and the client IP comes from `X-Forwarded-For`: keep `TRUSTED_PROXY_HOPS=1` if the platform appends exactly one hop), and cold starts. Run the worker as a second service (Render background worker, or Cloud Run with min-instances=1).

### Quick public URL during the hackathon
```bash
cloudflared tunnel --url https://localhost:443 --no-tls-verify     # full LB stack (http://localhost:80 only 301s to https)
cloudflared tunnel --url http://localhost:8000                     # API only (make api-dev), or :8088 (make lb-native)
```
The tunnel prints an `https://<random>.trycloudflare.com` URL. Set the frontend's API base to it. No account or DNS needed.

## What was verified on the dev machine
This machine cannot run containers (user namespaces are blocked), so `docker compose up` itself has **not** been run. What has been verified:
- `docker compose config -q` passed on an earlier revision. It has not been re-run since, because this machine has no Docker, and `docker-compose.yml` has changed since then (`TRUSTED_PROXY_HOPS`). The current compose file passes `PYTHON=backend/api/.venv/bin/python infra/scripts/check-configs.sh`, which parses it with PyYAML. The system `python3` here has no PyYAML, so without `PYTHON=` the port check is skipped. `haproxy -c` (HAProxy 3.4) passes on all three configs.
- **Native end-to-end with the real `l4.cfg` and `l7.cfg`**, with Docker DNS swapped for static `127.0.0.1` servers. Curl → L4 (tcp, `send-proxy-v2`) → L7 (`accept-proxy`, TLS, HTTP/2) → 3 uvicorn replicas showed:
  - round robin `api-1 → api-2 → api-3`
  - `X-Request-Id` generated, or the client's value kept
  - `/readyz` blocked from outside with a JSON 404
  - port 80 redirected 301 to https
  - a burst of 230 requests from one client got 429s with the ApiError body after about 200
- `make lb-native`: round robin, plus a drain on SIGTERM with no failed requests.
