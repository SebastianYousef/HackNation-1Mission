# CLAUDE.md — Rare Disease Atlas (backend + contract repo)

Hack-Nation 7th Global AI Hackathon, Challenge 05: **AI Atlas for the World's Rare Diseases** (`docs/brief.pdf`). We are building an evidence-cited knowledge graph that takes a patient-group leader from her disease to a supported connection, an existing asset, a collaborator and a concrete next step. 24 hours, 4 people: 2 backend (this repo) and 2 frontend (separate repo).

Read before non-trivial work: `docs/PLAN.md` (roles, timeline, scope decisions), `docs/ARCHITECTURE.md` (system design), `contract/README.md` (the bridge).

## Repo map

| Path | What | Owner |
|---|---|---|
| `contract/` | **The bridge.** `atlas.ts` = every API type; `fixtures/` = example responses; change protocol in README | B2 + F2 jointly |
| `backend/db/migrations/` | Postgres schema + `api_*` SQL functions (one per GET endpoint) | B2 (B1 for tables) |
| `backend/api/` | FastAPI service: REST `/api/v1/*`, stateless, Redis cache/jobs, OpenAI explain/draft, worker | B2 |
| `backend/pipeline/` | Offline ETL: ingest → extract (OpenAI) → reconcile → analytics → views → load | B1 |
| `backend/scripts/check_contract.py` | Verifies a running API against the contract | B2 |
| `infra/` | Docker Compose stack: L4 HAProxy → L7 HAProxy ×2 → API ×N, worker, web (nginx), Redis, Postgres | B2 |
| `frontend/` | Not app code: the frontend's half of the bridge (`FRONTEND_SPEC.md` and `CLAUDE.md` are synced into the frontend repo; `README.md` playbook, `PROTOTYPE_PROMPT.md`), plus `landing.html` (the public landing page, served at `/`, on live API data) | F1 + F2 |
| `scripts/sync-contract.sh` | Copies contract + fixtures + frontend spec into the frontend repo | anyone |
| `docs/` | `brief.pdf` (the challenge), `PLAN.md` (roles, timeline), `ARCHITECTURE.md` | — |
| `Makefile` | Root targets (`make help`): API dev/test, contract check, migrations, compose stack | — |
| `deploy/autodeploy/` | Auto-deploy for the live server: a `systemd --user` timer that runs `~/atlas/redeploy.sh` within a minute of every push to `main` (install once with `install.sh`) | B2 |
| `deploy/gpu-server/` | Older Tailscale-Funnel deploy kit (needs root). **Superseded** by the live deployment below | — |

## Non-negotiable rules
1. **The contract is law.** API responses must match `contract/atlas.ts` exactly. Nullable fields are present as `null`; arrays are never null; unknown id → 404 with the `ApiError` body. Changing a typed field = contract PR approved by one backend and one frontend person (`contract/README.md`). Prefer additive changes plus a minor version bump in both `atlas.ts` and `api_meta()`.
2. **Every edge has evidence.** Never insert an edge without ≥ 1 `evidence` row. Status must be honest: `curated` only for curated databases, `literature` for text-extracted claims (with a verbatim quote), `inferred` for our computations, `hypothesis` for anything speculative. LLM output is never `curated`.
3. **The LLM rephrases; it never invents.** Explanations, views and outreach drafts may only use evidence passed into the prompt, and every sentence cites an `edge_id`. Validate cited ids against the input.
4. **The API is stateless.** No in-process state that must survive a request (no sessions, no in-memory jobs in prod). Shared state goes to Postgres (durable) or Redis (ephemeral). This is what makes horizontal scaling and sticky-session-free load balancing work.
5. **Secrets only in `.env`** (gitignored): `OPENAI_API_KEY`, `BRIGHTDATA_API_KEY`, `DATABASE_URL`, `NCBI_API_KEY`. Never in code, fixtures, logs or commits. The frontend never receives any secret.
6. **The frontend app is not in this repo.** Teammates build it in their own repo (the server builds `FRONTEND_REPO`/`FRONTEND_REF` from `~/atlas/frontend.env`). Don't generate React code here; change `frontend/FRONTEND_SPEC.md` if the frontend's instructions must change, then run `scripts/sync-contract.sh`. This repo only *hosts* their build (see Deployment).
7. **Fixtures stay valid.** If you change the contract, update `contract/fixtures/` and run `node contract/scripts/validate-fixtures.mjs`.

## Common commands
Targets live in the root `Makefile`; check `make help` for the current list.
```bash
make api-dev                         # API on :8000 in DATA_MODE=fixtures (hot reload)
make api-test                        # pytest for the API
make contract-check BASE_URL=http://localhost:8000
node contract/scripts/validate-fixtures.mjs
make db-migrate DATABASE_URL=...     # apply backend/db/migrations in order
make up / make down / make scale N=5 # full LB stack via docker compose (infra/)
make lb-demo                         # curl the LB repeatedly, print X-Served-By
cd backend/pipeline && make all      # rebuild the dataset (see backend/pipeline/README.md)
```

## Conventions
- **Ids** are CURIEs (`MONDO:0016295`, `HGNC:2073`, `HP:0001250`, `PMID:…`, `NCT…`, `ORG:<slug>`, `PERSON:<slug>`, `ASSET:<slug>`, `ATLAS:mech-<slug>`). Edge id = `E:` + sha1(`type|src|dst`)[:16]. Use the helpers in `backend/pipeline` rather than hand-building ids.
- **SQL:** every API read is a `stable` SQL function `api_<name>(p_…)` returning `jsonb`. JSON shapes are built only in the `_node_brief/_node_full/_edge_json/_evidence_json` helpers. New migrations get a new timestamped file; never edit an applied migration after the hour-2 freeze.
- **Python:** 3.13, type hints, pydantic models mirroring the contract, `psycopg` 3 with `prepare_threshold=None` (Supabase transaction pooler).
- **Commits:** small, prefixed `api:`, `pipeline:`, `infra:`, `db:`, `contract:`, `docs:`. Reference the issue (`Fixes #12` closes it on merge; `Refs #12` doesn't).
- **Open work lives in GitHub issues** (`gh issue list`), not in TODO files or doc checklists. Label each one `frontend`, `backend` or `submission` (plus `bug`/`enhancement` if they fit). When you find work you won't do now, open an issue (`gh issue create`) instead of leaving a note. Before starting something, check whether an issue already covers it, and comment on it as you go.
- **Before saying something works:** run it. For API changes, run `make api-test` and `make contract-check`. For pipeline changes, load into a scratch DB and query the affected `api_*` function.

## Deployment (live, 24/7)

Everything runs on a friend's Linux PC, reached as **`ssh laqueinux`** (user `sebbe`, over Tailscale). The public internet reaches it through a Cloudflare quick tunnel.

**We are guests there. Hard rules for every human and agent:**
- Never use `sudo`. Stay inside `/home/sebbe`.
- Don't touch the owner's system Docker, Tailscale or other services.
- Bind every port to `127.0.0.1` only.
- Never print or commit `~/atlas/atlas.env` or `~/app/backend/.env`.

```
Internet ─► https://negligent-easiness-follicle.ngrok-free.dev ─► ngrok (systemd --user: atlas-ngrok)
             ─► nginx "web" 127.0.0.1:8080 ─┬─ /          → frontend/landing.html
                                            ├─ /api/*     → api 127.0.0.1:8000 (1 replica) ─► db (pgvector/pg16) + redis
                                            └─ other paths → team SPA build (falls back to landing.html until deployed)
                                                 worker ◄─ redis queue (gap-search → Bright Data)
```
Containers run in **rootless Docker** (`~/bin`, managed with `systemctl --user docker`). Linger is on, so everything survives logout and reboot via `restart: unless-stopped`. The HAProxy L4/L7 tier from `infra/` is **off** on this host.

### Where things are on the server
| Path | What |
|---|---|
| `~/app` | Clone of this repo (HTTPS, `main`). **Don't edit it in place.** Change things via git push + redeploy. |
| `~/app/backend/.env` | Secrets (copied from a laptop, mode 600; gitignored) |
| `~/atlas/` | Deploy config, **outside git** |
| `~/atlas/dc` | `docker compose` wrapper: `infra/docker-compose.yml` + `compose.override.yml` + `atlas.env`, project `atlas` |
| `~/atlas/compose.override.yml` | Host overrides: services db/redis/api/worker/web, loopback ports, resource limits, l4/l7 disabled |
| `~/atlas/atlas.env` | `DATA_MODE=db`, generated `POSTGRES_PASSWORD`, `DATABASE_URL` (secret) |
| `~/atlas/nginx/site.conf` | nginx routing (landing, `/api` proxy, SPA fallback) |
| `~/atlas/build-web.sh`, `~/atlas/frontend.env` | Builds the team SPA (if `FRONTEND_REPO` is set) + copies `landing.html` into `~/atlas/www/releases/<ts>`, then flips the `www/current` symlink |
| `~/atlas/redeploy.sh` | Redeploy: pull, apply new migrations, rebuild, republish the site, health check |
| `~/atlas/atlas-autodeploy.sh`, `~/.config/systemd/user/atlas-autodeploy.{service,timer}`, `~/atlas/autodeploy/` | Auto-deploy (from `deploy/autodeploy/`): checks `origin/main` every minute and runs `redeploy.sh` when it moved; log in `~/atlas/autodeploy/autodeploy.log` |
| `~/atlas/backup.sh`, `~/atlas/backups/` | `pg_dump` backups (keeps 14). Runs daily at 04:00 via `atlas-backup.timer` |
| `~/atlas/migrations.applied` | Migration files already applied. `redeploy.sh` applies any new `backend/db/migrations/*.sql` once. |
| `~/atlas/pipeline-data/` | Pipeline `data/` dir (raw download cache, interim, snapshot) |
| `~/bin/ngrok`, `~/.config/systemd/user/atlas-ngrok.service` | The public tunnel: a free static ngrok domain, the same URL after restarts. Browsers see an ngrok notice once; API clients send `ngrok-skip-browser-warning: 1`. The old cloudflared quick tunnel (`atlas-tunnel.service`) is stopped and disabled. |

### Day-to-day commands (run from a laptop)
```bash
ssh laqueinux ~/atlas/redeploy.sh                         # deploy whatever is on origin/main (the auto-deploy timer does this within a minute of a push)
ssh laqueinux bash ~/app/deploy/autodeploy/install.sh     # one-time: install the auto-deploy timer (no sudo)
ssh laqueinux 'systemctl --user list-timers atlas-autodeploy.timer; tail -20 ~/atlas/autodeploy/autodeploy.log'   # auto-deploy status
ssh laqueinux '~/atlas/dc ps'                             # status
ssh laqueinux '~/atlas/dc logs -f --tail 100 api worker'  # logs (also: db, redis, web)
ssh laqueinux 'systemctl --user status atlas-ngrok --no-pager'   # public tunnel (URL is fixed: https://negligent-easiness-follicle.ngrok-free.dev)
ssh laqueinux ~/atlas/backup.sh                           # backup now → ~/atlas/backups/atlas-<ts>.dump
scp laqueinux:atlas/backups/<file>.dump .                 # copy a backup off the machine
# restore: ssh laqueinux '~/atlas/dc exec -T db pg_restore -U atlas -d atlas --clean --if-exists' < <file>.dump
ssh laqueinux curl -s ifconfig.me                         # public IP, for the Bright Data allowlist (home line, can change)
```
- The **public URL is fixed** (free static ngrok domain), so the page's `LIVE_API` fallback and the README link stay valid across restarts. The GitHub Pages mirror (https://sebastianyousef.github.io/HackNation-1Mission/) reads the same API.
- **New secrets** (e.g. `OPENAI_API_KEY`, `BRIGHTDATA_API_KEY`): add them to `~/app/backend/.env` on the server, then run `redeploy.sh`.
- **Never** run `docker compose up` in `~/app/infra` directly. It would try to start the HAProxy tier on ports 80/443. Always use `~/atlas/dc`.

### Reloading data
The pipeline runs on the server in a throwaway container on the stack's network. The download cache in `~/atlas/pipeline-data` is reused.
```bash
ssh laqueinux 'export DOCKER_HOST=unix:///run/user/$(id -u)/docker.sock; set -a; . ~/atlas/atlas.env; set +a;
  docker run --rm --network atlas_default -v ~/app/backend/pipeline:/src -v ~/atlas/pipeline-data:/data \
    -v ~/atlas/pip-cache:/root/.cache/pip -e ATLAS_DATA_DIR=/data -e ATLAS_CONFIG_DIR=/src/config \
    -e DATABASE_URL="$DATABASE_URL" -w /src python:3.13-slim \
    sh -c "pip install -q --root-user-action=ignore \".[graph]\" && atlas-pipeline ingest && atlas-pipeline analytics && atlas-pipeline views && atlas-pipeline load"'
ssh laqueinux 'cd ~/app && python3 backend/scripts/check_contract.py --base-url http://127.0.0.1:8000'
```
That is the exact command that produced the current dataset. It deliberately passes **no** secrets, so Bright Data and OpenAI stages are skipped. To use the keys, add `--env-file ~/app/backend/.env` **together with** `.[graph,scrape]`; otherwise the Scraping Browser path runs without playwright. The Scraping Browser only works once the server's public IP is on the zone allowlist. Add `atlas-pipeline extract && atlas-pipeline reconcile` (before `analytics`) once `OPENAI_API_KEY` is set. The API cache keys on the dataset version, so a load is visible immediately.

### Deploying the team frontend
1. In the team's frontend repo: the API base is same-origin (`VITE_API_BASE=""`), and the app's home route is **`/app`**, because `/` is the landing page. Its other routes (`/search`, `/d/:id`, `/m/:id`, …) work unchanged.
2. On the server, set `FRONTEND_REPO=https://github.com/<owner>/<repo>.git`, `FRONTEND_REF=<branch>` (and `FRONTEND_SUBDIR` if the app isn't at the repo root) in `~/atlas/frontend.env`.
3. Run `ssh laqueinux ~/atlas/redeploy.sh`. It runs `npm ci && npm run build` in a `node:22` container and serves `dist/`.

**Fixed public address for the landing page:** https://sebastianyousef.github.io/HackNation-1Mission/ (GitHub Pages, `.github/workflows/pages.yml`). Every push to `main` that touches `frontend/landing.html` republishes it within a couple of minutes, for everyone, with no server step; the page reads live data from the team API (`LIVE_API` in the page), and the API always allows that origin (CORS). One-time setup by a repo admin: Settings > Pages > Source: GitHub Actions.

To change the landing page, edit `frontend/landing.html` here and push to `main`. With the auto-deploy timer installed, the live site updates within about a minute; otherwise run `redeploy.sh`. Data changes still need a pipeline run (see Reloading data).

### Current state (2026-10-04)
- Slice (`backend/pipeline/config/slice.yaml`): lysosomal storage diseases, deep on the NCLs, plus primary mitochondrial disease (incl. Leigh syndrome, `MONDO:0009723`) and developmental and epileptic encephalopathies (incl. Dravet syndrome). Static sources include MedlinePlus Genetics for curated plain summaries. Reconcile tier 3 matches names by embeddings (#12), Orphanet prevalence lands in disease `attrs.prevalence` (#11), and Orphanet gene evidence links to orpha.net with the cited PMIDs kept in `attrs.orphanet_pmids` (daf3cdc). After changing any of this, reload the server's data (below); `GET /api/v1/meta` shows which dataset version is live.
- Real dataset loaded with the hardened pipeline. For the current dataset version and counts (nodes by type, edges, evidence, clusters, focus diseases) see `GET /api/v1/meta`; they change with every load, so they are not kept here.
- `check_contract.py` passes (OK, 0 violations). Path strength has one rule: `analytics/paths.py` (`path_strength`) takes the weakest edge, lowered by `attrs.status_cap`/`confidence_cap`, and stores it in `paths.weakest_status/min_confidence` (migration 0004). `/paths` and the views serve that value. The checker accepts the edge minimum, or the cap when one is set, and never anything stronger.
- `curated.yaml` holds the hand-curated NCL facts: patient groups (e.g. the Gray Foundation for CLN6), `ATLAS:mech-*` mechanisms (e.g. `ATLAS:mech-er-to-golgi-transfer-of-lysosomal-enzymes`, `-subunit-c-storage`, `-lysosomal-bmp-synthesis`), assets (DEM-CHILD registry, natural history studies, CLN5/CLN3/CLN6 animal and cell models) and therapies. Re-run `--verify` after adding quotes. PubMed quotes use NCBI efetch URLs, because pubmed.ncbi.nlm.nih.gov shows scripts a cookie wall.
- Frontend prototype prompt with verified real demo ids: `frontend/PROTOTYPE_PROMPT.md` (ports `frontend/landing.html`, the only landing page).
- Demo-path evidence review: the automated pre-check for #13 is done; a human sign-off is still open (researcher edges marked `literature` but derived by algorithm, no contradicting evidence on the CLN5 path).
- `.env` has only the Bright Data **Scraping Browser** credentials:
  - No `OPENAI_API_KEY`, so `/explain` and `/outreach-draft` return 503 `upstream_unavailable`; there is no template fallback. TODO(decision API-01): a keyless fallback for `/explain` is pending.
  - Gap-search jobs fail with "BRIGHTDATA_API_KEY missing" (SERP key).
  - Extract, reconcile and `brightdata_orgs` have not run on the server.
- The team frontend app is not deployed yet (`FRONTEND_REPO` empty), so only the landing page and the API are public.

## Trigger word: `återgå`
If a user message is exactly `återgå` (nothing else), read `docs/RESUME.md`. Status on 2026-10-04: the Atlas expansion (§2) is merged and its dataset is live. The landing polish (§1) is **not** on `main` yet: main's `landing.html` came from `frontend/landing-2-live`, so the polish is being redone on top of it (branch `polish/resume`, Daniel's session). The OpenAI section (§3) is blocked on #7. Check `git log origin/main` and the open issues before starting, and don't redo work that is already merged.

## Demo-critical facts
- Slice: lysosomal storage diseases, deep on the NCLs (Batten). The hero is an NCL subtype without approved therapy, picked at hour 1 (fixtures use CLN5). The counterexample is CLN2 enzyme replacement (soluble enzyme) vs. membrane-protein subtypes.
- The demo journey and the 1-minute script are in `docs/PLAN.md`. Every edge on the demo path must be checked by a human before the data freeze (hour 16).
