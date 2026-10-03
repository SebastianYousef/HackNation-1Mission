# Frontend: setup and Lovable playbook

The frontend lives in **its own repo created by Lovable** (e.g. `HackNation-1Mission-web`). Lovable's GitHub integration creates and owns a repo of its own. That separation is exactly what we want: the two teams share only `contract/`.

This folder holds the frontend's half of the bridge:
- `LOVABLE.md` is pasted into Lovable's project Knowledge and copied to the web repo root.
- `CLAUDE.md` is for Claude Code sessions inside the web repo.
- This README covers setup and the prompt sequence.

## Setup (hour 0, ~20 min, one frontend person)
1. Create the Lovable project "Atlas". **Do not** enable Lovable Cloud or Supabase.
2. Project settings → **Knowledge** → paste the full contents of `LOVABLE.md`.
3. Connect GitHub → Lovable creates the repo. Clone it next to this repo:
   ```bash
   git clone git@github.com:<you>/HackNation-1Mission-web.git ../HackNation-1Mission-web
   ./scripts/sync-contract.sh ../HackNation-1Mission-web
   cd ../HackNation-1Mission-web && git add -A && git commit -m "Sync contract v1" && git push
   ```
4. Send **Prompt 1** below. From then on, re-run `sync-contract.sh` whenever `contract/` changes.

**Lovable credits.** The free plan has a small daily message cap. Put one teammate on a paid plan for the hackathon, or have one person prompt Lovable while the other codes in the synced repo with Claude Code. Batch requests into big, specific prompts like the ones below.

## Who does what (2 people, parallel from hour 0)

| | **F1: Journey & product** | **F2: Graph & data layer** |
|---|---|---|
| Owns | `src/pages/` (Home, Search, Disease action view, Mechanism view), cards, copy, Family/Expert mode, demo video | `src/api/` (http + mock + hooks), `src/lib/encoding.ts`, `src/components/graph/`, `src/components/evidence/`, explorer, cluster page, debug footer |
| Uses Lovable for | layouts, cards, styling | very little; mostly hand-written code in the synced repo |
| Hour 0–2 | Prompt 1, then the Home and Search pages | API layer + mock client + hooks, all compiling against `atlas.ts` |
| Hour 2–8 | `/d/:id` action view sections 1–8 on mocks | graph canvas, evidence drawer, path stepper component |
| Hour 8–14 | `/m/:id` mechanism view, outreach draft, gap search UI | switch to live API (`VITE_API_BASE`), fix real-data edge cases, explorer |
| Hour 14–20 | polish the demo journey, honesty copy, mobile | performance, error states, debug footer (X-Served-By) |
| Hour 20–24 | record the 1-min walkthrough + team video | production build → handed to backend for the `web` service |

## Prompt sequence for Lovable
**Fastest path to a working prototype on real data:** paste `frontend/PROTOTYPE_PROMPT.md` (one prompt, live API, verified demo ids) instead of prompts 1–4. Then continue with prompt 5.

Send these in order. Each one is self-contained and refers to the Knowledge.

**Prompt 1: skeleton and API layer**
> Read the project Knowledge (LOVABLE.md) carefully; it's binding. Set up the app shell: routes `/`, `/search`, `/d/:id`, `/m/:id`, `/n/:id`, `/c/:id`, `/explore/:id`, a top bar with the global search box and a Family/Expert toggle, the honesty footer, and the collapsible debug footer. Create `src/config.ts` and the `src/api/` layer exactly as specified (HttpAtlasApi, MockAtlasApi reading `src/mocks/fixtures` via import.meta.glob, react-query hooks, ApiError). Do not touch `src/contract/` or `src/mocks/fixtures/`. Do not enable any backend. Follow the `src/config.ts` snippet exactly: an unset `VITE_API_BASE` means same origin (production), not mock mode; mock mode is only `VITE_DATA_MODE=mock` or `?mock=1`.

**Prompt 2: visual language**
> Implement `src/lib/encoding.ts` (status → color/line style/label, node type → icon/hue) and the primitives StatusChip, ConfidenceDots, NodeIcon, EmptyState exactly per section 5 of the Knowledge. Then the global EvidenceDrawer (URL param `?edge=`), using `useEdge`, with Supporting/Contradicting/Context tabs.

**Prompt 3: Home and Search**
> Build Home (big search, three persona chips, coverage line from meta) and `/search` (results grouped by node type, "matched: <synonym>" when it differs from the label, keyboard navigation, `/` focuses search).

**Prompt 4: disease action view**
> Build `/d/:id` from `useActionView`, sections 1–8 in order, with progressive reveal per the Knowledge. If `coverage.has_supported_route` is false, move "What we don't know" to the top. Connections render as a stepper (node → status-chipped edge → node); every edge is clickable to the evidence drawer. Add "Explain in plain language" (useExplain) and "Draft message" (useOutreachDraft) actions.

**Prompt 5: graph**
> Build `src/components/graph/GraphCanvas.tsx` with cytoscape + react-cytoscapejs + fcose: node icon/hue by type, edge style by status from encoding.ts, width by confidence, red marker on edges with contradict_count > 0. Click node → navigate, click edge → evidence drawer. Highlight a path when given `edge_ids`. Use it on `/n/:id` (depth 1) and `/explore/:id` (filters: edge types, statuses, min confidence, depth 1–2, "showing top N" when truncated).

**Prompt 6: mechanism and cluster views**
> Build `/m/:id` from `useMechanismView` (genes this mechanism hides under, ranked diseases with groups/assets/unmet need, researchers, trials) and `/c/:id` from `useCluster`.

**Prompt 7: gaps and contributions**
> Add "Search the web for missing groups" (useGapSearch: start, poll every 2 s, results labeled "Unverified web results") and a "Contribute evidence" dialog (useSubmit) on disease pages.

**Prompt 8: polish**
> Make everything responsive to 375 px (graph collapses to a list with a "Show map" button), use skeleton loaders everywhere, error toasts with request id, and an empty state on every list. Walk the demo journey in LOVABLE.md section 9 in mock mode and fix anything that is not smooth.

## Going live
- **Dev:** the backend gives you a public URL (cloudflared tunnel or the deployed LB). Set `VITE_API_BASE` in `.env.local`, and in Lovable as an environment variable if needed. Their CORS allows `*.lovable.app` / `*.lovableproject.com` / localhost.
- **Production:** `npm run build`. The `dist/` folder is served by the backend's `web` service behind the same load balancer, so `API_BASE=""` (same origin, no CORS). Lovable's own publish (`*.lovable.app`) remains a backup demo URL, pointing at the public API.
