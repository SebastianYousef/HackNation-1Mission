# Data pipeline (`atlas_pipeline`)

Builds the evidence-cited graph offline and loads it into Postgres in one validated transaction.

```
ingest ─► extract (OpenAI) ─► reconcile ─► analytics ─► views ─► load ─► export-fixtures
```

```bash
cd backend/pipeline
make venv                      # Python 3.13 venv with deps (igraph + leidenalg for Leiden)
cp ../.env.example .env        # DATABASE_URL, OPENAI_API_KEY, NCBI_API_KEY, BRIGHTDATA_API_KEY …
make all                       # or stage by stage: make ingest | extract | reconcile | analytics | views | dry-run | load
ATLAS_API_BASE=http://localhost:8000 make snapshot   # real responses -> data/snapshot (offline demo fallback)
```

Everything under `data/` (raw downloads, interim jsonl/parquet, LLM cache, snapshot) is gitignored and reproducible. The slice is defined in `config/slice.yaml`; hand-curated facts with source URLs (patient groups, approved therapies) are in `config/curated.yaml`.

## Stages

| Stage | Module | Output (`data/interim/`) | Notes |
|---|---|---|---|
| ingest | `ingest/{mondo,hgnc,hpo,orphanet,reactome,clinvar,curated}` | `<source>.{nodes,edges,evidence}.jsonl` + parquet tables | static dumps, cached downloads |
| ingest | `ingest/{pubmed,clinicaltrials,nih_reporter}` | same + `coverage_<source>.json` | live APIs, focus diseases only; coverage feeds the "what we searched" UI |
| ingest | `ingest/brightdata_orgs` | same | **needs `BRIGHTDATA_API_KEY`**; request format marked `TODO(verify)` |
| extract | `extract.py` → `llm.py` | `pubmed_claims…` | OpenAI Structured Outputs; **claims whose quote is not an exact substring of the abstract are dropped**; `--batch` uses the Batch API |
| reconcile | `reconcile.py` → `llm.py` | `reconcile.*` | xref → exact/synonym → LLM picks among ≤ 5 candidates (or none) |
| analytics | `analytics/` | `mechanisms.*`, `analytics.*`, `clusters.jsonl`, `cluster_members.jsonl`, `overlap.jsonl`, `paths.jsonl` | see below |
| views | `views.py` | `views.jsonl` | ActionView for every focus disease, MechanismView for shared mechanisms; LLM only rephrases the headline (template fallback) |
| load | `load.py` | Postgres | invariants checked first (evidence on every edge, enums, endpoints, path connectivity, view keys = contract); truncate + COPY in one transaction; unique dataset version per load (invalidates API caches) |
| export-fixtures | `export_fixtures.py` | `data/snapshot/` | contract/fixtures layout; copy into the frontend's `src/mocks/fixtures` for an offline demo on real data |

**Analytics** (`analytics/`):
- `mechanisms`: disease → gene → Reactome pathway propagation, ignoring pathways with more than 200 genes.
- `ic`: phenotype information content.
- `similarity`: IC-weighted phenotype Jaccard ×0.6 + mechanism Jaccard ×0.25 + gene Jaccard ×0.15. Keeps the top 10 per disease, and flags **counterexamples** (phenotype ≥ 0.35, mechanism ≤ 0.1) with `attrs.caution`.
- `clusters`: Leiden communities.
- `overlap`: people bridging clusters.
- `paths`: Dijkstra over −ln(confidence). Single-symptom bridges and hub symptoms are penalised, and routes never pass *through* a group, person, trial or asset.

## Status (first real run, 2026-10-03)
Real data for the lysosomal slice:
- 3,426 nodes (195 diseases, 115 genes, 1,615 phenotypes, 346 pathways, 659 people, 249 publications, 56 trials, 77 grants)
- 11,119 edges: 7,823 curated, 1,708 literature, 1,588 inferred
- 548 similarity pairs, 15 Leiden clusters, 61 bridging people, 533 paths, 39 action views + 19 mechanism views

`check_contract.py` passed against the API serving this data (300 requests, 0 mismatches). The CLN3 vs. CLN2/CLN1 counterexamples are found automatically.

**Not yet run:** `extract` / `reconcile` with an OpenAI key, and `brightdata_orgs` with a Bright Data key.

**Known data gaps to work on (B1):**
- Few patient groups and no assets yet; add them via `curated.yaml` and Bright Data.
- Reactome barely covers the CLN genes. Add curated `ATLAS:mech-*` mechanisms such as lysosomal protein degradation.
- `plain_summary` is empty for most nodes.
