# CLAUDE.md — backend (B1 Data & Graph, B2 Platform & Services)

Read the root `CLAUDE.md` first; its rules apply here. This file covers the backend specifics.

## Ownership
- **B1:** `backend/pipeline/`. Start with `backend/pipeline/README.md`, including its "Known data gaps" list.
- **B2:** `backend/api/`, `backend/db/`, `infra/`, `backend/scripts/`. Start with `backend/api/README.md` and `infra/README.md`.
- Both: the contract. Any change to an `api_*` output or a view payload is a contract change.

## How data reaches the user
`pipeline` writes `nodes / node_names / edges / evidence / clusters / cluster_members / paths / views / dataset_meta`. Then `api_*` SQL functions build the JSON. The FastAPI route returns it unchanged, with caching keyed on `dataset_meta.dataset.version`. The L7 and L4 load balancers sit in front of that, and the browser at the end.

- Composite screens (`ActionView`, `MechanismView`) are **precomputed** in `pipeline/views.py` and stored in `views`. To change what Maria sees, change `views.py`, not the API.
- Generic graph reads (search, node, neighborhood, edge, similar, paths, clusters) are **live SQL**. To change them, add a new migration with `create or replace function`, then run the SQL check below.
- Every `load` writes a new dataset version, which invalidates API caches automatically.

## Verify before claiming it works
```bash
# scratch Postgres (no Docker on secureblue; Homebrew postgresql@17 + pgvector)
PGB=$(brew --prefix postgresql@17)/bin
$PGB/initdb -D /tmp/atlas-db -U postgres -A trust && mkdir -p /tmp/atlaspg
$PGB/pg_ctl -D /tmp/atlas-db -o "-p 55432 -k /tmp/atlaspg -c listen_addresses=''" -l /tmp/atlas-db.log start
export DATABASE_URL="postgresql://postgres@/postgres?host=/tmp/atlaspg&port=55432"
make db-migrate DATABASE_URL=$DATABASE_URL            # from repo root
(cd backend/pipeline && make analytics views load)    # needs ingested data/
make api-dev DATA_MODE=db DATABASE_URL=$DATABASE_URL  # API on :8000
make contract-check BASE_URL=http://localhost:8000    # must print "OK: responses match contract"
make api-test                                         # fixtures-mode test suite
node contract/scripts/validate-fixtures.mjs           # after touching contract/ or fixtures
```

## Gotchas
- **Postgres connections:** no deployment uses Supabase. Keep `prepare_threshold=None` (psycopg) so a transaction pooler (PgBouncer, Supavisor) stays usable. Budget replicas × pool size ≤ `max_connections`, or the pooler limit if one is in front.
- **Containers can't run on this dev machine** (user namespaces blocked). Use `make lb-native` for the load-balancer demo. `docker compose` works on the team's other machines and the cloud VMs.
- **OpenAI:** all calls go through `pipeline/atlas_pipeline/llm.py` or `api/atlas_api/ai.py`. Never add a call elsewhere. Cache everything. A missing key must degrade to templates (pipeline) or a 503 `upstream_unavailable` (API), never crash.
- **Bright Data:** request code is isolated in `pipeline/.../ingest/brightdata_orgs.py` and `api/atlas_api/jobs.py` (`brightdata_serp`). Both request formats were verified against the live API on 2026-10-03 (see their docstrings). `BRIGHTDATA_SERP_ZONE` is `serp_api1` in `.env.example`; when it is unset the API falls back to `serp_api1` but the pipeline to `mcp_unlocker`, so set it explicitly to a zone you have.
- **Statuses:** analytics output is `inferred`, LLM extraction is `literature` (or `hypothesis`), and only curated databases are `curated`. `graph.py` recomputes confidences from status + evidence, so don't hand-set them.
- **Ids:** use `pipeline/atlas_pipeline/ids.py` (`edge_id`, `evidence_id`, `path_id`, `org_id`, `mech_id`, …). Edge ids must be identical across stages for merging to work.
