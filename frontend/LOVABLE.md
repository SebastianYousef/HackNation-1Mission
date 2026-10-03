# LOVABLE.md — project knowledge for the Rare Disease Atlas frontend

> Paste this file into **Lovable → Project settings → Knowledge** and keep a copy at the repo root.
> It is the frontend's half of the bridge. The other half is `src/contract/atlas.ts`.

## 1. Your role: frontend ONLY

You build the user interface of **"Atlas — AI map for the world's rare diseases"**: a web app where a patient-group leader, a newly diagnosed family, a biotech scout or a researcher searches a disease, gene, symptom or mechanism. They then follow **evidence-backed connections** to related diseases, patient groups, reusable research assets, researchers and a concrete next step.

A separate backend team owns all data, AI and servers. They run a REST API behind a load balancer. **You consume that API and nothing else.**

### Never do these
- ❌ Do **not** enable Lovable Cloud, the Supabase integration, auth, storage, or database tables. Do **not** write migrations or edge functions. There is no backend code in this repo.
- ❌ Do **not** call OpenAI, Bright Data, PubMed or any other external API from the browser. Do **not** put any API key in the code.
- ❌ Do **not** edit `src/contract/**` or `src/mocks/fixtures/**`. They are synced from the backend repo.
- ❌ Do **not** invent endpoints or fields. If a screen needs data that isn't in `src/contract/atlas.ts`, add an entry to `CONTRACT_REQUESTS.md` (what, why, proposed type) and render a placeholder.
- ❌ Do **not** present an `inferred` or `hypothesis` connection as fact, and never hide contradicting evidence.

### Always do these
- ✅ Every data access goes through the `AtlasApi` interface (`src/contract/atlas.ts`) via `src/api/`.
- ✅ Every connection shown in the UI shows its **status** and is clickable to its **evidence** (see §5).
- ✅ Handle loading, empty, 404 and error states on every screen.

## 2. Tech stack (fixed)
- React + Vite + TypeScript (strict) + Tailwind + shadcn/ui + lucide-react icons
- `react-router-dom` for routes, `@tanstack/react-query` for all server state (no other global store needed)
- **Graph rendering:** `cytoscape` + `react-cytoscapejs`, with the `cose-bilkent` or `fcose` layout. Small neighbourhoods only (≤ 60 nodes).
- No other heavy dependencies without a reason.

## 3. How to talk to the backend

### Configuration (`src/config.ts`)
```ts
export const API_BASE = import.meta.env.VITE_API_BASE ?? "";  // "" = same origin (production behind the load balancer)
export const DATA_MODE: "http" | "mock" =
  new URLSearchParams(location.search).get("mock") === "1" || import.meta.env.VITE_DATA_MODE === "mock" ? "mock" : "http";
```
- Production: the site and the API share one origin. The L7 load balancer routes `/api/*` to the API servers, so `API_BASE = ""`.
- Development and Lovable preview: `VITE_API_BASE` = the backend team's public dev URL (they will give it to you).
- An unset `VITE_API_BASE` does **not** mean mock mode. It means `API_BASE = ""` (same origin), which is exactly what the production build needs. Mock mode is chosen only by `VITE_DATA_MODE=mock` or `?mock=1`, as in the snippet above. In a dev or preview environment with no backend URL yet, set `VITE_DATA_MODE=mock`.
- Appending `?mock=1` to any URL forces mock mode. That's useful when the backend is down.

### API layer (`src/api/`)
```
src/api/
  http.ts     class HttpAtlasApi implements AtlasApi      — fetch(`${API_BASE}/api/v1/...`)
  mock.ts     class MockAtlasApi implements AtlasApi      — reads src/mocks/fixtures (import.meta.glob), 150–400 ms fake latency
  index.ts    export const api: AtlasApi = DATA_MODE === "mock" ? new MockAtlasApi() : new HttpAtlasApi()
  hooks.ts    useSearch, useNode, useNeighborhood, useEdge, useSimilar, usePaths, useActionView, useMechanismView,
              useClusters, useCluster, useExplain (mutation), useOutreachDraft (mutation), useGapSearch (start + poll), useSubmit
  errors.ts   class ApiError { status, code, message, requestId }
```

**HTTP rules:**
- `encodeURIComponent(id)` for every id in a path. Ids look like `MONDO:0016295`.
- List params are comma-separated: `?types=disease,gene&statuses=curated,literature`.
- Non-2xx responses have the body `{error:{code,message,request_id}}`. Throw `ApiError`.
- Record the response headers `X-Served-By` and `X-Request-Id` of the last response (a tiny store) for the debug footer.
- Set react-query `staleTime` to 5 min for GETs. Retry once for 5xx and never for 4xx. Use `keepPreviousData` for search.

**Mock rules.** Fixture file naming: ids with `:` replaced by `_`.

| Call | Fixture file |
|---|---|
| `node(id)` | `nodes/<safe>.json` |
| `neighborhood(id)` | `neighborhood/<safe>.json` |
| `edge(id)` | `edges/<safe>.json` |
| `similar(id)` | `similar/<safe>.json` |
| `paths(from)` | `paths/<safe>.json` |
| `clusters()` / `cluster(id)` | `clusters.json` / `clusters/<safe>.json` |
| `actionView(id)` | `action-view/<safe>.json` |
| `mechanismView(id)` | `mechanism-view/<safe>.json` |
| `meta()` | `meta.json` |
| `explain()` | `explain.json` |
| `outreachDraft()` | `outreach-draft.json` |
| gap search | `gap-search-job.json` (first poll "running", then the file) |
| `search(q)` | `search.json` is a map `query → hits`; for an unknown query, filter all hits by substring |

A missing fixture means throwing `ApiError(404, "not_found")`, exactly like the real API.

### Endpoint cheat-sheet (types in `src/contract/atlas.ts`)
| Need | Call |
|---|---|
| Global search | `GET /api/v1/search?q=` → `SearchHit[]` (show `matched_name` when it differs from `label`: "matched: Batten disease") |
| Entity header | `GET /api/v1/nodes/{id}` → `NodeResponse` (`has_action_view` / `has_mechanism_view` decide which tabs exist) |
| Graph | `GET /api/v1/nodes/{id}/neighborhood?depth=1&max_nodes=60` |
| Evidence drawer | `GET /api/v1/edges/{id}` |
| Related diseases | `GET /api/v1/diseases/{id}/similar` |
| Connections | `GET /api/v1/paths?from={id}` |
| Maria / Devon screen | `GET /api/v1/diseases/{id}/action-view` → `ActionView` |
| Priya / Dr. Osei screen | `GET /api/v1/mechanisms/{id}/view` → `MechanismView` |
| Plain-language path explanation | `POST /api/v1/explain {edge_ids, audience}` (slow: 2–15 s, show progress) |
| Draft a message | `POST /api/v1/outreach-draft` |
| Web search for missing groups | `POST /api/v1/gap-search` → poll `GET /api/v1/jobs/{id}` every 2 s |
| Contribute evidence | `POST /api/v1/submissions` |

## 4. Screens and routes

| Route | Screen | Persona |
|---|---|---|
| `/` | **Home.** One big search box ("Search a disease, gene, symptom or mechanism"), three entry chips: *"My family just got a diagnosis"*, *"I lead a patient group"*, *"I research a mechanism"*, and the dataset/coverage line from `/meta`. | all |
| `/search?q=` | Results grouped by type, with synonym matches explained. | all |
| `/d/:id` | **Disease action view** (from `ActionView`), detailed below. | Maria, Devon |
| `/m/:id` | **Mechanism view.** "Every gene name this mechanism hides under", ranked diseases with groups/assets/unmet need, researchers, trials, interventions. | Priya, Dr. Osei |
| `/n/:id` | Generic entity page: header, plain summary, synonyms, graph canvas, neighbours grouped by type. Disease and mechanism ids redirect to `/d` or `/m` when those views exist. | all |
| `/c/:id` | Cluster page: members, shared symptoms and mechanisms, mini-graph. | Priya, Osei |
| `/explore/:id` | Full-screen graph explorer with filters (edge type, status, min confidence) and a depth control. | expert |

**The evidence drawer is global.** Any element with an edge id opens it (URL `?edge=<id>` so it is shareable).

### `/d/:id`: the core journey, in progressive reveal (summary first, depth on click)
1. **Header:** disease name, `headline`, `plain_summary`, treatment status badge (cite `treatment_status.edge_ids`).
2. **Your community:** `exact_groups`. If it is empty, say so honestly: "We found no patient group for exactly this diagnosis yet." Then show the closest `related_communities` and a "Help build it" action (gap-search + contribute).
3. **Connections:** `connections` as a **stepper**: node → edge → node, each edge with its status chip and "why?". Add an "Explain in plain language" button that calls `/explain` with the path's `edge_ids`.
4. **Related communities:** for each, show `why` **and** `differences` side by side. Display `caution` (a counterexample, e.g. "a therapy that works for X may not transfer because the mechanism differs") in an amber callout. Never hide it.
5. **What already exists:** `assets` (reusability badge: direct / adaptable / reference only; show `what_differs` and `needs_review`) and `trials`.
6. **People:** `researchers`, plus a **"Bridges between communities"** list from `shared_people` (network overlap).
7. **Next steps:** `next_steps` as action cards, grouped by `effort` (this week / this month / this quarter) and colored by `status` (viable / needs review / unsupported). Each card has a "Draft message" button (`/outreach-draft`) when `target` is an organization or person.
8. **What we don't know:** `coverage`. Show sources checked (with counts) and `gaps` (question → why it matters → what would resolve it). If `has_supported_route === false`, put this section first, phrased kindly and clearly.

## 5. Visual language: "low ink, high signal"

The UI is mostly whitespace and neutral greys. **Color only ever means something.** Every colored thing also has a text label or icon, for accessibility.

**Edge status (most important).** Use the same encoding everywhere: graph edges, chips, the stepper.

| status | meaning shown to users | line style | chip |
|---|---|---|---|
| `curated` | "Established: from a curated database" | solid, dark slate | slate |
| `literature` | "Reported in research" | solid, blue | blue |
| `inferred` | "Computed by the Atlas (similarity)" | dashed, violet | violet |
| `hypothesis` | "Hypothesis: needs testing" | dotted, amber | amber |

- **Contradicting evidence:** a red ⚠ marker on the edge plus a "contradicted by N sources" chip, and the contradicting quotes appear first in the drawer.
- **Confidence:** edge width (thin → thick) and a small 5-dot meter in chips. Never show raw decimals to families. Expert mode shows `0.82`.
- **Node types:** one muted hue plus one lucide icon each: disease (`HeartPulse`), gene (`Dna`), variant (`Dot`), phenotype (`Eye`), mechanism (`Cog`), intervention (`Pill`), organization (`Users`), person (`User`), publication (`FileText`), trial (`FlaskConical`), grant (`Banknote`), asset (`Database`).
- **Family / Expert toggle** (top bar, persisted in localStorage):
  - Family hides ids and decimals, uses `summary`/`plain_summary`, and sends `audience: "family"`.
  - Expert shows CURIEs, scores, methods and sources.

**Evidence drawer contents:**
- edge sentence: "**CLN5** — *associated with* → **CLN5 disease**"
- status chip, confidence and sources
- tabs: Supporting / Contradicting / Context. Each evidence row shows the quote, source name with a link, `source_ref`, date and method (`llm:…` → badge **"Extracted by GPT"** + the PMID link, `algorithm:…` → "computed by the Atlas").
- The explanation panel (from `/explain`) carries a small **"Explained by GPT · every sentence cited"** label. Each sentence links to its edge. The OpenAI prize track requires judges to *see* where OpenAI is used, so keep these labels visible in both Family and Expert mode.

## 6. Honesty and safety copy (required)
- Footer on every page: *"Atlas organizes published research to help communities find each other. It is not medical advice."*
- Gap-search results are labeled **"Unverified web results"**, and each one gets a "Report / contribute" link.
- Next steps with `status: "unsupported"` are shown greyed out with their `validation_needed` list, never as a recommendation.

## 7. Debug footer (small, bottom-right, collapsible)
Show `mode: live|mock`, `contract 1.0.0 · dataset <version>`, and `served by: <X-Served-By>`. The last one shows which backend replica answered; we use it to demo load balancing. It is hidden behind a toggle in production.

## 8. Quality bar
- Responsive down to 375 px. On mobile the graph collapses to a list, with the canvas behind a "Show map" button.
- Use skeleton loaders, not spinners. Every list has an empty state. Errors show a toast with the `X-Request-Id`.
- Keyboard: `/` focuses search, `Esc` closes the drawer.
- Keep components small: `src/components/graph/`, `src/components/evidence/`, `src/components/cards/` (OrgCard, AssetCard, TrialCard, PersonCard, NextStepCard), `src/pages/`.

## 9. The demo journey (must be flawless)

These are demo fixture ids. Real ids come from the backend.

1. Home → type "batten" → results show "matched: Batten disease" → open the CLN5 disease.
2. The action view says "no approved treatment" and shows the community, then **Connections**: CLN5 disease → lysosomal mechanism → CLN6 disease → patient group.
3. Click an edge to open the evidence drawer with its quote and source. Then click "Explain in plain language" for a cited explanation.
4. Related communities: the CLN2 caution callout (enzyme replacement works there, but the mechanism differs).
5. What already exists: a registry marked "adaptable" with "needs review" questions.
6. Next steps: "Draft message" opens a sourced email.
7. Switch to a disease with no supported route to show the honest "What we don't know" state.
