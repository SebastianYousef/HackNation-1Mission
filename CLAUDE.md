# CLAUDE.md — Rare Disease Atlas (backend + contract repo)

Hack-Nation 7th Global AI Hackathon, Challenge 05: **AI Atlas for the World's Rare Diseases** (`docs/brief.pdf`). We are building an evidence-cited knowledge graph that takes a patient-group leader from her disease to a supported connection, an existing asset, a collaborator and a concrete next step. 24 hours, 4 people: 2 backend (this repo) and 2 frontend (separate Lovable repo).

Read before non-trivial work: `docs/PLAN.md` (roles, timeline, scope decisions), `docs/ARCHITECTURE.md` (system design), `contract/README.md` (the bridge).

## Repo map

| Path | What | Owner |
|---|---|---|
| `contract/` | **The bridge.** `atlas.ts` = every API type; `fixtures/` = example responses; change protocol in README | B2 + F2 jointly |
| `backend/db/migrations/` | Postgres schema + `api_*` SQL functions (one per GET endpoint) | B2 (B1 for tables) |
| `backend/api/` | FastAPI service: REST `/api/v1/*`, stateless, Redis cache/jobs, OpenAI explain/draft, worker | B2 |
| `backend/pipeline/` | Offline ETL: ingest → extract (OpenAI) → reconcile → analytics → views → load | B1 |
| `backend/scripts/check_contract.py` | Verifies a running API against the contract | B2 |
| `infra/` | Docker Compose stack: L4 HAProxy → L7 HAProxy ×2 → API ×N, worker, web (nginx), Redis, Postgres | B2 |
| `frontend/` | Not code: the frontend's half of the bridge (`LOVABLE.md`, `CLAUDE.md`, playbook) synced into the Lovable repo | F1 + F2 |
| `scripts/sync-contract.sh` | Copies contract + fixtures + Lovable context into the frontend repo | anyone |

## Non-negotiable rules
1. **The contract is law.** API responses must match `contract/atlas.ts` exactly. Nullable fields are present as `null`; arrays are never null; unknown id → 404 with the `ApiError` body. Changing a typed field = contract PR approved by one backend and one frontend person (`contract/README.md`). Prefer additive changes plus a minor version bump in both `atlas.ts` and `api_meta()`.
2. **Every edge has evidence.** Never insert an edge without ≥ 1 `evidence` row. Status must be honest: `curated` only for curated databases, `literature` for text-extracted claims (with a verbatim quote), `inferred` for our computations, `hypothesis` for anything speculative. LLM output is never `curated`.
3. **The LLM rephrases; it never invents.** Explanations, views and outreach drafts may only use evidence passed into the prompt, and every sentence cites an `edge_id`. Validate cited ids against the input.
4. **The API is stateless.** No in-process state that must survive a request (no sessions, no in-memory jobs in prod). Shared state goes to Postgres (durable) or Redis (ephemeral). This is what makes horizontal scaling and sticky-session-free load balancing work.
5. **Secrets only in `.env`** (gitignored): `OPENAI_API_KEY`, `BRIGHTDATA_API_KEY`, `DATABASE_URL`, `NCBI_API_KEY`. Never in code, fixtures, logs or commits. The frontend never receives any secret.
6. **The frontend is not in this repo.** Don't generate React code here; change `frontend/LOVABLE.md` if the frontend's instructions must change, then run `scripts/sync-contract.sh`.
7. **Fixtures stay valid.** If you change the contract, update `contract/fixtures/` and run `node contract/scripts/validate-fixtures.mjs`.

## Common commands
Targets live in the root `Makefile`; check `make help` for the current list.
```bash
make api-dev                         # API on :8000 in DATA_MODE=fixtures (hot reload)
make api-test                        # pytest for the API
make contract-check BASE_URL=http://localhost:8000
node contract/scripts/validate-fixtures.mjs
make db-migrate DATABASE_URL=...     # apply backend/db/migrations in order
make up / make down / make scale N=5 # full LB stack via docker compose (infra/)
make lb-demo                         # curl the LB repeatedly, print X-Served-By
cd backend/pipeline && make all      # rebuild the dataset (see backend/pipeline/README.md)
```

## Conventions
- **Ids** are CURIEs (`MONDO:0016295`, `HGNC:2073`, `HP:0001250`, `PMID:…`, `NCT…`, `ORG:<slug>`, `PERSON:<slug>`, `ASSET:<slug>`, `ATLAS:mech-<slug>`). Edge id = `E:` + sha1(`type|src|dst`)[:16]. Use the helpers in `backend/pipeline` rather than hand-building ids.
- **SQL:** every API read is a `stable` SQL function `api_<name>(p_…)` returning `jsonb`. JSON shapes are built only in the `_node_brief/_node_full/_edge_json/_evidence_json` helpers. New migrations get a new timestamped file; never edit an applied migration after the hour-2 freeze.
- **Python:** 3.13, type hints, pydantic models mirroring the contract, `psycopg` 3 with `prepare_threshold=None` (Supabase transaction pooler).
- **Commits:** small, prefixed `api:`, `pipeline:`, `infra:`, `db:`, `contract:`, `docs:`.
- **Before saying something works:** run it. For API changes, run `make api-test` and `make contract-check`. For pipeline changes, load into a scratch DB and query the affected `api_*` function.

## Demo-critical facts
- Slice: lysosomal storage diseases, deep on the NCLs (Batten). The hero is an NCL subtype without approved therapy, picked at hour 1 (fixtures use CLN5). The counterexample is CLN2 enzyme replacement (soluble enzyme) vs. membrane-protein subtypes.
- The demo journey and the 1-minute script are in `docs/PLAN.md`. Every edge on the demo path must be checked by a human before the data freeze (hour 16).
