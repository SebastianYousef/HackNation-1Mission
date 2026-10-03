"""Build the composite screens of contract/atlas.ts:
  ActionView    for every focus disease            (Maria / Devon)
  MechanismView for mechanisms linked to >= N focus diseases (Priya / Dr. Osei)
Everything is assembled deterministically from the graph; the LLM (llm.rephrase) only rewrites the
headline / plain summary from the same facts, with a template fallback. Writes views.jsonl."""
from __future__ import annotations

import logging
from collections import defaultdict
from typing import Any

from pydantic import BaseModel

from . import llm
from .analytics.overlap import person_disease_links
from .analytics.paths import counterexample_therapy, other_subtype_therapy
from .config import slice_config
from .graph import Graph, load_graph
from .config import INTERIM
from .store import read_json, read_jsonl, write_jsonl

log = logging.getLogger(__name__)
STATUS_RANK = ["curated", "literature", "inferred", "hypothesis"]
GROUP_SUBTYPES = {"patient_group", "foundation"}
REUSE = ["direct", "adaptable", "reference_only"]
# has_supported_route needs a lead linked to exactly this disease by curated or corroborated evidence:
# curated = 0.9; literature = 0.5 for one source, >= 0.6 once two independent sources agree
SUPPORTED_MIN_CONF = 0.6
COVERAGE_FILES = {"PubMed": "coverage_pubmed", "ClinicalTrials.gov": "coverage_clinicaltrials",
                  "NIH RePORTER": "coverage_nih_reporter"}


def _rows_or_empty(name: str) -> list[dict]:
    return list(read_jsonl(name)) if (INTERIM / name).exists() else []


# ---------------------------------------------------------------- JSON shapes (mirror the SQL helpers)
def brief(g: Graph, nid: str) -> dict:
    n = g.nodes[nid]
    return {"id": n["id"], "type": n["type"], "subtype": n.get("subtype"), "label": n["label"],
            "summary": n.get("plain_summary")}


def full(g: Graph, nid: str) -> dict:
    n = g.nodes[nid]
    return brief(g, nid) | {"description": n.get("description"), "synonyms": list(n.get("synonyms") or []),
                            "xrefs": n.get("xrefs") or {}, "attrs": n.get("attrs") or {}, "url": n.get("url")}


def edge_json(g: Graph, eid: str) -> dict:
    e, ev = g.edges[eid], g.evidence[eid]
    return {"id": e["id"], "src": e["src"], "dst": e["dst"], "type": e["type"], "label": e.get("label"),
            "status": e["status"], "confidence": e["confidence"], "score": e.get("score"),
            "support_count": sum(v["stance"] == "supports" for v in ev),
            "contradict_count": sum(v["stance"] == "contradicts" for v in ev),
            "sources": sorted({v["source_name"] for v in ev}), "attrs": e.get("attrs") or {}}


class Headline(BaseModel):
    headline: str
    plain_summary: str


class Index:
    """Adjacency lookups by (node, edge type)."""

    def __init__(self, g: Graph):
        self.g = g
        self.out: dict[tuple[str, str], list[dict]] = defaultdict(list)   # (src, type) -> edges
        self.inc: dict[tuple[str, str], list[dict]] = defaultdict(list)   # (dst, type) -> edges
        for e in g.edges.values():
            self.out[(e["src"], e["type"])].append(e)
            self.inc[(e["dst"], e["type"])].append(e)

    def src_of(self, dst: str, t: str) -> list[dict]:
        return self.inc[(dst, t)]

    def dst_of(self, src: str, t: str) -> list[dict]:
        return self.out[(src, t)]

    def similar(self, d: str) -> list[tuple[str, dict]]:
        es = [(e["dst"], e) for e in self.out[(d, "disease_similar_to")]] + \
             [(e["src"], e) for e in self.inc[(d, "disease_similar_to")]]
        return sorted(es, key=lambda x: (-(x[1].get("score") or 0), x[0]))

    def ancestors(self, d: str, depth: int = 2) -> list[tuple[str, list[str]]]:
        """Broader diseases up the MONDO hierarchy (disease_subtype_of, child -> parent), nearest first,
        each with the subtype edge ids that lead there."""
        out: list[tuple[str, list[str]]] = []
        seen, level = {d}, [(d, [])]
        for _ in range(depth):
            nxt = []
            for x, chain in level:
                for e in sorted(self.dst_of(x, "disease_subtype_of"), key=lambda e: e["dst"]):
                    if e["status"] != "hypothesis" and e["dst"] not in seen:
                        seen.add(e["dst"])
                        nxt.append((e["dst"], chain + [e["id"]]))
            out += nxt
            level = nxt
        return out


# ---------------------------------------------------------------- cards
def org_card(ix: Index, e: dict) -> dict:
    g, o = ix.g, ix.g.nodes[e["src"]]
    a = o.get("attrs") or {}
    return {"node": brief(g, o["id"]), "for_disease": brief(g, e["dst"]), "edge_id": e["id"],
            "website": a.get("website") or o.get("url"), "contact_url": a.get("contact_url"),
            "country": a.get("country"), "has_registry": a.get("has_registry")}


def groups_for(ix: Index, d: str) -> list[dict]:
    es = [e for e in ix.src_of(d, "organization_serves_disease")
          if ix.g.nodes[e["src"]].get("subtype") in GROUP_SUBTYPES]
    return [org_card(ix, e) for e in sorted(es, key=lambda e: (-e["confidence"], e["src"]))]


def person_card(ix: Index, pid: str, links: dict[str, set[str]]) -> dict:
    g = ix.g
    a = g.nodes[pid].get("attrs") or {}
    pubs = len(ix.dst_of(pid, "person_authored"))
    grants = len(ix.src_of(pid, "grant_funds_person"))
    roles = a.get("roles") or [g.nodes[pid].get("subtype") or "researcher"]
    return {"node": brief(g, pid), "affiliation": a.get("affiliation"),
            "roles": [r for r in roles if r in {"researcher", "clinician", "industry", "funder", "investor"}]
            or ["researcher"],
            "works_on": [brief(g, d) for d in sorted(links)[:6]], "recent_publications": pubs,
            "active_grants": grants, "contact_url": a.get("contact_url"),
            "edge_ids": sorted({x for s in links.values() for x in s})[:12]}


def trial_card(ix: Index, e: dict, relevance: str, note: str | None) -> dict:
    g, t = ix.g, ix.g.nodes[e["src"]]
    a = t.get("attrs") or {}
    ivs = ix.dst_of(t["id"], "trial_tests_intervention")
    return {"node": brief(g, t["id"]), "nct_id": a.get("nct_id") or t["id"], "phase": a.get("phase"),
            "overall_status": a.get("overall_status"),
            "intervention": ", ".join(g.nodes[x["dst"]]["label"] for x in ivs) or a.get("intervention"),
            "conditions": [brief(g, x["dst"]) for x in ix.dst_of(t["id"], "trial_studies_disease")][:5],
            "relevance": relevance, "eligibility_note": note,
            "url": t.get("url") or f"https://clinicaltrials.gov/study/{a.get('nct_id') or t['id']}",
            "edge_ids": [e["id"]] + [x["id"] for x in ivs]}


def asset_card(ix: Index, aid: str, d: str, related: set[str], family: dict[str, list[str]] | None = None) -> dict:
    """family = {broader disease: subtype edge ids}: an asset built for d's broader family is adaptable."""
    g, family = ix.g, family or {}
    cov_es = sorted(ix.dst_of(aid, "asset_covers_disease"), key=lambda x: x["dst"])
    own_es = sorted(ix.src_of(aid, "organization_maintains_asset"), key=lambda x: x["src"])
    covers = [x["dst"] for x in cov_es]
    owner = own_es[0]["src"] if own_es else None
    label = g.nodes[d]["label"]
    fam = [c for c in covers if c in family]
    reuse = "direct" if d in covers else ("adaptable" if fam or related & set(covers) else "reference_only")
    if reuse == "direct":
        differs, review = [], []
    elif fam:
        differs = [f"Built for {g.nodes[fam[0]]['label']}, the broader group that includes {label}"]
        review = [f"Does it already enrol {label} families, and are they recorded under that diagnosis?"]
    else:
        differs = [f"Built for {', '.join(g.nodes[c]['label'] for c in covers[:3])}, not {label}"]
        review = [f"Do eligibility criteria and outcome measures fit {label} patients?",
                  "Does the owner accept families with a related diagnosis?"]
    eids = [x["id"] for x in cov_es] + [x["id"] for x in own_es] + \
        sorted({x for c in fam for x in family[c]})
    return {"node": brief(g, aid), "asset_kind": g.nodes[aid].get("subtype") or "registry",
            "owner": brief(g, owner) if owner else None, "covers": [brief(g, c) for c in covers[:6]],
            "reusability": reuse, "what_differs": differs, "needs_review": review, "edge_ids": eids}


def path_json(g: Graph, p: dict) -> dict:
    """weakest_status / min_confidence come from the edges, lowered by the path's honesty caps
    (attrs.status_cap / confidence_cap, set in analytics/paths.py): a route that hinges on one shared
    symptom or on a therapy transfer is never shown as stronger than inferred / hypothesis."""
    es = [g.edges[x] for x in p["edge_ids"]]
    attrs = p.get("attrs") or {}
    weakest = max([e["status"] for e in es] + ([attrs["status_cap"]] if attrs.get("status_cap") else []),
                  key=STATUS_RANK.index)
    min_conf = min([e["confidence"] for e in es] +
                   ([attrs["confidence_cap"]] if attrs.get("confidence_cap") is not None else []))
    return {"id": p["id"], "kind": p["kind"], "title": p["title"], "score": p["score"], "from": p["from_id"],
            "to": p["to_id"], "node_ids": p["node_ids"], "edge_ids": p["edge_ids"],
            "nodes": [brief(g, n) for n in p["node_ids"]], "edges": [edge_json(g, x) for x in p["edge_ids"]],
            "weakest_status": weakest, "min_confidence": min_conf, "attrs": attrs}


# ---------------------------------------------------------------- ActionView
def action_view(ix: Index, d: str, paths: list[dict], overlap: list[dict], plinks) -> dict:
    g = ix.g
    dn, label = g.nodes[d], g.nodes[d]["label"]
    allsims = ix.similar(d)
    # a caution that d's own approved therapy may not transfer to the other disease is about the other
    # disease's families, not d's: it does not pull extra communities, gaps or experiments into d's view
    own_tx = {e["id"] for _, e in allsims if (e.get("attrs") or {}).get("caution_kind") == "approved_therapy"
              and (e.get("attrs") or {}).get("therapy_for") == d}
    # top 5 plus every counterexample: a look-alike with a different mechanism must always be visible
    sims = allsims[:5] + [x for x in allsims[5:] if (x[1].get("attrs") or {}).get("caution") and x[1]["id"] not in own_tx]
    related = {o for o, _ in sims}
    family = dict(ix.ancestors(d))   # broader disease -> subtype edge ids leading there

    tx = ix.src_of(d, "intervention_treats_disease")
    approved = [e for e in tx if (e.get("attrs") or {}).get("approval") == "approved"]
    has_tx = True if approved else (False if tx else None)
    tx_note = (f"Approved: {', '.join(g.nodes[e['src']]['label'] for e in approved)}" if approved else
               "No approved treatment found in the sources we checked." if tx is not None else "")

    exact = groups_for(ix, d)
    # groups serving a broader disease (e.g. every NCL) also serve d's families: shown as that family's
    # community (never inside exact_groups), cited through the disease_subtype_of chain
    umbrella: list[tuple[str, list[str], list[dict]]] = []
    seen_org = {o["node"]["id"] for o in exact}
    for p, chain in ix.ancestors(d):
        gs = [o for o in groups_for(ix, p) if o["node"]["id"] not in seen_org]
        seen_org |= {o["node"]["id"] for o in gs}
        if gs:
            umbrella.append((p, chain, gs))
    mech_d = {e["dst"] for e in ix.dst_of(d, "disease_involves_mechanism") if e["status"] != "hypothesis"}
    gene_d = {e["src"] for e in ix.src_of(d, "gene_associated_with_disease")}
    fam_comms = []
    for p, chain, gs in umbrella:
        if len(chain) > 1:   # a grandparent: one similarity_edge_id cannot cite the chain; see the steps
            continue
        pl = g.nodes[p]["label"]
        fam_comms.append({"disease": brief(g, p), "similarity": min(g.edges[x]["confidence"] for x in chain),
                          "similarity_edge_id": chain[0],
                          "why": f"{label} is a form of {pl}, so groups for the whole {pl} family also serve its families.",
                          "differences": [f"These groups serve every form of {pl}, not only {label}"],
                          "caution": None, "groups": gs[:3]})
    sim_comms = []
    fam_ids = {c["disease"]["id"] for c in fam_comms}
    for o, e in sims:
        if o in fam_ids:   # a parent that is also a look-alike is shown once, as its family
            continue
        comp = (e.get("attrs") or {}).get("components", {})
        ph = comp.get("phenotype")   # absent = not scored (too few annotated symptoms), not "low"
        mech_o = {x["dst"] for x in ix.dst_of(o, "disease_involves_mechanism") if x["status"] != "hypothesis"}
        gene_o = {x["src"] for x in ix.src_of(o, "gene_associated_with_disease")}
        why = []
        if gene_d & gene_o:
            why.append("the same gene (" + ", ".join(g.nodes[x]["label"] for x in sorted(gene_d & gene_o)) + ")")
        if mech_d & mech_o:
            why.append("pathways (" + ", ".join(g.nodes[x]["label"] for x in sorted(mech_d & mech_o)[:2]) + ")")
        if ph is not None and ph >= 0.2:
            why.append("an overlapping pattern of informative symptoms")
        diffs = []
        if gene_o - gene_d:
            diffs.append("Different gene: " + ", ".join(g.nodes[x]["label"] for x in sorted(gene_o - gene_d)[:3]))
        if mech_o and mech_d and not (mech_o & mech_d):
            diffs.append("No shared known pathway")
        if ph is None:
            diffs.append("Too few symptoms recorded to compare them")
        elif ph < 0.2:
            diffs.append("Symptoms overlap only weakly")
        sim_comms.append({"disease": brief(g, o), "similarity": e.get("score") or 0.0, "similarity_edge_id": e["id"],
                          "why": f"{label} and {g.nodes[o]['label']} share " + (" and ".join(why) or "some features") + ".",
                          "differences": diffs, "caution": (e.get("attrs") or {}).get("caution"),
                          "groups": groups_for(ix, o)[:3]})
    communities = fam_comms + sim_comms

    my_paths = [p for p in paths if p["from_id"] == d]
    connections = []
    for kind in ("patient_group", "related_disease", "asset", "researcher", "trial", "intervention"):
        connections += [path_json(g, p) for p in my_paths if p["kind"] == kind][:2]

    asset_ids = sorted({e["src"] for x in {d} | related | set(family) for e in ix.src_of(x, "asset_covers_disease")})
    assets = sorted((asset_card(ix, a, d, related, family) for a in asset_ids),
                    key=lambda c: (REUSE.index(c["reusability"]), c["node"]["id"]))[:6]

    # an interventional trial that studies a counterexample (e.g. CLN2 enzyme replacement for CLN5) is never
    # suggested unless it also studies d itself; registries and natural-history studies stay
    # (same rules as analytics/paths.py)
    partners = {o for o, e in allsims if (e.get("attrs") or {}).get("caution")}
    genes_of = lambda x: {e["src"] for e in ix.src_of(x, "gene_associated_with_disease") if e["status"] != "hypothesis"}  # noqa: E731
    conds = lambda e: {x["dst"] for x in ix.dst_of(e["src"], "trial_studies_disease") if x["status"] != "hypothesis"}  # noqa: E731
    therapy = lambda e: g.nodes[e["src"]].get("subtype") == "interventional"  # noqa: E731
    ok = lambda e: not counterexample_therapy(d, conds(e), therapy(e), partners)  # noqa: E731
    # filed under d's broader family but naming only other subtypes (CLN2 gene therapy under "late infantile NCL")
    for_d_family = lambda e: ok(e) and not other_subtype_therapy(  # noqa: E731
        d, conds(e), therapy(e), [genes_of(x) for x in conds(e)], genes_of(d))
    trials = [trial_card(ix, e, "same_disease", None) for e in ix.src_of(d, "trial_studies_disease")]
    for p, chain in family.items():
        for e in [e for e in ix.src_of(p, "trial_studies_disease") if for_d_family(e)][:3]:
            pl = g.nodes[p]["label"]
            c = trial_card(ix, e, "related_disease",
                           f"Tests a therapy in {pl}, the broader group that includes {label}, without naming {label}; "
                           "whether it applies is unproven." if therapy(e) else
                           f"Studies {pl}, the broader group that includes {label}; check eligibility.")
            c["edge_ids"] += chain   # the subtype chain backs "the broader group that includes"
            trials.append(c)
    for o, _ in sims:
        if o not in partners:
            trials += [trial_card(ix, e, "related_disease", f"Studies {g.nodes[o]['label']}; check eligibility for {label}.")
                       for e in ix.src_of(o, "trial_studies_disease") if ok(e)]
    seen, uniq = set(), []
    for t in trials:
        if t["node"]["id"] not in seen:
            seen.add(t["node"]["id"])
            uniq.append(t)
    trials = uniq[:8]

    people = [(pid, dm) for pid, dm in plinks.items() if d in dm]
    people.sort(key=lambda x: (-(len(x[1][d]) + 0.1 * len(x[1])), x[0]))
    researchers = [person_card(ix, pid, dm) for pid, dm in people[:8]]
    shared = [{"person": person_card(ix, r["person"], plinks[r["person"]]),
               "communities": [brief(g, x) for x in r["diseases"][:6]], "edge_ids": r["edge_ids"][:12]}
              for r in overlap if d in r["diseases"]][:5]

    steps: list[dict] = []
    stepped: set[str] = set()   # one contact_group step per organisation

    def step(kind, title, rationale, target, status, validation, effort, eids):
        if kind == "contact_group":
            stepped.add(target)
        steps.append({"id": f"S:{d}:{len(steps) + 1}", "kind": kind, "title": title, "rationale": rationale,
                      "target": brief(g, target) if target else None, "status": status,
                      "validation_needed": validation, "effort": effort, "edge_ids": eids})
    if exact:
        o = exact[0]
        step("contact_group", f"Contact {o['node']['label']}", f"They support families with {label}.",
             o["node"]["id"], "viable", [], "this_week", [o["edge_id"]])
    # the nearest family-wide group when d has none of its own, and the nearest grandparent-wide group (it has
    # no related_communities entry); both cite the serves edge plus the whole subtype chain
    near = [umbrella[0]] if umbrella and not exact else []
    near += [u for u in umbrella if len(u[1]) > 1][:1]
    for p, chain, gs in near:
        o, pl = gs[0], g.nodes[p]["label"]
        if o["node"]["id"] in stepped:
            continue
        mid = g.nodes[g.edges[chain[0]]["dst"]]["label"]   # d's direct parent
        step("contact_group", f"Contact {o['node']['label']}",
             f"They support families across {pl}, which includes {label}." if len(chain) == 1 else
             f"They support families across {pl}; {label} is a form of {mid}, which is part of {pl}.",
             o["node"]["id"], "viable",
             [f"Ask whether they already support {label} families"], "this_week", [o["edge_id"]] + chain)
    if not exact:
        step("build_missing_group", f"Start a community for {label}",
             "We found no patient group for exactly this diagnosis.", None, "viable", [], "this_month", [])
    for c in sim_comms[:2]:
        gs = [o for o in c["groups"] if o["node"]["id"] not in stepped]
        if gs:
            o = gs[0]
            step("contact_group", f"Ask {o['node']['label']} about joining forces",
                 c["why"], o["node"]["id"], "needs_review" if c["caution"] else "viable",
                 c["differences"] + ([c["caution"]] if c["caution"] else []), "this_week",
                 [c["similarity_edge_id"], o["edge_id"]])
    for a in assets[:2]:
        if a["reusability"] != "reference_only":
            step("join_registry" if a["asset_kind"] == "registry" else "adapt_study_design",
                 f"Explore reusing {a['node']['label']}", "An existing asset could save years of setup.",
                 a["node"]["id"], "viable" if a["reusability"] == "direct" else "needs_review",
                 a["needs_review"], "this_month", a["edge_ids"])
    if researchers:
        r = researchers[0]
        step("contact_researcher", f"Reach out to {r['node']['label']}",
             f"Active on {label} ({r['recent_publications']} papers, {r['active_grants']} grants in our sources).",
             r["node"]["id"], "viable", [], "this_month", r["edge_ids"][:5])
    # cautions about a transfer *to* d (not about d's own approved therapy reaching the other disease)
    transfer = [c for c in sim_comms if c["caution"] and c["similarity_edge_id"] not in own_tx]
    for c in transfer[:1]:
        step("validate_experiment", f"Test whether {label} and {c['disease']['label']} share the mechanism",
             c["caution"], None, "needs_review", ["Expert review of the mechanism difference"], "this_quarter",
             [c["similarity_edge_id"]])

    cov = []
    for name, f in COVERAGE_FILES.items():
        c = (read_json(f, {}) or {}).get(d)
        cov.append({"name": name, "checked": c is not None, "query": (c or {}).get("query"),
                    "result_count": (c or {}).get("result_count"), "checked_at": (c or {}).get("checked_at")})
    for name in ("HPO", "Orphanet", "MONDO"):
        cov.append({"name": name, "checked": True, "query": d, "result_count": None, "checked_at": None})
    # supported = a group, asset, trial or researcher linked to exactly d by curated or corroborated evidence.
    # Family-wide groups (umbrella), look-alikes and same-gene siblings are shown but are leads for d only
    # by extension, so they do not count.
    strong = lambda e: e["status"] in ("curated", "literature") and e["confidence"] >= SUPPORTED_MIN_CONF  # noqa: E731
    supported = any(strong(g.edges[o["edge_id"]]) for o in exact) \
        or any(a["reusability"] == "direct" for a in assets) \
        or any(strong(e) for e in ix.src_of(d, "trial_studies_disease")) \
        or any(strong(e) for e in ix.src_of(d, "person_studies"))
    gaps = []
    if not supported:
        gaps.append({"question": f"Who is actively working on {label}?",
                     "why_it_matters": f"Nothing in our sources links a group, study, asset or researcher to exactly "
                                       f"{label} with curated or independently confirmed evidence; the leads shown "
                                       "come through related diseases.",
                     "what_would_resolve": "Ask the clinicians who diagnose it; check registries and conference "
                                           "abstracts; contribute any group, study or researcher you know."})
    if not exact:
        gaps.append({"question": (f"Is there a group for exactly {label}, beyond the "
                                  f"{g.nodes[umbrella[0][0]]['label']}-wide groups?" if umbrella else
                                  f"Is there a patient group for {label} that our sources missed?"),
                     "why_it_matters": "Families need a community and researchers need a partner to recruit through.",
                     "what_would_resolve": "Ask clinicians and related groups; contribute any group you know."})
    if not mech_d:
        gaps.append({"question": f"Which biological pathway is disrupted in {label}?",
                     "why_it_matters": "Without a mechanism, related diseases can only be matched by symptoms.",
                     "what_would_resolve": "Functional studies of the gene product; expert curation."})
    for c in transfer[:1]:
        gaps.append({"question": f"Do findings from {c['disease']['label']} transfer to {label}?",
                     "why_it_matters": c["caution"],
                     "what_would_resolve": "Compare the disrupted step in cell or animal models."})

    facts = {"disease": label, "approved_treatment": has_tx, "exact_groups": len(exact),
             "related": [c["disease"]["label"] for c in sim_comms[:3]], "description": dn.get("description")}
    hl, _ = llm.rephrase("headline+plain_summary for a family's disease page", facts, Headline, lambda: Headline(
        headline=(f"{'No approved treatment found yet' if not has_tx else 'An approved treatment exists'}"
                  f" — {label} is connected to {len(sim_comms)} related diseases"),
        plain_summary=dn.get("plain_summary") or dn.get("description") or
        f"{label} is a rare disease in the lysosomal storage group."))
    return {"disease": full(g, d), "headline": hl.headline, "plain_summary": hl.plain_summary,
            "treatment_status": {"has_approved_treatment": has_tx, "note": tx_note, "edge_ids": [e["id"] for e in tx]},
            "exact_groups": exact, "related_communities": communities, "connections": connections,
            "assets": assets, "trials": trials, "researchers": researchers, "shared_people": shared,
            "next_steps": steps, "coverage": {"has_supported_route": supported, "sources": cov, "gaps": gaps}}


# ---------------------------------------------------------------- MechanismView
def mechanism_view(ix: Index, m: str, clusters: list[dict], members: list[dict], plinks) -> dict:
    g = ix.g
    cl_of = {x["node_id"]: x["cluster_id"] for x in members}
    cl = {c["id"]: c for c in clusters}
    dis = sorted((e for e in ix.src_of(m, "disease_involves_mechanism") if e["status"] != "hypothesis"),
                 key=lambda e: (-e["confidence"], e["src"]))
    ranked = []
    for e in dis:
        d = e["src"]
        approved = any((x.get("attrs") or {}).get("approval") == "approved"
                       for x in ix.src_of(d, "intervention_treats_disease"))
        c = cl.get(cl_of.get(d, ""))
        ranked.append({"disease": brief(g, d), "score": e["confidence"], "status": e["status"],
                       "evidence_edge_ids": [e["id"]], "cluster": {"id": c["id"], "label": c["label"]} if c else None,
                       "groups": groups_for(ix, d)[:3],
                       "assets": [asset_card(ix, a, d, set()) for a in
                                  sorted({x["src"] for x in ix.src_of(d, "asset_covers_disease")})][:3],
                       "unmet_need": None if approved else "No approved therapy found in our sources"})
    by_cl = defaultdict(list)
    for r in ranked:
        if r["cluster"]:
            by_cl[r["cluster"]["id"]].append(r["disease"]["id"])
    dset = {r["disease"]["id"] for r in ranked}
    people = sorted(((pid, dm) for pid, dm in plinks.items() if dset & set(dm)),
                    key=lambda x: (-len(dset & set(x[1])), x[0]))
    trials, seen = [], set()   # top-ranked diseases first, one card per trial
    for r in ranked[:20]:
        for e in sorted(ix.src_of(r["disease"]["id"], "trial_studies_disease"), key=lambda e: e["src"]):
            if e["src"] not in seen:
                seen.add(e["src"])
                trials.append(trial_card(ix, e, "shared_mechanism", None))
    trials = trials[:8]
    label = g.nodes[m]["label"]
    return {"mechanism": full(g, m),
            "headline": f"{label}: {len(ranked)} diseases in the atlas share this mechanism",
            "plain_summary": g.nodes[m].get("plain_summary") or g.nodes[m].get("description") or
            f"{label} is a biological process that is disrupted in several rare diseases.",
            "genes": [brief(g, x) for x in sorted({e["src"] for e in ix.src_of(m, "gene_in_mechanism")})][:30],
            "diseases": ranked[:30],
            "clusters": [{"cluster": {k: cl[c][k] for k in ("id", "label", "summary", "method", "size", "attrs")},
                          "disease_ids": ds, "score": round(len(ds) / max(1, len(ranked)), 3)}
                         for c, ds in sorted(by_cl.items(), key=lambda x: (-len(x[1]), x[0]))],
            "researchers": [person_card(ix, pid, dm) for pid, dm in people[:8]],
            "trials": trials,
            "interventions": [brief(g, x) for x in
                              sorted({e["src"] for e in ix.src_of(m, "intervention_targets_mechanism")})]}


def run() -> None:
    g = load_graph()
    ix = Index(g)
    paths = _rows_or_empty("paths.jsonl")
    overlap = _rows_or_empty("overlap.jsonl")
    clusters, members = _rows_or_empty("clusters.jsonl"), _rows_or_empty("cluster_members.jsonl")
    plinks = person_disease_links(g)
    focus = [d for d in (read_json("slice", {}) or {}).get("focus", []) if d in g.nodes]
    rows: list[dict[str, Any]] = [{"kind": "action", "key": d, "payload": action_view(ix, d, paths, overlap, plinks)}
                                  for d in focus]
    min_d = int(slice_config().views.get("mechanism_min_diseases", 2))
    fset = set(focus)
    mechs = [m["id"] for m in sorted(g.by_type("mechanism"), key=lambda n: n["id"])
             if len({e["src"] for e in ix.src_of(m["id"], "disease_involves_mechanism")}) >= min_d
             and fset & {e["src"] for e in ix.src_of(m["id"], "disease_involves_mechanism")}]
    rows += [{"kind": "mechanism", "key": m, "payload": mechanism_view(ix, m, clusters, members, plinks)}
             for m in mechs]
    write_jsonl("views.jsonl", rows)
    log.info("views: %d action views, %d mechanism views", len(focus), len(mechs))
