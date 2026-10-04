# CLAUDE.md — Atlas frontend repo

This repo is the **frontend only** of the Rare Disease Atlas. Read `FRONTEND_SPEC.md` first: it is the binding spec for screens, the visual language and the API layer.

## Hard boundaries
- Data comes **only** from the REST API described in `src/contract/atlas.ts`, through `src/api/` (`api: AtlasApi`). No Supabase, no direct DB, no OpenAI or Bright Data calls, no secrets in code.
- `src/contract/**` and `src/mocks/fixtures/**` are **read-only**. They are synced from the backend repo by `scripts/sync-contract.sh`. If you need a contract change, write it in `CONTRACT_REQUESTS.md` and tell the team. Don't patch around it with `any` or extra fields.
- Never show an `inferred` or `hypothesis` edge without its status chip, or hide contradicting evidence.

## Working together
- Pull before you edit (`git pull --rebase`), push small commits often, and don't edit the same file as a teammate. Announce "I'm in `src/components/graph/`" in the team chat.

## Commands
```bash
npm i
npm run dev                     # http://localhost:8080 — uses VITE_API_BASE (unset = same origin, not mock)
VITE_DATA_MODE=mock npm run dev # force mock data
npm run build && npm run preview
npx tsc --noEmit                # type-check against the contract
```
`.env.local` holds `VITE_API_BASE=https://<backend dev url>` (never commit secrets; this URL is not secret).

## Conventions
- Server state goes through react-query hooks in `src/api/hooks.ts`. Components never call `fetch` directly.
- Shared visual primitives: `StatusChip`, `ConfidenceDots`, `NodeIcon`, `EvidenceDrawer`, `EmptyState`. Reuse them, don't restyle per screen.
- Status colors and line styles live in **one** file (`src/lib/encoding.ts`). The graph, chips and stepper all import it.
- `?mock=1` works on every route. Test every screen in mock mode before wiring live data.
- The definition of done for a screen: works in mock **and** live, has loading/empty/404/error states, works at 375 px, and has a Family/Expert toggle respected.

## Where the app is deployed (live backend + hosting)
The backend team hosts the site and API on one origin: a home server behind a Cloudflare tunnel. Details are in the backend repo's root `CLAUDE.md` → "Deployment".
- **Public URL:** `https://<random>.trycloudflare.com`. It changes when the tunnel restarts, so ask the backend team for the current one. For local dev, set `VITE_API_BASE` to it (CORS allows localhost on any port).
- **Production build:** same origin, so `VITE_API_BASE=""`. All calls go to `/api/v1/*`.
- **Routes:** `/` is the team landing page (`frontend/landing.html` in the backend repo), **not** this app. Give the app's home the route **`/app`**. All other routes (`/search`, `/d/:id`, `/m/:id`, `/n/:id`, `/c/:id`, `/explore/:id`) are served to this SPA (nginx falls back to `index.html`). Link to `/app` (not `/`) for "home".
- **Shipping it:** push to the frontend branch the server builds from (`FRONTEND_REPO`/`FRONTEND_REF` in `~/atlas/frontend.env`) and tell the backend team, or run `ssh laqueinux ~/atlas/redeploy.sh` if you have access. The server runs `npm ci && npm run build` in a `node:22` container and serves `dist/`, so the build must work with plain `npm ci && npm run build` and output to `dist/`.
