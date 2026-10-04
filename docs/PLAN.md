# Plan: 24 hours, 2 backend + 2 frontend in parallel

Challenge 05, **AI Atlas for the World's Rare Diseases** (brief: `docs/brief.pdf`). Architecture: `docs/ARCHITECTURE.md`. The bridge: `contract/`.

## The goal in one sentence
Maria types her disease into one box and walks a **cited** path: shared mechanism → related disease → its patient group → a reusable registry → a sourced message she can send this week. When no supported path exists, the Atlas says so and names the next question to test.

## Scope decisions (made, don't re-litigate)
- **Slice:** lysosomal storage diseases, deep on the **neuronal ceroid lipofuscinoses** (NCL / Batten; CLN1–CLN14). Why: many genes, one mechanism family, an approved therapy for one subtype (CLN2, enzyme replacement) that works as a natural **counterexample** for others, and active patient groups and registries.
- **Hero:** a CLN subtype **without** an approved therapy, chosen at hour 1 by the backend from real data density (candidates: CLN5, CLN6, CLN3, CLN7). Fixtures use CLN5.
- **Honest-gap case:** a second disease with no exact patient group, used for Devon's story.
- **Everything precomputed**; live LLM calls only for "explain", "draft message" and the optional web gap-search.
- **OpenAI** for extract, reconcile and explain (prize eligibility). **Bright Data** for the patient-org layer and live gap-search.

## Roles

| | Person | Owns | Never touches |
|---|---|---|---|
| **B1** | Data & Graph | `backend/pipeline/`: ingest of biology + literature, OpenAI extraction, reconcile, analytics (IC, similarity, counterexamples, Leiden, paths), views, load | frontend repo |
| **B2** | Platform & Services | `backend/api/`, `infra/` (L4/L7 LB, compose, deploy), `backend/db/`, AI endpoints, worker/Bright Data, **plus** ingest of people & assets (ClinicalTrials.gov, RePORTER, Bright Data orgs) from hour 8 | frontend repo |
| **F1** | Journey & Product | pages, cards, copy, Family/Expert mode, demo video | `src/contract`, backend |
| **F2** | Graph & Data layer | `src/api/` (http + mock), encoding, graph canvas, evidence drawer, explorer, debug footer | `src/contract`, backend |

**Contract owner pair:** B2 + F2 approve every `contract:` PR (5-minute async review).

## Timeline

Hours are from the start. ⟂ marks an integration checkpoint: 10 minutes, all four people, demo what works.

| Hour | B1 Data & Graph | B2 Platform & Services | F1 Journey | F2 Graph & Data layer |
|---|---|---|---|---|
| **0–1** | **Everyone:** read this plan + `contract/README.md`, set up accounts (Supabase, OpenAI, Bright Data, Oracle/Render), create `.env` files, agree on the hero disease candidate. | | | |
| 1–2 | MONDO + HPO ingest for the slice; check data density for the hero candidates → **pick hero** | Supabase project, run migrations; API in `DATA_MODE=fixtures` running locally | frontend repo + `FRONTEND_SPEC.md` + Prompt 1 | sync contract, `src/api/` http + mock + hooks |
| **2 ⟂** | **Contract v1 frozen.** Backend shares a public dev URL (cloudflared tunnel) serving fixtures. | | | |
| 2–6 | HGNC, Orphanet, ClinVar, Reactome; IC + similarity; first `load` into Supabase | compose stack: L4 → L7×2 → API×3 + Redis; `lb-demo`; endpoints switched to `db` as data lands; `check_contract.py` | Home, Search, action-view sections 1–4 on mocks | encoding.ts + primitives, evidence drawer, graph canvas |
| **6 ⟂** | Search + node + neighborhood + similar work on **real data** through the LB. Frontend points `VITE_API_BASE` at it. | | | |
| 6–12 | PubMed fetch + OpenAI extraction → `literature` edges with quotes; counterexample detection; Leiden clusters | `/explain` + `/outreach-draft` (OpenAI, cached, rate-limited); worker + Bright Data gap-search; start ClinicalTrials.gov + RePORTER ingest | action view 5–8, mechanism view, outreach + gap-search UI | path stepper + path highlight in the graph, explorer filters, cluster page |
| **12 ⟂** | **Hero action view is real end to end**, including evidence drawer and explain. List everything still on fixtures. | | | |
| 12–16 | paths + views (ActionView/MechanismView) for the hero + gap disease + 3 more; quality pass on edges | Bright Data patient orgs + registries → nodes/edges; network-overlap people; deploy to cloud (or the tunnel fallback) | Family/Expert copy pass, honesty copy, mobile | live-data edge cases, performance, error states |
| **16 ⟂** | **Data freeze.** Final `load`; `export-fixtures` → snapshot for the offline fallback. | | | |
| 16–20 | Validate demo-path evidence by hand (read every quote on the path); README dataset reproduction section | production deploy: `web` serves the frontend build behind the LB; load test + `lb-demo`; README architecture | polish the demo journey; write the video script | debug footer (X-Served-By), final bugs; production build handed to B2 |
| **20 ⟂** | **Feature freeze.** Full dress rehearsal of the 1-minute walkthrough on the production URL **and** on `?mock=1` snapshot. | | | |
| 20–24 | 10× case slide + numbers | uptime watch, backups of the dump | record walkthrough + team video | support recording, fix only blockers |

## Integration rules (how we avoid blocking each other)
1. **Hour 0 → 2:** both sides build against fixtures. Nothing waits on anything.
2. **From hour 2** the backend always has *some* public URL answering every endpoint (fixtures first, then real data). The frontend switches with one env var. `?mock=1` stays available.
3. A missing endpoint or field is a `CONTRACT_REQUESTS.md` entry (frontend) or a contract PR (backend). Never a quiet workaround.
4. Backend runs `make contract-check BASE_URL=…` before telling the frontend that something is "live".
5. Merge small and often. Each side only touches its own repo/folders.

## The 1-minute walkthrough (what we are building toward)
1. *(0:00)* "Maria's child has CLN5 disease. No approved treatment. She knows one gene name." She types "batten".
2. *(0:10)* Action view: *"No approved treatment yet, but your disease shares a pathway with 3 others."* The connections stepper shows CLN5 disease → lysosomal mechanism → CLN6 disease → a patient group. Each edge carries a status chip.
3. *(0:22)* Click an edge: the quote from the paper, the source, the date, one contradicting finding. Then "Explain in plain language", with every sentence cited.
4. *(0:32)* Caution callout: CLN2 has an approved enzyme therapy, but the mechanism differs, so don't assume it transfers. *"Here is what must be checked."*
5. *(0:40)* An existing registry ("adaptable"), a researcher who bridges two communities, and a next step: "Draft message" produces a sourced proposal to join forces.
6. *(0:52)* Switch to a disease with no supported route: the honest gap, what we searched, and the next question to test.
7. *(0:58)* Footer: `served by api-2`. "Every request is load-balanced across stateless replicas."

## The 10× case (for judges)
- **Milestone:** a shared natural-history study or registry across 2+ related NCL communities.
- **Today**, for a typical small group: finding related communities and assets happens by word of mouth and cold outreach, often taking months to over a year. Judging whether another disease's registry or study design applies needs scarce expert time.
- **With the Atlas:** related communities, assets and contacts with cited evidence in minutes. A sourced proposal the same week. Expert time goes only to the explicit `needs_review` questions.
- **Assumptions to state honestly:** the speed-up comes from discovery and preparation, not from biology or regulation. Coverage depends on public sources. Every link still needs expert validation, and the Atlas shrinks that to a checklist.
- **What needs validation next:** recall of the patient-group layer, expert review of the similarity and counterexample rules, and a pilot with one real patient organization.

## Judging criteria → where we score

| Criterion | Our answer |
|---|---|
| Graph quality | Typed 12-node / 23-edge schema, IC-weighted phenotype similarity, mechanism-aware Leiden clusters, explicit counterexamples (`caution`), precomputed explainable paths |
| Evidence integrity | Status on every edge (curated / literature / inferred / hypothesis); quotes and sources on every edge; contradicting evidence surfaced; the loader refuses edges without evidence |
| Patient progress | The action view ends in next steps with a sourced outreach draft; the honest gap state with the next question |
| 10× impact | The milestone, timeline comparison and assumptions above |
| Ambition & craft | Persona-specific views, Family/Expert mode, low-ink design, production-style LB architecture with horizontal scaling |

## Submission checklist
- [ ] Production URL (behind LB) + `?mock=1` snapshot works offline
- [ ] README: architecture, how to run (`make up`), **how to reproduce the dataset** (`backend/pipeline` `make all`)
- [ ] Team video + 1-minute walkthrough
- [ ] `.env` secrets not in git (`git grep -i "sk-"` returns nothing)

## Risks and mitigations

| Risk | Mitigation |
|---|---|
| Real data is thin for the hero | Pick the hero at hour 1 from data density; fixtures keep the frontend unblocked |
| OpenAI / Bright Data rate limits or outage | Everything precomputed + cached; deterministic fallbacks; snapshot mode |
| Cloud deploy eats hours | cloudflared tunnel from the laptop is the fallback public URL; `make up` locally satisfies "easy to run locally" |
| LLM hallucinated edges | Quotes required; extraction validated against the source text; status `literature`/`hypothesis`, never `curated`; hand-check every edge on the demo path |
| Contract churn | v1 frozen at hour 2; additive changes only; 5-minute PR protocol |
