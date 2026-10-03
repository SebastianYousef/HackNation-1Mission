# Fixtures — example responses for every endpoint (ALL MOCK DATA)

Every file here is **exactly** the response body of one endpoint in `../atlas.ts` — no wrappers, no extra keys.
The frontend's mock client serves these before the real backend exists.

> **Everything is MOCK.** Disease names, gene symbols (CLN5, CLN6, CLN3, TPP1, PPT1, MFSD8), HPO-style symptom
> labels, cerliponase alfa and BDSRA are real, well-known names. Every identifier (`MONDO:MOCK…`, `HP:MOCK…`,
> `PMID:MOCK…`, `NCTMOCK…`, `ORG:mock-…`, `PERSON:mock-…`), every person ("Dr. A. Example"), quote (`[MOCK] …`),
> number, date and URL (`https://example.org/…`) is a placeholder. "MOCK ultra-rare NCL subtype X" and gene
> `MOCKG1` are fictional. Never show this data as fact.

## The story (Batten / NCL slice)

Hero: **CLN5 disease** (no approved treatment, Maria's group "CLN5 Families Network") → shared mechanism
**Lysosomal degradation** → related **CLN6, CLN2, CLN3** disease, their groups, a shared registry, a CLN6 natural
history study, a CLN5 sheep model → next steps.
Counterexample: **CLN2** has an approved enzyme replacement therapy (cerliponase alfa) because TPP1 is a soluble
enzyme; the CLN5→CLN2 and CLN5→CLN3 similarities carry a `caution`, and the CLN5→"soluble enzyme deficiency" edge
is a `hypothesis` with a contradicting evidence row. Devon's honest-gap case is the fictional **subtype X**
(`exact_groups: []`, `coverage.has_supported_route: false`). Shared person: Dr. A. Example (CLN5 + CLN3).

## File naming

`<safe-id>` = the id with every `:` replaced by `_` (e.g. `MONDO:MOCK0005` → `MONDO_MOCK0005`,
`E:3f2a…` → `E_3f2a…`). Edge ids follow the repo convention `E:` + sha1(`type|src|dst`)[:16].

| File | Endpoint | Type |
|---|---|---|
| `meta.json` | `GET /meta` | `MetaResponse` |
| `search.json` | `GET /search?q=` | **a map `{ [q]: SearchResponse }`** for q = `cln5`, `batten`, `cln`, `lysosom`, `seizure`, `bdsra`, `zzz` (empty). The only file that is not a bare response — the mock client looks up `q.toLowerCase()` and returns `[]` for unknown queries. |
| `nodes/<safe-id>.json` | `GET /nodes/{id}` | `NodeResponse` — one per node (41) |
| `neighborhood/<safe-id>.json` | `GET /nodes/{id}/neighborhood` (depth 1) | `NeighborhoodResponse` — CLN5 disease, lysosomal degradation |
| `edges/<safe-id>.json` | `GET /edges/{id}` | `EdgeResponse` — one per edge (94) |
| `similar/<safe-id>.json` | `GET /diseases/{id}/similar` | `SimilarResponse` — CLN5 disease |
| `paths/<safe-id>.json` | `GET /paths?from={id}` | `PathsResponse` — from CLN5 disease (5 kinds) |
| `clusters.json` / `clusters/<safe-id>.json` | `GET /clusters` / `GET /clusters/{id}` | `ClustersResponse` / `ClusterResponse` |
| `action-view/<safe-id>.json` | `GET /diseases/{id}/action-view` | `ActionView` — CLN5 (rich) and subtype X (`MONDO:MOCK0099`, honest gap) |
| `mechanism-view/<safe-id>.json` | `GET /mechanisms/{id}/view` | `MechanismView` — `ATLAS:mech-lysosomal-degradation` |
| `explain.json` | `POST /explain` | `Explanation` (CLN5 → CLN6 path, audience family) |
| `outreach-draft.json` | `POST /outreach-draft` | `OutreachDraft` (CLN5 Families Network → CLN6 Parents Alliance) |
| `gap-search-job.json` | `GET /jobs/{id}` | `JobStatus` (`done`, 3 unverified leads) |
| `error-not-found.json` | any 404 | `ApiError` |

A missing file means the real API would answer **404** (`error-not-found.json`): e.g. only CLN5 and subtype X have
an action view — `nodes/*.json` says so via `has_action_view` / `has_mechanism_view`.

## Regenerate / validate

Fixtures are generated from one in-memory graph, so ids stay consistent. Don't hand-edit; change the generator:

```bash
node contract/scripts/build-fixtures.mjs      # rewrites contract/fixtures/**
node contract/scripts/validate-fixtures.mjs   # tsc --strict type check vs atlas.ts + referential integrity
node contract/scripts/validate-fixtures.mjs --no-types   # integrity only (no npx / network)
```
