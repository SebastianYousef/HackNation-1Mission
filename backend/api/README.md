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

Fixture-mode details: `search.json` maps query → hits. For an unknown query, the API filters all hits by substring (id, label, matched_name) and dedupes them. Neighborhood filters (`statuses`, `edge_types`, `min_confidence`) are applied to the static fixture. `similar` and `paths` honour `limit`, `to` and `kind`.

## Behaviour
- **Headers on every response:** `X-Request-Id` (an incoming one is propagated, otherwise generated) and `X-Served-By` (`INSTANCE_ID`, default hostname).
- **Errors:** always `{"error":{"code","message","request_id"}}`. Codes are `not_found` 404, `bad_request` 400/422, `rate_limited` 429, `upstream_unavailable` 502/503 (DB down, OpenAI missing or failing) and `internal` 500.
- **GET cache:** keyed by dataset version + URL in Redis (TTL `CACHE_TTL_SECONDS`=300). Responses carry `X-Cache: HIT|MISS`, `Cache-Control: public, max-age=60` and a weak `ETag` from the dataset version, so `If-None-Match` gets a 304. Without `REDIS_URL` the API falls back to an in-process TTL cache.
- **Ids** contain `:`. Clients should `encodeURIComponent` them, and routes use `{id:path}`, so `%3A` and `%2F` both work.
- **`search`:** a `q` shorter than 2 characters returns `[]` (it does not raise an error). `limit` is clamped to 1..50.
- **AI** (`POST /explain`, `/outreach-draft`):
  - Uses OpenAI Structured Outputs (`OPENAI_MODEL`).
  - The prompt contains only the edges and evidence loaded through `api_edge` for the requested `edge_ids`.
  - The server drops any step that cites an edge outside the request, and fills `status` and `confidence` from the DB.
  - Explanations are cached in the `explanations` table under `sha1(audience|edge_ids)`.
  - Rate limit: `AI_RATE_LIMIT_PER_MINUTE` (10) per client IP, counted in Redis.
  - Without `OPENAI_API_KEY` these endpoints return 503.
- **Jobs:**
  - `POST /gap-search` returns 202 `{job_id}` and pushes the job onto the Redis list `atlas:jobs`.
  - `python -m atlas_api.worker` takes it with BRPOP, calls the Bright Data SERP API (`jobs.brightdata_serp`, whose request format still needs verifying, see TODO), and stores the `JobStatus` at `job:<id>` (TTL 1 h).
  - Without Redis the job runs in-process (dev only).
- **Ops:** `GET /healthz` checks that the process is alive. `GET /readyz` checks DB, Redis and draining state, and returns 503 when anything fails.
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
