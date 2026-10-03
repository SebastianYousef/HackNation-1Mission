# Prototype prompt: `landing-2.html` → a working One Mission app on real data

Paste everything inside the box below into **Lovable** (or **Claude Code** in the frontend repo) as one message.

It turns the design and sections of `frontend/landing-2.html` (One Mission) into a React app wired to the **live API with real data**. landing-2 runs on hand-written *sample* data (Leigh syndrome, Dravet). The live dataset is the **lysosomal / Batten (NCL) slice**, so the prompt keeps landing-2's look, copy and section structure and fills it with real NCL records.

**Before pasting:**
1. Attach or paste `frontend/landing-2.html` into the conversation (Lovable: upload it; Claude Code: copy it into the repo as `design/landing-2.html`).
2. Make sure `src/contract/atlas.ts` + `src/mocks/fixtures/` are synced (`scripts/sync-contract.sh`).
3. Replace `<LIVE_API_URL>` with the current backend URL: `ssh laqueinux "journalctl --user -u atlas-tunnel -o cat | grep -o 'https://[a-z0-9-]*\.trycloudflare\.com' | tail -1"`, or ask the backend team. It changes when the tunnel restarts.

---

```text
Build a WORKING PROTOTYPE of "One Mission" (the rare-disease atlas) as a React app on the LIVE backend with REAL data.

DESIGN SOURCE: the attached landing-2.html. Port its look and structure faithfully: the :root tokens incl. dark mode (--paper, --ink, --accent, --night*, --ev/--hy/--un and their -n variants), fonts (Newsreader display, Hanken Grotesk body, IBM Plex Mono labels), "night" bands, the .st status chips, line styles, section rhythm, copy tone and the Reader/Researcher switch. Use its sections as components. REPLACE all of its sample DATA with live API calls; no hard-coded diseases, people or organizations may remain (remove every "(sample)" / "Sample data · demo" label once a section is live).

DATA SOURCE: only the REST API typed in src/contract/atlas.ts (the source of truth: never invent fields or endpoints, never edit src/contract/** or src/mocks/fixtures/**). No Supabase, no Lovable Cloud, no auth, no API keys in the browser. Stack: React + Vite + TypeScript strict + Tailwind (theme mapped to landing-2's tokens) + react-router + @tanstack/react-query + lucide-react; cytoscape + fcose for the graph.

GOAL OF THIS PASS: a user can go /app → search → a disease → see its real graph and connections → click any line → see the real evidence (quotes, sources) → get a cited plain-language explanation → build a "my first step" plan. Ship that end to end first; a working plain slice beats a beautiful half.

## 0. Wiring (first)
- src/config.ts:
    const LIVE = "<LIVE_API_URL>";
    export const API_BASE = import.meta.env.VITE_API_BASE
      ?? (/lovable\.app$|lovableproject\.com$|^localhost$|^127\./.test(location.hostname) ? LIVE : "");  // "" = same origin on our server
    export const DATA_MODE = new URLSearchParams(location.search).get("mock") === "1" || import.meta.env.VITE_DATA_MODE === "mock" ? "mock" : "http";
- src/api/http.ts: HttpAtlasApi implements AtlasApi with fetch(`${API_BASE}/api/v1/...`). encodeURIComponent every id (CURIEs like "MONDO:0009745"); comma-separated list params; non-2xx → throw ApiError{status, code, message, requestId} from the body {error:{code,message,request_id}}; remember the last X-Served-By / X-Request-Id. src/api/mock.ts: MockAtlasApi over src/mocks/fixtures (import.meta.glob, ":" → "_" in file names). src/api/index.ts picks by DATA_MODE. src/api/hooks.ts: react-query hooks, staleTime 5 min, retry once on 5xx, never on 4xx, keepPreviousData for search.
- 404 = landing-2's honest "Unknown" state, never a crash. Other errors: a toast with the request id.

## 1. Status language: map the contract's 4 statuses onto landing-2's 3
Put this mapping in ONE file (src/lib/encoding.ts); chips, graph lines, steppers and tables all use it.
| contract edge.status | landing-2 chip | line (as landing-2 .lnk) | sub-label (drawer + Researcher view) |
| curated    | Evidence   | solid --ev  | "From a curated database" |
| literature | Evidence   | solid --ev  | "Reported in research" (show the quote) |
| inferred   | Hypothesis | dashed --hy | "Computed by the Atlas: plausible, untested" |
| hypothesis | Hypothesis | dashed --hy | "Proposed: needs testing" |
"Unknown" (dotted --un) is not an edge status: use it for things we have no data on (404s, empty lists, coverage.gaps, coverage.has_supported_route === false). Keep landing-2's rule visible: "An inferred connection is never presented as established clinical evidence." Researcher view additionally shows the raw status, confidence as a number, CURIEs and evidence methods; Reader view shows 5 confidence dots and plain words only. Contradicting evidence: a red marker plus a "contradicted by N" chip on the line, and the Contradicting tab opens first in the drawer.

## 2. Routes
- "/" on the server is the static team landing page, NOT this app. The app's home is "/app" (redirect "/" → "/app" for preview/dev). Every "Explore the Atlas" / logo / home link goes to "/app".
- /app (Atlas home), /search?q=, /d/:id (disease), /n/:id (any entity), /m/:id (mechanism), /c/:id (cluster), /plan?d=<id>&stage=<stage>. The evidence drawer works on every route via ?edge=<edge id> (shareable).
- Top bar = landing-2's nav (logo "ONE MISSION", Atlas · Journey · Communities · Plans · Evidence) with the Reader/Researcher switch (localStorage; Reader → audience "family", Researcher → audience "expert"). Footer on every page: "One Mission organizes published research to help communities find each other. It is not medical advice." Small collapsible debug line: live|mock · contract + dataset version (GET /meta) · served by <X-Served-By>.

## 3. Vertical slice: landing-2 sections on live data, in this order
a) /app = landing-2 "The Atlas" band. "Start with what you know." search box (Reader) / "Find connections hidden across disease boundaries." (Researcher). Replace the "Try:" chips with REAL ones:
     Reader: "Batten disease" (search "batten"), "CLN5 disease" → /d/MONDO:0009745, "vision loss" (search), "Kufs disease" → /d/MONDO:0008768
     Researcher: "CLN3" (search), "lysosome" (search), "CLN2 vs CLN3" → /d/MONDO:0008767, "NCL patient groups" → /d/MONDO:0016295
   Under it, a coverage line from GET /meta: "<n> diseases · <edges> evidence-backed connections · sources: …" (numbers from /meta, never hard-coded).
b) Search (GET /search?q=, ≥ 2 chars, debounce 200 ms): results grouped by type with landing-2's type colors; show "matched: <matched_name>" when it differs from the label. disease → /d/:id, mechanism/intervention → /m/:id, others → /n/:id.
c) /d/:id from GET /diseases/{id}/action-view (404 → /n/:id). Build it from landing-2's pieces:
   1. Header (landing-2 detail panel): label, headline, plain_summary (Reader) or disease.description + synonyms + xrefs (Researcher); treatment badge from treatment_status (true → "Approved treatment exists" as Evidence; false/null → "No approved treatment found yet"), whose edge_ids open the drawer.
   2. Graph: landing-2's graph look, from GET /nodes/{id}/neighborhood?depth=1&max_nodes=40. Click a node → its page; click a line → drawer. Same caption: "Click a node to explore. Click a line for its evidence." Below 640 px show a list with a "Show map" button.
   3. "One connected journey": action_view.connections[] as landing-2's journey stepper (node → line → node; each step: why it connects, what it rests on = its edge's sources, what could break it = contradict_count / the path's caps). Show path.weakest_status and path.min_confidence AS GIVEN as "path strength"; never recompute them from the edges (they are deliberately capped lower for routes through a parent disease). Button "Explain in plain language" → POST /explain {edge_ids: path.edge_ids, audience} (2–15 s; skeleton + "writing a cited explanation…").
   4. "The people already on the path" (landing-2 communities): exact_groups first. EMPTY IS NORMAL on real data, so say "We found no patient group for exactly this diagnosis yet" and show related_communities[].groups with "why" AND "differences" side by side; caution in an amber callout, never hidden. Then researchers (affiliation, works_on) and shared_people as "Bridges between communities". Trials: phase, overall_status, link.
   5. "Before anyone acts: what would need to be validated?" (landing-2 validation block): next_steps grouped by effort, each with status (viable → Evidence-style, needs_review → Hypothesis-style, unsupported → Unknown-style and greyed, with validation_needed as the checklist) and coverage.gaps (question → why it matters → what would resolve it). If coverage.has_supported_route === false, this section goes FIRST, phrased kindly.
   6. "Every connection needs evidence" (landing-2 evidence table): rows = the disease's paths from GET /paths?from=<id> (From / To / Source / Relationship / Confidence; the "Include hypotheses" toggle filters inferred/hypothesis). Selecting a row opens the drawer.
   7. Data sources band: coverage.sources (name, checked, result_count) plus /meta sources.
d) Evidence drawer (?edge=): GET /edges/{id}. "source — label → target" (both clickable), mapped chip + sub-label, confidence, sources, contradiction chip. Tabs Supporting / Contradicting / Context. Each row: verbatim quote, source_name linked to url (PMID:x → https://pubmed.ncbi.nlm.nih.gov/x/), source_ref, date, method badge (llm:* → "Extracted by GPT", algorithm:* → "Computed by the Atlas"). Esc closes. Explain button for the single edge.
e) Explanation panel: headline, numbered steps, each with its mapped chip linking to its edge_id, uncertainties as an Unknown-styled box, what_to_check_next. Label "Explained by GPT · every sentence cited" when model is non-null, else "Template explanation · every sentence cited" (be honest: the backend may run without an OpenAI key).
f) /plan = landing-2 "Plan your first step": "Your disease" (search-as-you-type, diseases only) + "Where are you now?" (Just diagnosed / Living with it for a while / Leading a patient group) → "Create my plan →". Build the cards from the disease's action view, keeping landing-2's card kinds and copy: Care (static advice), Community (exact_groups / related groups with website + contact), Research (trials, recruiting first; registries from assets when present), Collaboration (researchers), Shared action (the first viable next_step, labeled as a hypothesis to test together). Order by stage: diagnosed → Care, Community first; living with it → Research first; leading a group → Collaboration, Shared action and gaps first. If has_supported_route is false or the disease has no action view, use landing-2's fallback plan (genetic testing / look for an organization / search studies / "Help put your disease on the map"). Every card that rests on data shows its chip and opens its evidence. "Draft a message" on Community/Collaboration cards → POST /outreach-draft {disease_id, target_id, edge_ids} in a dialog with a copy button.

## 4. Then, if time remains
- /n/:id: header + neighbours grouped by type (each row: chip + opens the drawer) + the same graph. /m/:id from GET /mechanisms/{id}/view (404 → /n/:id). /c/:id from GET /clusters/{id}.
- "Search the web for missing groups" in the communities section → POST /gap-search, poll GET /jobs/{id} every 2 s, label results "Unverified web results". If the job fails, show its error calmly (the web-search key may not be configured yet).
- "Contribute evidence" dialog → POST /submissions.
- landing-2's 10× section can live on /about as-is (it is an illustrative model and says so).

## 5. Real-data facts to design for (verified against the live API)
- Labels are lower-case MONDO names ("neuronal ceroid lipofuscinosis 5"); show them as given (sentence case at most).
- Many nodes have summary: null and several lists are empty; assets is currently empty for every disease. Every section needs a graceful Unknown/empty state; never render "null"/"undefined".
- "batten" → juvenile NCL (MONDO:0019262), BDFA, BDSRA, the Batten Disease Clinical Research Consortium grant.
- Hero CLN5 (MONDO:0009745): no approved treatment, no exact patient group, 6 related communities with 4 groups, 8 connections, 8 trials, 8 researchers, 4 next steps, 3 gaps, a caution.
- CLN3 (MONDO:0008767): 1 exact group (Beyond Batten Disease Foundation) + the CLN2 counterexample caution. CLN2 (MONDO:0008769): approved treatment exists. NCL umbrella (MONDO:0016295): 3 groups (BDFA, BDSRA, NCL-Stiftung).
- Kufs type (MONDO:0008768): coverage.has_supported_route === false (the honest "Unknown" state).
- Mechanism views exist only for a few mechanisms; always handle 404.

## 6. Build and deploy constraints (our server builds this automatically)
- `npm ci && npm run build` must succeed from a clean checkout and output to dist/. No .env needed: production is same-origin (API_BASE "").
- Responsive to 375 px, skeleton loaders (not spinners), keyboard: "/" focuses search, Esc closes the drawer; respect prefers-reduced-motion and dark mode like landing-2.

## 7. Acceptance (check each against the LIVE API, not mocks)
1. /app looks like landing-2's Atlas band, shows the real /meta coverage line, and the debug line says "live" with a served-by id.
2. Search "batten" → grouped results with "matched: batten disease" on juvenile NCL.
3. /d/MONDO:0009745 renders header, graph, journey, communities (honest empty state + related groups), validation, evidence table and sources.
4. Any line or row opens the drawer with real quotes and sources; Esc closes; ?edge= links are shareable.
5. "Explain in plain language" returns cited steps (or the honest template label).
6. /d/MONDO:0008767 shows the amber CLN2 caution; the Reader/Researcher switch changes the detail level everywhere.
7. /d/MONDO:0008768 opens with the validation/Unknown section first, phrased kindly.
8. /plan?d=MONDO:0009745&stage=diagnosed produces a real plan; a disease with no data gives the fallback plan.
9. /d/MONDO:0000000 shows a friendly Unknown state; ?mock=1 still works on every route; no sample data is left anywhere.
Report which acceptance items pass, and anything in the contract that blocked you (write it to CONTRACT_REQUESTS.md, don't patch around it).
```

---

## After the prompt
- **Ship it:** push to the frontend branch and tell the backend team, or set `FRONTEND_REPO`/`FRONTEND_REF` in `~/atlas/frontend.env` on the server and run `ssh laqueinux ~/atlas/redeploy.sh` (root `CLAUDE.md` → "Deploying the team frontend"). It then lives at `<public URL>/app`.
- **If the team wants landing-2's Leigh / Dravet journeys on real data**, the backend slice must be widened first (`backend/pipeline/config/slice.yaml`: add their MONDO roots to `roots` and `focus_roots`, re-run the pipeline). Until then the app shows the NCL slice.
- Next prompts: the playbook in `frontend/README.md` (graph explorer, polish).
