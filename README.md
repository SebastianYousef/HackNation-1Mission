# Atlas — an AI map for the world's rare diseases

**Hack-Nation 7th Global AI Hackathon · Challenge 05** (OpenAI × Buffalo Initiative)

A patient-group leader types her child's disease into one search box. The Atlas follows it through an **evidence-cited knowledge graph**: shared mechanism → related disease → the patient group working on it → an existing registry or study → a concrete, sourced next step. Every connection shows where it comes from, how sure we are, and what contradicts it. When no supported route exists, the Atlas says so and names the question to test next.

## How it works

```
OFFLINE PIPELINE                                 ONLINE
MONDO · HPO · HGNC · Orphanet · ClinVar ·        Browser ─► L4 LB (TCP) ─► L7 LB ×2 (HTTP/TLS) ─► API ×N (stateless)
Reactome · PubMed · ClinicalTrials.gov ·                                                         │
NIH RePORTER · Bright Data (patient orgs)                                          Postgres ◄────┤────► Redis
   │  OpenAI: extract claims · reconcile names                                                   └────► OpenAI (explain)
   │  analytics: IC-weighted phenotype similarity,                                 worker ◄── queue ──► Bright Data
   │  mechanism overlap, counterexamples, Leiden
   ▼  clusters, explainable paths
 Postgres (nodes · edges · evidence · clusters · paths · views)
```

- **Graph:** 12 node types and 23 typed edges. Every edge carries a **status** (curated / literature / inferred / hypothesis), a confidence, and evidence rows with verbatim quotes and sources, including contradicting ones.
- **AI (OpenAI):** extracts claims from abstracts with quotes, reconciles names to stable ids, and explains paths in plain language where every sentence cites an edge.
- **Platform:** stateless API replicas behind a two-layer load balancer with health checks and draining. State lives in Postgres/Redis, so the system scales horizontally without sticky sessions.
- **Frontend:** a React app in a separate repo. It talks only to the REST contract in `contract/`.

Details: [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) · team plan: [`docs/PLAN.md`](docs/PLAN.md) · API contract: [`contract/README.md`](contract/README.md)

## Run it locally

```bash
cp backend/.env.example backend/.env       # add OPENAI_API_KEY etc. (optional for browsing)
make up                                    # L4 → L7×2 → API×3 + worker + web + Redis + Postgres
make lb-demo                               # watch requests spread across replicas (X-Served-By)
make scale N=5                             # horizontal scaling
make down                                  # stop the stack
```
`make up` needs Docker Compose. The bundled Postgres applies `backend/db/migrations` on first start. The API serves the mock fixtures until you set `DATA_MODE=db` in `backend/.env` and load the dataset (next section). The `web` tier serves a built frontend from `frontend-dist/` (or `WEB_DIST`), see [`infra/README.md`](infra/README.md). `make help` lists all targets.

API only, on mock data: `make api-dev` → http://localhost:8000/api/v1/meta

## Reproduce the dataset

```bash
cd backend/pipeline
cp ../.env.example .env                    # set DATABASE_URL, e.g. postgresql://atlas:atlas@localhost:5432/atlas for `make up`
make all                                   # ingest → extract → reconcile → analytics → views → load
```
`make all` loads into the Postgres at `DATABASE_URL`, which must already have the schema (`make up` does that, or `make db-migrate DATABASE_URL=...` from the repo root). The API keys are optional: without `OPENAI_API_KEY`, extract and reconcile use only cached results, and without Bright Data credentials that stage is skipped. Downloads are cached in `backend/pipeline/data/`. See [`backend/pipeline/README.md`](backend/pipeline/README.md) for sources, environment variables and the stage outputs.

## Offline demo (`?mock=1`)

The team frontend app (its own repo) has a mock client: `?mock=1` on any route serves the fixtures bundled into the build from `src/mocks/fixtures`, so it needs no API. By default those are the mock fixtures in `contract/fixtures` (copied by `scripts/sync-contract.sh`). For the demo on real data, run `ATLAS_API_BASE=http://localhost:8000 make snapshot` in `backend/pipeline` and copy `data/snapshot/` into the frontend's `src/mocks/fixtures` (run `sync-contract.sh` first, since it replaces that folder). The landing page `frontend/landing.html` has no mock mode; it reads the live API and says so when the API is not reachable.

## Repository layout

| Path | Contents |
|---|---|
| `contract/` | API contract (TypeScript types), fixtures, change protocol |
| `backend/db/` | Postgres schema and SQL API functions |
| `backend/api/` | FastAPI service and worker |
| `backend/pipeline/` | Data pipeline |
| `infra/` | Load balancers, compose stack, deployment notes |
| `frontend/` | Frontend spec, playbook and landing page (the frontend code lives in its own repo) |
| `docs/` | Brief, architecture, plan |

## Open work

We track all open work in [GitHub issues](https://github.com/SebastianYousef/HackNation-1Mission/issues), labelled `frontend`, `backend` or `submission`. Found something that needs doing? Open an issue rather than leaving a TODO in the code or docs. Reference it in your commits (`Fixes #N`).

*Atlas organizes published research to help communities find each other. It is not medical advice.*
