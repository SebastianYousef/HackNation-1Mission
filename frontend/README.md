# Frontend: setup and playbook

The frontend lives in **its own repo**. That separation is exactly what we want: the two teams share only `contract/`.

**The team's actual app** is the team's fork (see root `CLAUDE.md`, rule 6, and `FRONTEND_REPO`/`FRONTEND_REF` on the server). To work on it, clone that repo and branch and run `./scripts/sync-contract.sh <path-to-the-clone>`. The setup below describes starting a fresh frontend repo; `HackNation-1Mission-web` there is only an example name.

This folder holds the frontend's half of the bridge:
- `FRONTEND_SPEC.md` is the binding spec, copied to the web repo root.
- `CLAUDE.md` is for Claude Code sessions inside the web repo.
- `PROTOTYPE_PROMPT.md` is a one-message prompt that ports `landing-2.html` into the app on live data.
- `landing.html` is the public landing page served at `/`; `landing-2.html` is currently an identical copy. Neither is synced into the web repo.
- This README covers setup and the prompt sequence.

## Setup (hour 0, ~20 min, one frontend person)
1. Create the frontend repo (React + Vite + TypeScript, see `FRONTEND_SPEC.md` §2) and clone it next to this repo:
   ```bash
   git clone git@github.com:<you>/HackNation-1Mission-web.git ../HackNation-1Mission-web
   ./scripts/sync-contract.sh ../HackNation-1Mission-web
   cd ../HackNation-1Mission-web && git add -A && git commit -m "Sync contract v1" && git push
   ```
2. Send **Prompt 1** below to Claude Code in that repo. From then on, re-run `sync-contract.sh` whenever `contract/` changes.

## Who does what (2 people, parallel from hour 0)

| | **F1: Journey & product** | **F2: Graph & data layer** |
|---|---|---|
| Owns | `src/pages/` (Home, Search, Disease action view, Mechanism view), cards, copy, Family/Expert mode, demo video | `src/api/` (http + mock + hooks), `src/lib/encoding.ts`, `src/components/graph/`, `src/components/evidence/`, explorer, cluster page, debug footer |
| Hour 0–2 | Prompt 1, then the Home and Search pages | API layer + mock client + hooks, all compiling against `atlas.ts` |
| Hour 2–8 | `/d/:id` action view sections 1–8 on mocks | graph canvas, evidence drawer, path stepper component |
| Hour 8–14 | `/m/:id` mechanism view, outreach draft, gap search UI | switch to live API (`VITE_API_BASE`), fix real-data edge cases, explorer |
| Hour 14–20 | polish the demo journey, honesty copy, mobile | performance, error states, debug footer (X-Served-By) |
| Hour 20–24 | record the 1-min walkthrough + team video | production build → handed to backend for the `web` service |

## Prompt sequence (Claude Code)
**Fastest path to a working prototype on real data:** paste `frontend/PROTOTYPE_PROMPT.md` (one prompt, live API, verified demo ids) instead of prompts 1–4. Then continue with prompt 5.

Send these in order. Each one is self-contained and refers to `FRONTEND_SPEC.md`.

**Prompt 1: skeleton and API layer**
> Read `FRONTEND_SPEC.md` carefully; it's binding. Set up the app shell: routes `/app` (Home; `/` is the landing page on the server, so redirect `/` → `/app` in dev), `/search`, `/d/:id`, `/m/:id`, `/n/:id`, `/c/:id`, `/explore/:id`, a top bar with the global search box and a Family/Expert toggle, the honesty footer, and the collapsible debug footer. Create `src/config.ts` and the `src/api/` layer exactly as specified (HttpAtlasApi, MockAtlasApi reading `src/mocks/fixtures` via import.meta.glob, react-query hooks, ApiError). Do not touch `src/contract/` or `src/mocks/fixtures/`. Do not enable any backend. Follow the `src/config.ts` snippet exactly: an unset `VITE_API_BASE` means same origin (production), not mock mode; mock mode is only `VITE_DATA_MODE=mock` or `?mock=1`.

**Prompt 2: visual language**
> Implement `src/lib/encoding.ts` (status → color/line style/label, node type → icon/hue) and the primitives StatusChip, ConfidenceDots, NodeIcon, EmptyState exactly per section 5 of `FRONTEND_SPEC.md`. Then the global EvidenceDrawer (URL param `?edge=`), using `useEdge`, with Supporting/Contradicting/Context tabs.

**Prompt 3: Home and Search**
> Build Home (big search, three persona chips, coverage line from meta) and `/search` (results grouped by node type, "matched: <synonym>" when it differs from the label, keyboard navigation, `/` focuses search).

**Prompt 4: disease action view**
> Build `/d/:id` from `useActionView`, sections 1–8 in order, with progressive reveal per `FRONTEND_SPEC.md`. If `coverage.has_supported_route` is false, move "What we don't know" to the top. Connections render as a stepper (node → status-chipped edge → node); every edge is clickable to the evidence drawer. Add "Explain in plain language" (useExplain) and "Draft message" (useOutreachDraft) actions.

**Prompt 5: graph**
> Build `src/components/graph/GraphCanvas.tsx` with cytoscape + react-cytoscapejs + fcose: node icon/hue by type, edge style by status from encoding.ts, width by confidence, red marker on edges with contradict_count > 0. Click node → navigate, click edge → evidence drawer. Highlight a path when given `edge_ids`. Use it on `/n/:id` (depth 1) and `/explore/:id` (filters: edge types, statuses, min confidence, depth 1–2, "showing top N" when truncated).

**Prompt 6: mechanism and cluster views**
> Build `/m/:id` from `useMechanismView` (genes this mechanism hides under, ranked diseases with groups/assets/unmet need, researchers, trials) and `/c/:id` from `useCluster`.

**Prompt 7: gaps and contributions**
> Add "Search the web for missing groups" (useGapSearch: start, poll every 2 s, results labeled "Unverified web results") and a "Contribute evidence" dialog (useSubmit) on disease pages.

**Prompt 8: polish**
> Make everything responsive to 375 px (graph collapses to a list with a "Show map" button), use skeleton loaders everywhere, error toasts with request id, and an empty state on every list. Walk the demo journey in `FRONTEND_SPEC.md` section 9 in mock mode and fix anything that is not smooth.

## Going live
- **Dev:** the backend gives you a public URL (cloudflared tunnel or the deployed LB). Set `VITE_API_BASE` in `.env.local`. Their CORS allows localhost on any port.
- **Production:** `npm run build`. The `dist/` folder is served by the backend's `web` service behind the same load balancer, so `API_BASE=""` (same origin, no CORS).
