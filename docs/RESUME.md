# Resume the paused work

Trigger: a user message that is exactly **`återgå`**. Then read this file and do the work below, in order, without asking again. Any team member's Claude Code session can do it; all the work is on GitHub.

Paused 2026-10-04 by Daniel. Two things were in flight: a polish pass on the landing page and an expansion of the Atlas data.

## Rules that apply to every step
- The rules in `CLAUDE.md` still apply: the contract is law, every edge has evidence, honest statuses, the LLM rephrases and never invents, and no secrets in code.
- **Nothing fake on the page.** Every name, number, status and source comes live from the API. The only static content allowed is explanatory copy and the 10x model's step durations, labelled as assumptions.
- **Free and open.** No logins, no paywalls, no accounts. Personal data (plans, the journey log) stays in the visitor's browser. Contributions go through `POST /submissions` and are reviewed first.
- **Style.** Professional, warm and human. Sentence case, no all-caps, no em dashes, no middots, plain numbers. Families come first in Reader mode; Researcher mode gets the detail. Never cluttered.
- **Testing.** Use headless browsers only (Playwright `channel="chrome", headless=True`), never visible tabs. Check for console errors and look at screenshots at 1440 and 390 px.
- **No machine settings.** Don't change launchd services, databases or tunnels on anyone's machine unless its owner asks.
- The page is one file: `frontend/landing.html`. To test against live data from anywhere, open it with `?api=https://<team API host>/api/v1`. The page also falls back to the team API on its own when its host's API is down.

## 1. Finish the landing page polish (branch `wip/polish`)
Start from `origin/wip/polish`. It is `main` plus:
- one finished commit: clinical studies hub with screening and matching, live communities for families, plan step reasons, a journey log, and inline Explain buttons
- one WIP commit from the middle of stage (a) below

Do these stages one after another, then test, repair and merge to `main`:

**a. Everything live.**
- **#journey:** rebuild from live CLN5 data (`MONDO:0009745`, using its action view and `/paths`). Each step shows its real source and opens the evidence drawer.
- **#evidence:** the chain rows are real edges. The route finder uses `/search` and `/paths`, and shows an honest "no supported route" state.
- **#sources:** comes from `/meta`.
- **#tenx:** keep the copy and the labelled durations, but look up every named asset, group and mechanism live, with its status.
- **Remove all sample data:** the "Sample examples" row, the sample plans, the old `DATA` object and every code path only it used.
- **Hero eyebrow:** replace it with a live line from `/meta`, for example "Live: 195 rare diseases, 10,395 sourced connections, updated 4 October".
- **Hero graphic:** redesign it as a clear, clickable path: Disease, Mechanism, Community, Asset, Collaborator, Shared action. Each node scrolls to its section.

**b. Plain trust language and decluttering.**
- **Status labels:** replace Evidence, Hypothesis, Unknown, "None recorded" and "94% (high)" with one shared function.
  - Reader mode: "Shown in published research (source)", "A possible link, not yet tested", "We don't know yet" plus what would settle it, "Nothing we found says otherwise", and Strong, Moderate or Weak.
  - Researcher mode: the exact status, the method and the number.
  - Use the same symbol (solid, dashed or dotted) everywhere.
- **Declutter** the whole page per audience: no repetition, simpler words.
- **Footer:** "One Mission is free and open, with no account needed. It connects published research and public databases so families and researchers can find each other, and every link shows its source. Built with OpenAI. Not medical advice: talk to your care team about any decision."
- **Hero:** add the line "Free and open for everyone. No account needed."

**c. New features.**
- **Atlas map (#map):**
  - Clusters from `/clusters` drawn as constellations. Zoom in step by step: cluster, then its diseases (`/clusters/{id}`), then a disease's relatives (`/diseases/{id}/similar`, with caution flags). Links that only share symptoms are drawn weaker than links that share a mechanism.
  - Reader mode shows plain families. Researcher mode adds overlays and filters.
  - Provide a keyboard-accessible list alternative.
- **Built with OpenAI:**
  - A short section explaining Extract, Reconcile and Explain.
  - "Explain in plain language" buttons call `POST /explain` (audience `family` for Reader, `expert` for Researcher). Show the cited steps labelled "Explained by OpenAI". If the API answers 503, fall back to the guide bubble.
  - "Draft a message" calls `POST /outreach-draft` and offers copy to clipboard.
- **Back to top:** a button that appears after one screen of scrolling and sits above the guide orb without overlapping it, also when the panel is open or on mobile. It scrolls smoothly, or instantly with reduced motion, and has a 44px target.
- **Study submission in #studies:** "Run a study? Add it here", a form that needs no account and posts to `/submissions`. It says that submissions are reviewed before they appear.

**d. Accessibility.**
- Run axe-core at 1440 and 390 px, in both modes and with the guide open. Fix every serious or critical violation against WCAG 2.2 AA.
- Apply the relevant rules from Vercel's Web Interface Guidelines (https://github.com/vercel-labs/web-interface-guidelines).

**Then:**
- Delete `frontend/landing-2.html` so the repo has one page.
- Point the references in `frontend/PROTOTYPE_PROMPT.md` to `frontend/landing.html`.
- Merge to `main` and push. The live site picks up `main`.

## 2. Finish the Atlas expansion (branch `wip/atlas-expand`)
Start from `origin/wip/atlas-expand`. Its 4 commits started:
- new disease families in `backend/pipeline/config/slice.yaml`: primary mitochondrial disease (including Leigh syndrome) and developmental and epileptic encephalopathies (including Dravet syndrome and SCN1A), alongside the NCL slice
- a free curated plain-summary source (MedlinePlus Genetics)
- better Bright Data lead finding

Finish and verify it:
- **Pipeline tests:** they must pass.
- **Bright Data:** at most 400 requests. Leads stay `attrs.unverified` with `hypothesis` edges, never literature.
- **Clinical trials, PubMed and RePORTER:** must cover the new focus diseases.
- **Full pipeline run:** ingest, analytics, views and load into a scratch database. Run `check_contract.py` (strict), and include Leigh syndrome and Dravet syndrome in `--ids`.
- **Review:** sample 40 new edges to check that their statuses are honest. Make sure the organization leads are real organizations.
- **Merge:** merge to `main`, then reload the team server's dataset. The pipeline command is in `CLAUDE.md` under "Reloading data".
- **Landing page:** once Leigh and Dravet are in the live API, they become normal live examples. They must never be labelled "sample".

## 3. Use OpenAI (needs `OPENAI_API_KEY` in `backend/.env` and `backend/pipeline/.env`)
The remaining credit is about 9 USD, so set a **hard cap of 7 USD** and use `gpt-4.1-mini`. Track tokens on every call, and stop gracefully at the cap or on `insufficient_quota`, keeping what is done. In order:
1. **Precompute `/explain` answers** for the demo paths and connections, and insert them into the `explanations` table after `load`. The API serves cached answers without a key, but `load` clears that table, so insert after loading.
2. **`atlas-pipeline extract` and `reconcile`:** quotes must be exact substrings of the abstract, and the results are literature edges.
3. **Plain summaries for families**, rewritten only from curated text (MONDO definitions, MedlinePlus). Never invent: every fact must be in the input, and each summary cites its source.

Report the actual spend. The team server needs the key in its own `backend/.env` before the live explain and draft features work there.

## Not in the repo (Daniel's machine)
These items depend on credentials only Daniel has. If you are not Daniel, skip them and say so:
- the ElevenLabs agent's knowledge base and prompt
- the PowerPoint in `~/hacknation/deck`
- the local launchd and tunnel setup
