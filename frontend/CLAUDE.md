# CLAUDE.md — Atlas frontend (Lovable repo)

This repo is the **frontend only** of the Rare Disease Atlas. It is generated and edited with Lovable and synced through GitHub. Read `LOVABLE.md` first: it is the binding spec for screens, the visual language and the API layer.

## Hard boundaries
- Data comes **only** from the REST API described in `src/contract/atlas.ts`, through `src/api/` (`api: AtlasApi`). No Supabase, no direct DB, no OpenAI or Bright Data calls, no secrets in code.
- `src/contract/**` and `src/mocks/fixtures/**` are **read-only**. They are synced from the backend repo by `scripts/sync-contract.sh`. If you need a contract change, write it in `CONTRACT_REQUESTS.md` and tell the team. Don't patch around it with `any` or extra fields.
- Never show an `inferred` or `hypothesis` edge without its status chip, or hide contradicting evidence.

## Working alongside Lovable
- Lovable pushes to `main`. Pull before you edit (`git pull --rebase`), push small commits often, and don't edit the same file someone is prompting Lovable about. Announce "I'm in `src/components/graph/`" in the team chat.
- Prefer editing code here for logic (API layer, hooks, graph behaviour, types). Use Lovable for layout and styling iterations.

## Commands
```bash
npm i
npm run dev                     # http://localhost:8080 (Lovable's Vite default) — uses VITE_API_BASE or mock
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
