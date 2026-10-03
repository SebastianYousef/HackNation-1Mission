# Architecture: Rare Disease Atlas

> One-line version: an **offline pipeline** builds an evidence-cited knowledge graph into Postgres. **Stateless API replicas behind an L4 → L7 load-balancer chain** serve it through a frozen REST contract to a **Lovable-built React frontend**. The two teams meet only at `contract/`.

## 1. System overview

```
                                   ┌──────────────────────────── OFFLINE (backend, laptops) ─────────────────────────────┐
                                   │ ingest ─► extract (OpenAI) ─► reconcile ─► analytics ─► views ─► load ──────────┐  │
                                   │  MONDO HPO HGNC Orphanet ClinVar Reactome PubMed CT.gov RePORTER Bright Data      │  │
                                   └───────────────────────────────────────────────────────────────────────────────────┼──┘
                                                                                                                        ▼
 Browser ──DNS──► 203.0.113.10 ──► [L4 LB] ──TCP+PROXY v2──► [L7 LB ×2] ──HTTP──► [API ×N] ──SQL──► Postgres
  (React SPA,       public IP       TCP:443      10.0.0.0/24       TLS end,  /api/* ─► 10.0.1.x:8000  │      api_* functions
   from Lovable)                    src-IP hash                    routing   /*     ─► [web] nginx     ├──► Redis (cache, rate
                                                                   health    (static dist/)            │     limits, job queue)
                                                                                                      └──► OpenAI (explain/draft)
                                                                                    [worker ×M] ◄── Redis queue ──► Bright Data SERP
```

| Layer | Owner | Tech | State? |
|---|---|---|---|
| Frontend SPA | Frontend team | Lovable → React, Vite, TS, Tailwind, shadcn, react-query, cytoscape | none (browser) |
| L4 load balancer | Backend (B2) | HAProxy `mode tcp` (prod: cloud Network LB) | none |
| L7 load balancer | Backend (B2) | HAProxy `mode http` ×2 (prod: cloud Flexible/Application LB) | none |
| API replicas | Backend (B2) | FastAPI + uvicorn, psycopg3 pool | **none: stateless** |
| Worker(s) | Backend (B2) | Python, consumes Redis queue | none |
| Cache / queue | Backend (B2) | Redis | ephemeral |
| Database | Backend (B1+B2) | Postgres 16+ with pg_trgm and pgvector (self-hosted; the Supabase free tier was the original plan and is not used) | **all durable state** |
| Pipeline | Backend (B1) | Python 3.13: pandas, networkx/igraph+leidenalg, openai | files in `data/` |

## 2. The request path, layer by layer

What happens when a user opens `https://atlas.example/d/MONDO:0016295`:

1. **DNS (application layer, before any LB).** `atlas.example` → an `A` record pointing at the **one public IP** of the L4 load balancer (e.g. `203.0.113.10`). Nothing else has a public IP.
2. **L3 (IP).** Packets are routed to `203.0.113.10`. Cloud firewall / security list: only TCP 80/443 are allowed in.
3. **L4 LB (TCP).** It sees only the 5-tuple (src IP, src port, dst IP, dst port, protocol), not HTTP.
   - It picks an L7 node with `balance source` (hash of the client IP), so one client keeps hitting the same L7 node. This is cheap L4 affinity that keeps TLS sessions warm.
   - It does **TLS passthrough** (it never decrypts) and prepends a **PROXY protocol v2** header (`send-proxy-v2`), so the L7 layer still knows the real client IP even though the TCP source is now the L4. The L7 binds with `accept-proxy`.
   - It health-checks the L7 nodes with `GET /healthz` on their monitor port 8404 every 2 s (sent without a PROXY header) and removes a dead node after 2 failures, about 4 s.
   - Why have it? It is the scale-out point for the L7 tier: we can run 2..n L7 nodes behind one IP. It is also what cloud providers give you as a "Network Load Balancer".
4. **L7 LB (HTTP).** It terminates TLS (holds the certificate) and parses HTTP. Then it:
   - **Routes by path:** `/api/*` → `api` pool, `/healthz` and `/readyz` are blocked from outside (JSON 404), everything else → `web` pool (static SPA, with fallback to `index.html`).
   - **Balances** the `api` pool with `roundrobin`. Replicas are identical and stateless, and `leastconn` is a one-line swap if slow AI calls ever make the load uneven.
   - **Health-checks** each API replica on `GET /readyz` every 2 s (`fall 2 rise 2 slowstart 10s`). `/readyz` returns 503 only while the replica is **draining** (after SIGTERM, so a deploy drains gracefully) or when its **database** is unreachable. Redis is shared by every replica, and the app fails open without it, so a Redis outage is reported as `200` with `"degraded": {"redis": true}` and does not empty the whole pool.
   - **Sets headers:** it *replaces* `X-Forwarded-For` with exactly one entry, the real client IP from the PROXY header (no appending, so a client-sent value never reaches the API). It also overwrites `X-Forwarded-Proto: https` and adds `X-Request-Id` if absent.
   - **Rate-limits** per client IP with a stick-table (200 requests per 10 s, about 20 req/s overall). The app adds a tighter Redis-backed limit on the AI, gap-search and submission endpoints, which cost money.
   - **Answers its own errors in the contract's shape:** 429 (rate limit), 404 (ops routes), and 502/503/504 (no replica up, reset, or slower than `timeout server 30s`) all come back as `ApiError` JSON with the request id, never as an HAProxy HTML page.
   - **Sticky sessions:** supported (`cookie SERVERID insert indirect nocache`) but **off**, see §3.
5. **API replica (application).** A private IP like `10.0.1.11:8000`, reachable only from the LB subnet.
   - Takes the client IP for rate limits and logs from the `X-Forwarded-For` entry `TRUSTED_PROXY_HOPS` hops from the right (1 behind our L7). Uvicorn runs with `--no-proxy-headers`, so no header can rewrite the peer address or scheme. The app builds no absolute URLs (`redirect_slashes=False`, relative `Location`), so it doesn't need `X-Forwarded-Proto`. Details: `infra/README.md`, "Client IP and forwarded headers".
   - Validates params, checks the Redis cache (`dataset_version + URL`), and on a miss calls one SQL function (`select api_action_view($1)`).
   - Returns JSON with `ETag`, `Cache-Control`, `X-Request-Id` and **`X-Served-By: api-2`**, which makes the load balancing visible in the UI's debug footer.
6. **Postgres.** Reached directly, or through a **transaction pooler** (PgBouncer, Supavisor) so many replicas share a bounded number of DB connections. The API disables server-side prepared statements, so either works.

## 3. Horizontal scaling and why sessions are not sticky

**The API holds no state between requests.** Everything durable lives in Postgres. Everything shared and ephemeral (cache, rate-limit counters, background jobs) lives in Redis. So:
- **Any replica can answer any request.** Scaling out = start another replica. The L7 LB discovers it via DNS service discovery (`server-template`) and starts sending traffic after it passes `/readyz`. Scaling in = mark it not ready, let it drain, stop it.
- **Sticky sessions are unnecessary.** We have no login and no in-memory sessions. Stickiness would only *hurt*: uneven load, and lost "sessions" when a node dies.
- Where affinity *would* make sense, we already cover it differently:

| Need | How we handle it |
|---|---|
| Long AI job (web gap-search) | The job is stored in Redis and any replica can answer `GET /jobs/{id}` polls. No stickiness needed. |
| Cache locality for expensive `/explain` | Optional: L7 `balance uri` + `hash-type consistent` on that one route. Same path → same replica → warm in-process cache. |
| Future login sessions | Signed stateless JWT cookie. If we ever had server sessions, they would go in Redis; L7 cookie stickiness would be a last resort. |

**Scaling limits to watch:**

| Resource | Budget |
|---|---|
| DB connections | replicas × WEB_CONCURRENCY × pool_size ≤ Postgres `max_connections`, or the pooler's client limit if one is in front (use pool_size = 5) |
| OpenAI | per-IP rate limits in Redis; complete explanations cached in Postgres per (audience, edge_ids) until the next data load (`load` clears the `explanations` table); outreach drafts are not cached |
| Bright Data | only in the worker tier; scale workers independently of API replicas |

## 4. Data model (in Postgres, see `backend/db/migrations/`)

Property graph in relational tables:
- `nodes(id CURIE, type, subtype, label, plain_summary, synonyms, xrefs, attrs, embedding)`: 12 node types (disease, gene, variant, phenotype, mechanism, intervention, organization, person, publication, trial, grant, asset).
- `node_names`: every label, synonym, xref and id, with a trigram index → one search box for everything with synonym resolution.
- `edges(type, src, dst, status, confidence, score, attrs, support_count, contradict_count, sources)`: 23 typed relations.
- `evidence(edge_id, stance supports|contradicts|context, source_type, source_name, source_ref, url, quote, method, dates)`: **every edge has ≥ 1 row**, enforced by the loader.
- `clusters`, `cluster_members`, `paths` (precomputed explainable routes), `views` (precomputed ActionView/MechanismView JSON), `explanations` (LLM cache), `submissions` (contributed evidence, quarantined), `dataset_meta`.

### Edge status: the backbone of "evidence integrity"

| status | produced by | default confidence | UI |
|---|---|---|---|
| `curated` | curated DBs: HPO, Orphanet, MONDO, ClinVar, ClinicalTrials.gov, RePORTER | 0.9 | solid slate |
| `literature` | OpenAI extraction from abstracts, with the verbatim quote | 0.5 + 0.1 per independent PMID, max 0.85, −0.15 per contradiction | solid blue |
| `inferred` | our analytics (similarity, clustering) | calibrated score | dashed violet |
| `hypothesis` | atlas-proposed links and speculative claims | ≤ 0.4 | dotted amber |

A path is only as strong as its weakest edge (`weakest_status`, `min_confidence` in `/paths`).

## 5. Pipeline (offline, `backend/pipeline/`)

```
ingest ──► extract ──► reconcile ──► analytics ──► views ──► load ──► export-fixtures
```
- **Ingest** (free APIs and dumps; Bright Data only where no API exists):
  - MONDO: stable disease ids, synonyms, xrefs, hierarchy
  - HPO `phenotype.hpoa` and gene–disease files: phenotypes with frequency, gene associations
  - HGNC: gene synonyms
  - Orphanet: gene–disease, prevalence
  - ClinVar: variants for slice genes
  - Reactome: gene → pathway
  - PubMed E-utilities: abstracts
  - ClinicalTrials.gov API v2: studies
  - NIH RePORTER: grants, PIs
  - **Bright Data:** patient-organization directories (NORD, Global Genes, EURORDIS, Rare Disease UK), org websites (registries, natural history studies, contacts, press releases), and the SERP API for "patient registry for X".
  - OMIM needs a license key, so we reach OMIM-level facts through MONDO/HPO xrefs.
- **Extract (OpenAI, Structured Outputs).** Abstracts become claims (gene, variant effect, mechanism, phenotype, investigator, stance: supports/contradicts), each with a verbatim quote and PMID.
- **Reconcile (OpenAI + rules).** Order of matching: xref → exact or synonym → embedding nearest-neighbour + LLM judge. Every mapping keeps its method and confidence.
- **Analytics:**
  1. Phenotype **information content** (IC = −log p over diseases, propagated up the HPO hierarchy). "Seizure" is common and uninformative; a rare retinal sign is highly informative. This is how we distinguish broad symptoms from unusually informative ones, as the brief asks.
  2. **Disease similarity** = w₁·IC-weighted phenotype similarity + w₂·mechanism overlap (pathways and variant effect) + w₃·shared/related genes. The top-k become `disease_similar_to` edges, `inferred`, with their component scores.
  3. **Counterexamples:** high phenotype similarity with low mechanism similarity gets `attrs.caution`. For example, CLN2 (soluble enzyme, enzyme replacement approved) vs. CLN3 (membrane protein): looks alike, and the therapy does not transfer.
  4. **Leiden clustering** on the similarity graph gives mechanism/phenotype communities, labeled from their top phenotypes and mechanisms.
  5. **Network overlap:** researchers, clinicians and funders linked to diseases in different clusters (the brief's "shared key opinion leader").
  6. **Paths:** k-best routes from each disease to related diseases, patient groups, assets, researchers and trials. Score = Π confidence × hub penalty for high-degree nodes.
- **Views (OpenAI to rephrase only).** ActionView / MechanismView JSON. The LLM may only rephrase evidence it is given (headline, why/differences, next steps). Each output sentence carries edge ids. With no API key, a deterministic template fallback is used.
- **Load.** One transaction with invariant checks (evidence on every edge, valid enums, endpoints exist, view payloads match the contract), then `dataset_meta.version` is bumped. The API cache keys on that version, so a reload invalidates caches automatically.
- **Export-fixtures.** Dumps real responses for the demo ids. The frontend can then run the real demo fully offline (`?mock=1`) as a fallback.

## 6. OpenAI: minimal footprint, maximum visibility (required for the prize track)

The brief: *"If you want to win the challenge track prizes, you need to leverage OpenAI's models or tools"*. It names three verbs: **Extract, Reconcile, Explain**. We cover all three, but only where an LLM is genuinely better than code. Everything else is deterministic. All OpenAI code lives in **one module per side** (`backend/pipeline/.../llm.py`, `backend/api/atlas_api/ai.py`), and model names come from env (`OPENAI_MODEL`).

| # | Brief verb | When | What the LLM does | Volume / cost | Guardrail |
|---|---|---|---|---|---|
| 1 | **Extract** | offline, **once** (Batch API) | abstract → JSON claims `{subject, relation, object, stance, quote}` | ~500–1,500 slice abstracts; cheap model; cached by PMID | Structured Outputs schema; **the quote must be an exact substring of the abstract, or the claim is dropped**; speculative wording → `hypothesis` |
| 2 | **Reconcile** | offline, only leftovers | pick the right id among ≤ 5 candidates for names that xref/synonym matching couldn't resolve | tens to hundreds of names | the LLM may only choose from the given candidates or "none"; logged with method `llm:<model>` |
| 3 | **Explain** | live `POST /explain` (nothing is precomputed) | path edges + their evidence → plain-language steps, each citing one `edge_id` | live calls cached per (audience, edge_ids) until the next data load; rate-limited | only the given evidence goes into the prompt; cited ids are validated against the input; without `OPENAI_API_KEY` it returns 503 `upstream_unavailable` (TODO(decision API-01): a keyless fallback is pending) |
| (opt.) | Explain | offline | `plain_summary`, `headline`, `why`/`differences` in views | a few dozen calls | same rules; template fallback without a key |
| (opt.) | Explain | live `POST /outreach-draft` | evidence → a cited message draft | on demand, not cached; rate-limited | citations validated against the input; 503 without a key, no template fallback |

**Deliberately not LLM:**
- search (trigram + synonyms)
- similarity, clustering, paths and confidence (math, explainable)
- ingestion of curated databases
- embeddings: not needed for the slice. Use `text-embedding-*` only if reconcile leftovers are many.

**Make it visible to judges** (cheap and decisive):
- every LLM-produced evidence row shows "Extracted by GPT from PMID …" in the evidence drawer (`method: llm:<model>`)
- the explanation panel is labeled "Explained by GPT, every sentence cited"
- a "Built with OpenAI" section in the README with these three verbs and counts (claims extracted, names reconciled, explanations served)
- name it in the video

## 7. Frontend / backend separation (the bridge)

- The **only** shared artifact is `contract/`: `atlas.ts` (types + `AtlasApi`), `fixtures/`, `README.md` (endpoints + change protocol).
- Frontend: `AtlasApi` has two implementations, `HttpAtlasApi` and `MockAtlasApi` (fixtures). `?mock=1` toggles. The frontend can therefore finish every screen without the backend.
- Backend: `DATA_MODE=fixtures` serves the same fixtures over real HTTP behind the real LB from hour ~2. Endpoints are then switched to `db` one by one as data lands, and the frontend never notices.
- `backend/scripts/check_contract.py --base-url …` checks a running API against the contract. `contract/scripts/validate-fixtures.mjs` checks the fixtures.
- Separate repos: the frontend lives in the Lovable-owned repo; this repo holds backend + contract + docs. `scripts/sync-contract.sh` copies the bridge across.

## 8. Deployment

> **Live deployment (current):** rootless Docker on the friend's PC (`ssh laqueinux`), public via a Cloudflare quick tunnel, landing page at `/`, API at `/api`. See the root `CLAUDE.md` → "Deployment". The Tailscale-Funnel plan below needed root and was not used.

**Original plan: the team's GPU workstation**, on the tailnet at `100.87.219.50`. The whole compose stack runs there, the internet reaches it through Tailscale Funnel (public TLS edge → L4 edge port → L7 → API replicas), and the GPU runs OpenAI's open-weight gpt-oss for bulk extraction. Everything is isolated in `~/atlas-server`. See [`deploy/gpu-server/README.md`](../deploy/gpu-server/README.md).

The free-tier cloud options below are the fallback, and the reference for a multi-machine production setup.


| Component | Primary (full control, shows the LB layers) | Fallback (simplest) |
|---|---|---|
| L4 LB | Oracle Cloud Always Free **Network Load Balancer** | (none) |
| L7 LB | Oracle Cloud Always Free **Flexible Load Balancer** (10 Mbps), or HAProxy on a VM | Render / Cloud Run built-in L7 |
| API + worker + web + Redis | Oracle Always Free VMs (Ampere A1) in a **private subnet**, running `infra/docker-compose.yml` | Render free web service / Cloud Run |
| Postgres | Managed Postgres free tier with pg_trgm + pgvector (via its pooler), or the compose `db` service | same |
| Public URL during the hackathon | `cloudflared tunnel --url https://localhost:443 --no-tls-verify` (free, instant HTTPS; `http://localhost:80` only 301s to https) | Lovable publish (`*.lovable.app`) + public API |

Verify the current Always Free limits when signing up; Oracle sign-up needs a card, so start that at hour 0. Managed free-tier Postgres projects (e.g. Supabase) may pause after a week of inactivity, so keep one active through judging if you use it. The brief also accepts "easy to run locally": `make up` starts the whole stack (L4 → L7 ×2 → API ×3 → Redis/Postgres → web) with Docker Compose.

Network layout (prod):
```
VCN 10.0.0.0/16
 ├─ public subnet  10.0.0.0/24   NLB (203.0.113.10)  ─►  L7 LB nodes 10.0.0.21, 10.0.0.22
 └─ private subnet 10.0.1.0/24   app VMs 10.0.1.11, 10.0.1.12  (api ×N, worker, web, redis)
     security list: ingress 8000/8080 only from 10.0.0.0/24; egress 443 (OpenAI, Bright Data, managed Postgres if used)
```

## 9. Key decisions and why

| Decision | Why |
|---|---|
| Precompute the graph, paths and composite views offline | The demo must be fast and deterministic; LLM and scrape failures cannot break it |
| Postgres instead of a graph DB | One store for graph + search + vectors + cache tables; small slice; the SQL functions are the data-access layer |
| REST contract, not direct DB access from the browser | Total decoupling; enables the LB/scaling story; secrets stay server-side; fixtures make the frontend independent |
| Stateless API + Redis | Horizontal scaling without sticky sessions; jobs survive replica death |
| Status on every edge + contradicting evidence in the API | Directly targets the "Evidence integrity" and "Graph quality" judging criteria |
| A narrow slice (lysosomal / NCL) done deeply | The 24h ambition is one complete journey; NCL has many genes, one shared mechanism family, an approved therapy for one subtype (a perfect counterexample), and active patient groups |
