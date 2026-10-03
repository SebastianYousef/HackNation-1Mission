# The Bridge — API contract v1

This folder is the **only** thing the frontend and backend teams share. If both sides honour it, they never block each other.

| File | What it is | Who edits |
|---|---|---|
| `atlas.ts` | Every request/response type + the `AtlasApi` client interface. **Source of truth.** | Both, via the change protocol below |
| `fixtures/` | A valid example response for every endpoint (coherent MOCK dataset: Batten/NCL slice) | Backend (keeps them valid) |
| `scripts/validate-fixtures.mjs` | Type-checks fixtures against `atlas.ts` + referential integrity | — |

The SQL that produces the GET shapes is the `api_*` functions in `backend/db/migrations/`: first defined in `*_api_v1.sql`, with `api_search` replaced in `*_search_ranking_submissions.sql` and `api_paths` in `*_path_strength.sql` and `*_paths_arrays_never_null.sql` (the last file to define a function wins). The backend's `scripts/check_contract.py` verifies a running API against this contract.

## Endpoints

Base URL is `{API_BASE}/api/v1`. In production `API_BASE` is the same origin as the website (the L7 load balancer routes `/api/*`). In development it is the backend's public dev URL. Ids are CURIEs, so always `encodeURIComponent(id)` in paths.

| Method & path | Response type | Who uses it | Notes |
|---|---|---|---|
| `GET /meta` | `MetaResponse` | footer / about | contract + dataset version |
| `GET /search?q=&types=&limit=` | `SearchResponse` | global search box | q ≥ 2 chars, matches synonyms & ids |
| `GET /nodes/{id}` | `NodeResponse` | every entity page | `has_action_view` / `has_mechanism_view` tell the UI which screen to offer |
| `GET /nodes/{id}/neighborhood?depth=&edge_types=&statuses=&min_confidence=&max_nodes=` | `NeighborhoodResponse` | graph canvas | `truncated` → "showing top N" |
| `GET /edges/{id}` | `EdgeResponse` | "Why connected?" evidence drawer | supporting / contradicting / context |
| `GET /diseases/{id}/similar?limit=` | `SimilarResponse` | related diseases list | show `caution` (counterexample) prominently |
| `GET /paths?from=&to=&kind=&limit=` | `PathsResponse` | connection stepper | precomputed, ordered nodes+edges |
| `GET /clusters` · `GET /clusters/{id}` | `ClustersResponse` · `ClusterResponse` | cluster explorer | |
| `GET /diseases/{id}/action-view` | `ActionView` | **Maria / Devon main screen** | 404 if not precomputed for that disease |
| `GET /mechanisms/{id}/view` | `MechanismView` | **Priya / Dr. Osei screen** | id = mechanism or intervention |
| `POST /explain` | `Explanation` | "Explain this path" | 2–15 s; cached until the next data load; every sentence cites an edge; 503 without an OpenAI key |
| `POST /outreach-draft` | `OutreachDraft` | "Draft a message to this group" | |
| `POST /gap-search` → `GET /jobs/{id}` | `JobAccepted` → `JobStatus` | "Search the web for missing groups" | poll every 2 s; results are UNVERIFIED |
| `POST /submissions` | `SubmissionCreated` | "Contribute evidence" form | stored for review, never shown as fact |

The ops endpoints `GET /healthz` and `GET /readyz` are used by the load balancer only, not by the UI.

**Errors.** Every non-2xx response has the body `ApiError` (`{error:{code,message,request_id}}`). The UI treats `404 not_found` as an empty state ("we have no data on this yet"), not as a crash.

**Headers.** `X-Request-Id` (show it in error toasts) and `X-Served-By` (which server replica answered; shown in the debug footer).

## Rules that keep the teams independent

1. **Frontend never talks to the database, Supabase, OpenAI or Bright Data.** It talks only to the REST API above.
2. **Backend never changes a response shape without a contract change.** Adding data inside an existing `attrs` object is allowed. Adding, renaming or removing a typed field is a contract change.
3. **Nullable means `| null`, present.** Arrays are never null. Unknown id → 404.
4. **Fixtures are always valid.** Both the frontend mock client and the backend's `DATA_MODE=fixtures` serve them, so the frontend can build every screen at hour 0.

## Change protocol (takes 5 minutes, not a meeting)

1. Open a PR touching `contract/atlas.ts` + the affected `fixtures/` (+ SQL if needed). Title: `contract: …`.
2. **One person from each side approves.**
3. Prefer **additive** changes: a new optional field `foo: X | null` or a new endpoint. Bump the minor version (`1.1.0`) in `atlas.ts` and in `api_meta()`.
4. Breaking change = major bump. Avoid after hour 6.
5. After merge, frontend copies `atlas.ts` + `fixtures/` into the Lovable repo: `scripts/sync-contract.sh <path-to-frontend-repo>`.

Contract freeze: **v1 is frozen at hour 2**. Only additive changes after that.
