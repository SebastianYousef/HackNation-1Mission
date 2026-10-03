# Data pipeline (`atlas_pipeline`)

Builds the evidence-cited graph offline and loads it into Postgres in one validated transaction.

```
ingest ─► extract (OpenAI) ─► reconcile ─► analytics ─► views ─► load ─► export-fixtures
```

```bash
cd backend/pipeline
make venv                      # Python 3.13 venv with deps (igraph + leidenalg for Leiden); reuses an existing .venv
make venv PY=/path/to/python3.13   # if python3.13 is not on PATH (default: python3.13, else python3)
cp ../.env.example .env        # DATABASE_URL, OPENAI_API_KEY, NCBI_API_KEY, BRIGHTDATA_API_KEY …
make all                       # or stage by stage: make ingest | extract | reconcile | analytics | views | dry-run | load
ATLAS_API_BASE=http://localhost:8000 make snapshot   # real responses -> data/snapshot (offline demo fallback)

.venv/bin/pip install -e '.[dev,scrape]'   # pytest + playwright (Scraping Browser)
.venv/bin/python -m pytest -q              # tests/: synthetic data in a temp ATLAS_DATA_DIR, ATLAS_LLM=off, no network
```

`ATLAS_DATA_DIR` moves `data/` (e.g. a scratch copy for experiments: copy `data/interim`, link `data/raw`).

Everything under `data/` (raw downloads, interim jsonl/parquet, LLM cache, snapshot) is gitignored and reproducible. The slice is defined in `config/slice.yaml`; hand-curated facts with source URLs and verbatim quotes (patient groups, mechanisms, assets, therapies) are in `config/curated.yaml`.

## Stages

| Stage | Module | Output (`data/interim/`) | Notes |
|---|---|---|---|
| ingest | `ingest/{mondo,hgnc,hpo,orphanet,reactome,clinvar,curated}` | `<source>.{nodes,edges,evidence}.jsonl` + parquet tables | static dumps, cached downloads |
| ingest | `ingest/{pubmed,clinicaltrials,nih_reporter}` | same + `coverage_<source>.json` | live APIs, focus diseases only; coverage feeds the "what we searched" UI |
| ingest | `ingest/brightdata_orgs` | same + `coverage_brightdata.json` | patient-org / registry **leads**, all `hypothesis`; see "Bright Data" below |
| extract | `extract.py` → `llm.py` | `claims.jsonl` | OpenAI Structured Outputs; `--batch` uses the Batch API. **Quote guardrail:** a claim is dropped unless its quote is found in the abstract or in the title (separately, never across both), has ≥ 4 words and ≥ 20 characters, and names the claim's subject or object; the stored quote is the span copied from the source text, not the LLM's string. Drops are counted in `openai_usage.json` (`claims_dropped_quote_check`) |
| reconcile | `reconcile.py` → `llm.py` | `reconcile.*` | xref → exact/synonym → LLM picks among ≤ 5 candidates (or none) |
| analytics | `analytics/` | `mechanisms.*`, `analytics.*`, `clusters.jsonl`, `cluster_members.jsonl`, `overlap.jsonl`, `paths.jsonl` | see below |
| views | `views.py` | `views.jsonl` | ActionView for every focus disease, MechanismView for shared mechanisms; LLM only rephrases the headline (template fallback) |
| load | `load.py` | Postgres | invariants checked first (evidence on every edge, every `literature` edge has a quoted supporting row, enums, path connectivity, view keys = contract); then **delete + COPY in one transaction** (readers keep the old snapshot until commit; an advisory lock serialises loads; `submissions` is never touched); unique dataset version per load (invalidates API caches) |
| export-fixtures | `export_fixtures.py` | `data/snapshot/` | contract/fixtures layout; copy into the frontend's `src/mocks/fixtures` for an offline demo on real data |

**Merging** (`graph.py`, used by analytics, views and load): nodes and edges merge by id across stages; the edge status is the strongest any stage gave; confidence is recomputed from status + evidence. A `literature` edge with no `supports` evidence row carrying a non-empty quote (web scrapes, `method: scrape:*`, don't count) is **downgraded to `hypothesis`** (logged as `N literature edges have no quoted supporting evidence -> hypothesis`, and `GraphWriter.close` warns per stage) rather than failing the load; `load.validate` re-checks it. Dangling edges, edges without evidence and text-derived edges with no supporting row are dropped.

**Analytics** (`analytics/`):
- `mechanisms`: disease → gene → Reactome pathway propagation, ignoring pathways with more than 200 genes.
- `ic`: phenotype information content.
- `similarity`: IC-weighted phenotype Jaccard ×0.6 + mechanism Jaccard ×0.25 + gene Jaccard ×0.15. Keeps the top 10 per disease, and flags **counterexamples** (phenotype ≥ 0.35, mechanism ≤ 0.1) with `attrs.caution`.
- `clusters`: Leiden communities.
- `overlap`: people bridging clusters.
- `paths`: Dijkstra over −ln(confidence). Single-symptom bridges and hub symptoms are penalised, and routes never pass *through* a group, person, trial or asset.

**Curated facts** (`ingest/curated.py`, `config/curated.yaml`; the YAML schema is at the top of that file):
- Sections: `organizations` (→ `organization_serves_disease`), `mechanisms` (`ATLAS:mech-*` → `gene_in_mechanism`, `disease_involves_mechanism`), `assets` (`ASSET:*`, attrs `asset_kind/access/owner_id` → `organization_maintains_asset`, `asset_covers_disease`, `asset_targets_gene`), `interventions` (→ `intervention_treats_disease` with `attrs.approval`, `intervention_targets_mechanism`).
- Every edge carries an `evidence` list (`quote`, `source_type`, `source_name`, `source_ref`, `url`, `published_at`, `retrieved_at`). Diseases may be MONDO ids or OMIM/ORPHA xrefs. Genes may be HGNC ids or symbols.
- Status: an edge with a verbatim quote is `literature`. Without one it is loaded as `hypothesis` (capped at 0.4, with a warning). Nothing from this file is `curated`. Entries without evidence, or with diseases outside the slice, are skipped and logged.
- `python -m atlas_pipeline.ingest.curated --verify` fetches every evidence url and checks that each quote appears on the page (tags stripped, whitespace collapsed). Run it after adding facts. It needs network, so it is not part of `make ingest`.

**Bright Data** (`ingest/brightdata_orgs.py`; verified against the live API 2026-10-03):
- Search: `POST https://api.brightdata.com/request`, `Authorization: Bearer $BRIGHTDATA_API_KEY`, body `{zone: $BRIGHTDATA_SERP_ZONE, url: "https://www.google.com/search?q=…&hl=en&brd_json=1", format: "raw"}`. A SERP API zone or a Web Unlocker zone (`mcp_unlocker`) both work. Don't send Google's `num` (rejected, `x-brd-serp-warn`), and don't use `OR` in the query (Google drops the quoted phrase). Bright Data answers **HTTP 200 on failure** with the reason in `x-brd-error` / `x-brd-err-code` / `x-brd-err-msg` and an empty body; an unknown zone gives HTTP 400. Each search is sent at most twice (the stage's own loop; the HTTP helper does not retry Bright Data calls), so a failing disease costs at most 2 billed requests; it then gets coverage `result_count: null` and the stage carries on. Successful answers are cached in `data/raw/http_cache/brightdata`.
- Pages: each org-looking result (one per host) is fetched directly, following redirects (cached with the final URL in `http_cache/brightdata_pages`); the final URL decides the host and the curated-org match. The Scraping Browser (`BRIGHTDATA_BROWSER_WSS`, Playwright `connect_over_cdp`) only searches Google for diseases whose API search failed. Re-visiting pages the direct fetch could not read is opt-in (`ATLAS_BRIGHTDATA_BROWSER_VISITS=1`), because Bright Data **blocks sites it classes as "Philanthropy & Non-Profit Organizations"** (`proxy_error`), which covers most patient-org sites, and every session is billed.
- Honesty: a lead is kept only if the disease is named in the result or on the page. Every edge is `hypothesis`, method `scrape:brightdata`, nodes carry `attrs.unverified`. `quote` is only ever text read from the page, and only a sentence about the organisation itself: it names the org (site name, curated label/synonyms, acronym) or speaks as "we/our", or sits on the homepage/about page, and it names no other organisation (a directory line such as "Noah's Hope was founded by …" is not evidence for the directory site). Otherwise `quote` is null and Google's snippet goes to `edge.attrs.search_snippet`. Scraped rows (`method: scrape:*`) never satisfy the literature quote guardrail in `graph.py`/`load.validate`, so they cannot keep a curated edge at `literature`. A lead under a `curated.yaml` organisation's `website` reuses that ORG id (no duplicate node, no asset) and only adds an edge when the page has such a quote. A registry asset needs wording like "join our patient registry".
- Zones' IP allowlists must contain the machine's public IP (not a 100.x Tailscale address). Limit a test run with `ATLAS_BRIGHTDATA_DISEASES=MONDO:…,MONDO:…` and `ATLAS_BRIGHTDATA_PER_DISEASE=3`; a full run is one search per focus disease.

## Status (first real run, 2026-10-03)
Real data for the lysosomal slice. For the current dataset version and counts (nodes by type, edges, evidence, clusters, focus diseases) see `GET /api/v1/meta`; they change with every load, so they are not kept here.

`check_contract.py` passed against the API serving this data (300 requests, 0 mismatches). The CLN3 vs. CLN2/CLN1 counterexamples are found automatically.

**Not yet run:** `extract` / `reconcile` with an OpenAI key. `brightdata_orgs` has only been run for CLN5, CLN3 and CLN2 in a scratch data dir (12 hypothesis edges: 9 on curated orgs with quotes naming the org, 3 on new orgs, two of them quote-less search leads); the shared `data/interim/brightdata_orgs.*` is still empty.

**Known data gaps to work on (B1):**
- Patient groups and assets cover only a few diseases (mostly the NCL subtypes in `curated.yaml`); add more via `curated.yaml` and Bright Data.
- Reactome barely covers the CLN genes. Add curated `ATLAS:mech-*` mechanisms such as lysosomal protein degradation.
- `plain_summary` is empty for most nodes.
