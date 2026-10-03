# atlas_api: the Rare Disease Atlas REST API

A stateless FastAPI service that serves `contract/atlas.ts` v1 under `/api/v1`. Each GET endpoint maps 1:1 to one `api_*` SQL function in `backend/db/migrations/` and returns that function's jsonb as is. When the function returns NULL, the API answers 404.

```bash
make api-dev                      # :8000, DATA_MODE=fixtures, hot reload (creates backend/api/.venv)
make api-test                     # pytest, fixtures mode
make contract-check BASE_URL=http://localhost:8000 ARGS=--allow-missing   # fixtures (partial ids)
make contract-check BASE_URL=http://localhost:8000                        # db mode: strict
make worker                       # job worker (needs REDIS_URL)
```
Settings come from env vars or `backend/.env`. See `backend/.env.example` for the full list.

## Data modes
| `DATA_MODE` | GET endpoints read | AI / jobs / submissions |
|---|---|---|
| `fixtures` | `contract/fixtures/` (ids: `:` → `_`, e.g. `nodes/MONDO_0016295.json`) | canned `explain.json`, `outreach-draft.json`, `gap-search-job.json`; submissions accepted but not stored |
| `db` | `select api_*(...)` via psycopg pool (`prepare_threshold=None` for the Supabase transaction pooler) | live OpenAI / Bright Data / insert |

To switch one endpoint at a time, keep `DATA_MODE=fixtures` and set `DB_ENDPOINTS=search,node,neighborhood` (names: `meta search node neighborhood edge similar paths clusters cluster action_view mechanism_view explain outreach gap_search submissions`).

Fixture-mode details: `search.json` maps query → hits. For an unknown query, the API filters all hits by substring (id, label, matched_name) and dedupes them. Neighborhood filters (`statuses`, `edge_types`, `min_confidence`) are applied to the static fixture, and `max_nodes` keeps the centre plus the best-connected neighbours (with `truncated: true`); `depth` is ignored. `similar` and `paths` honour `limit`, `to` and `kind`. A lookup only succeeds when the fixture's own id equals the requested id, so `MONDO_1` or `MONDO%2F1` is a 404, as in db mode.

In mixed mode the ETag / cache key combines the fixture version and the DB `dataset_meta` version, so a pipeline `load` still invalidates caches.

## Behaviour
- **Headers on every response:** `X-Request-Id` (an incoming one is propagated, otherwise generated) and `X-Served-By` (`INSTANCE_ID`, default hostname).
- **Errors:** always `{"error":{"code","message","request_id"}}`. Codes are `not_found` 404, `bad_request` 400/413/422, `rate_limited` 429, `upstream_unavailable` 502/503 (DB or job queue down, OpenAI missing, failing or timing out) and `internal` 500. 500s carry CORS headers too. Ids longer than 200 characters or containing a NUL byte are 404 (path) or 400/422 (body/query).
- **Request bodies** over `MAX_BODY_BYTES` (64 KiB) get a 413 before they are parsed, chunked uploads included.
- **GET cache:** keyed by dataset version + path + parsed, sorted query params in Redis (TTL `CACHE_TTL_SECONDS`=300). Responses carry `X-Cache: HIT|MISS`, `Cache-Control: public, max-age=60` and a weak `ETag` from the dataset version, so `If-None-Match` gets a 304. Without `REDIS_URL` the API falls back to an in-process TTL cache.
- **Ids** contain `:`. Clients should `encodeURIComponent` them, and routes use `{id:path}`, so `%3A` and `%2F` both work.
- **`search`:** a `q` shorter than 2 characters returns `[]` (it does not raise an error). `limit` is clamped to 1..50.
- **AI** (`POST /explain`, `/outreach-draft`):
  - Uses OpenAI Structured Outputs (`OPENAI_MODEL`).
  - The prompt contains only the edges and evidence loaded through `api_edge` for the requested `edge_ids`, fenced in an `<evidence>` block the model is told to treat as data, never instructions (quotes and scraped page text are untrusted).
  - Explain: the server drops any step that cites an edge outside the request and fills `status` and `confidence` from the DB. No valid step left → 502. Only explanations with a step for every requested edge are cached (`explanations` table, key `sha1(audience|edge_ids)`).
  - Outreach: citations must name an input edge. A sentence whose `[n]` markers all lack a valid citation is dropped, dangling markers are stripped, and `citations` lists only numbers the body still shows. No grounded marker left → 502. Marker-free sentences (greeting, ask, sign-off) are kept.
  - URLs and e-mail addresses the model writes are replaced with `[link removed]` unless they appear in the prompt input.
  - Budget: OpenAI client timeout 12 s, 1 retry, hard deadline 24 s (below the LB's 30 s), timeout → 503.
  - Rate limit: `AI_RATE_LIMIT_PER_MINUTE` (10) per client IP, counted in Redis. The client IP is the X-Forwarded-For entry `TRUSTED_PROXY_HOPS` (default 1) from the right, i.e. the address our own L7 appended; client-supplied entries to its left are ignored. `TRUSTED_PROXY_HOPS=0` ignores XFF and uses the socket peer (API exposed directly). The same limit (separate buckets) applies to `/gap-search` and `/submissions`.
  - Without `OPENAI_API_KEY` these endpoints return 503.
- **Jobs:**
  - `POST /gap-search` returns 202 `{job_id}` and pushes the job onto the Redis list `atlas:jobs` (503 if Redis is down).
  - `python -m atlas_api.worker` takes it with BRPOP, runs three Google searches through Bright Data (`jobs.brightdata_serp`: `POST https://api.brightdata.com/request {zone, url, format:"raw"}` with `brd_json=1`; verified live with a Web Unlocker zone, see the docstring), and stores the `JobStatus` at `job:<id>` (TTL `JOB_TTL_SECONDS`, 1 h; `running` only 120 s, so a job whose worker died expires instead of spinning). Leads are unverified web results and always carry the disclaimer.
  - The whole search is capped at 50 s, inside the worker's 60 s `stop_grace_period`, so a SIGTERMed worker finishes its job. If some queries fail the others' leads are kept; if all fail, or Bright Data reports an error (it answers HTTP 200 with `x-brd-err-code`/`x-brd-err-msg` headers and an empty body), the job is `failed` with a readable `error`. A malformed payload marks its job `failed` and never stops the worker.
  - Gap search runs for real only when `gap_search` is DB-backed (`DATA_MODE=db` or `DB_ENDPOINTS` includes `gap_search`); the disease label comes from the `node` endpoint, so in mixed mode add `node` too unless the disease has a fixture. Otherwise `GET /jobs/{id}` serves `gap-search-job.json`.
  - Without Redis the job runs in-process (dev only).
- **Ops:** `GET /healthz` checks that the process is alive. `GET /readyz` returns 503 only when draining or when the DB (or, in fixtures mode, the fixtures dir) is unavailable. Redis is shared by every replica and the API fails open without it, so a Redis outage is reported as `degraded: {"redis": true}` with status 200 rather than pulling the whole pool out of the LB.
- **Shutdown:** on SIGTERM, `/readyz` switches to 503 for `SHUTDOWN_GRACE_SECONDS`, so the LB marks the replica DOWN. Uvicorn then stops gracefully and finishes in-flight requests.
- **CORS:** allowed origins are `CORS_ORIGINS` (a comma list where `https://*.lovable.app` style wildcards work) plus localhost on any port. `X-Request-Id`, `X-Served-By`, `X-Cache` and `ETag` are exposed to the browser.
- **OpenAPI docs:** `/api/docs`.

## Layout
```
atlas_api/config.py   settings          data.py    fixtures + Postgres (one method per endpoint)
atlas_api/routes.py   /api/v1 routes    cache.py   Redis / in-process store (cache, counters, jobs)
atlas_api/ai.py       explain/outreach  jobs.py    gap-search + Bright Data     worker.py  queue consumer
atlas_api/errors.py   ApiError body     main.py    app factory, middleware, healthz/readyz, drain hook
```
