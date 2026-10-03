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
from .config import slice_config
from .graph import Graph, load_graph
from .config import INTERIM
from .store import read_json, read_jsonl, write_jsonl

log = logging.getLogger(__name__)
STATUS_RANK = ["curated", "literature", "inferred", "hypothesis"]
GROUP_SUBTYPES = {"patient_group", "foundation"}
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
        return sorted(es, key=lambda x: -(x[1].get("score") or 0))


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
    return [org_card(ix, e) for e in sorted(es, key=lambda e: -e["confidence"])]


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


def asset_card(ix: Index, aid: str, d: str, related: set[str]) -> dict:
    g = ix.g
    covers = [x["dst"] for x in ix.dst_of(aid, "asset_covers_disease")]
    owner = next((x["src"] for x in ix.src_of(aid, "organization_maintains_asset")), None)
    label = g.nodes[d]["label"]
    reuse = "direct" if d in covers else ("adaptable" if related & set(covers) else "reference_only")
    differs = [] if reuse == "direct" else [f"Built for {', '.join(g.nodes[c]['label'] for c in covers[:3])}, not {label}"]
    review = [] if reuse == "direct" else [f"Do eligibility criteria and outcome measures fit {label} patients?",
                                           "Does the owner accept families with a related diagnosis?"]
    eids = [x["id"] for x in ix.dst_of(aid, "asset_covers_disease")] + \
           [x["id"] for x in ix.src_of(aid, "organization_maintains_asset")]
    return {"node": brief(g, aid), "asset_kind": g.nodes[aid].get("subtype") or "registry",
            "owner": brief(g, owner) if owner else None, "covers": [brief(g, c) for c in covers[:6]],
            "reusability": reuse, "what_differs": differs, "needs_review": review, "edge_ids": eids}


def path_json(g: Graph, p: dict) -> dict:
    es = [g.edges[x] for x in p["edge_ids"]]
    return {"id": p["id"], "kind": p["kind"], "title": p["title"], "score": p["score"], "from": p["from_id"],
            "to": p["to_id"], "node_ids": p["node_ids"], "edge_ids": p["edge_ids"],
            "nodes": [brief(g, n) for n in p["node_ids"]], "edges": [edge_json(g, x) for x in p["edge_ids"]],
            "weakest_status": max((e["status"] for e in es), key=STATUS_RANK.index),
            "min_confidence": min(e["confidence"] for e in es), "attrs": p.get("attrs") or {}}


# ---------------------------------------------------------------- ActionView
def action_view(ix: Index, d: str, paths: list[dict], overlap: list[dict], plinks) -> dict:
    g = ix.g
    dn, label = g.nodes[d], g.nodes[d]["label"]
    allsims = ix.similar(d)
    # top 5 plus every counterexample: a look-alike with a different mechanism must always be visible
    sims = allsims[:5] + [x for x in allsims[5:] if (x[1].get("attrs") or {}).get("caution")]
    related = {o for o, _ in sims}

    tx = ix.src_of(d, "intervention_treats_disease")
    approved = [e for e in tx if (e.get("attrs") or {}).get("approval") == "approved"]
    has_tx = True if approved else (False if tx else None)
    tx_note = (f"Approved: {', '.join(g.nodes[e['src']]['label'] for e in approved)}" if approved else
               "No approved treatment found in the sources we checked." if tx is not None else "")

    exact = groups_for(ix, d)
    mech_d = {e["dst"] for e in ix.dst_of(d, "disease_involves_mechanism") if e["status"] != "hypothesis"}
    gene_d = {e["src"] for e in ix.src_of(d, "gene_associated_with_disease")}
    communities = []
    for o, e in sims:
        comp = (e.get("attrs") or {}).get("components", {})
        mech_o = {x["dst"] for x in ix.dst_of(o, "disease_involves_mechanism") if x["status"] != "hypothesis"}
        gene_o = {x["src"] for x in ix.src_of(o, "gene_associated_with_disease")}
        why = []
        if gene_d & gene_o:
            why.append("the same gene (" + ", ".join(g.nodes[x]["label"] for x in sorted(gene_d & gene_o)) + ")")
        if mech_d & mech_o:
            why.append("shared pathways (" + ", ".join(g.nodes[x]["label"] for x in sorted(mech_d & mech_o)[:2]) + ")")
        if comp.get("phenotype", 0) >= 0.2:
            why.append("an overlapping pattern of informative symptoms")
        diffs = []
        if gene_o - gene_d:
            diffs.append("Different gene: " + ", ".join(g.nodes[x]["label"] for x in sorted(gene_o - gene_d)[:3]))
        if mech_o and mech_d and not (mech_o & mech_d):
            diffs.append("No shared known pathway")
        if comp.get("phenotype", 0) < 0.2:
            diffs.append("Symptoms overlap only weakly")
        communities.append({"disease": brief(g, o), "similarity": e.get("score") or 0.0, "similarity_edge_id": e["id"],
                            "why": f"{label} and {g.nodes[o]['label']} share " + (" and ".join(why) or "some features") + ".",
                            "differences": diffs, "caution": (e.get("attrs") or {}).get("caution"),
                            "groups": groups_for(ix, o)[:3]})

    my_paths = [p for p in paths if p["from_id"] == d]
    connections = []
    for kind in ("patient_group", "related_disease", "asset", "researcher", "trial", "intervention"):
        connections += [path_json(g, p) for p in my_paths if p["kind"] == kind][:2]

    asset_ids = {e["src"] for x in {d} | related for e in ix.src_of(x, "asset_covers_disease")}
    assets = sorted((asset_card(ix, a, d, related) for a in asset_ids),
                    key=lambda c: ["direct", "adaptable", "reference_only"].index(c["reusability"]))[:6]

    trials = [trial_card(ix, e, "same_disease", None) for e in ix.src_of(d, "trial_studies_disease")]
    for o in related:
        trials += [trial_card(ix, e, "related_disease", f"Studies {g.nodes[o]['label']}; check eligibility for {label}.")
                   for e in ix.src_of(o, "trial_studies_disease")]
    seen, uniq = set(), []
    for t in trials:
        if t["node"]["id"] not in seen:
            seen.add(t["node"]["id"])
            uniq.append(t)
    trials = uniq[:8]

    people = [(pid, dm) for pid, dm in plinks.items() if d in dm]
    people.sort(key=lambda x: -(len(x[1][d]) + 0.1 * len(x[1])))
    researchers = [person_card(ix, pid, dm) for pid, dm in people[:8]]
    shared = [{"person": person_card(ix, r["person"], plinks[r["person"]]),
               "communities": [brief(g, x) for x in r["diseases"][:6]], "edge_ids": r["edge_ids"][:12]}
              for r in overlap if d in r["diseases"]][:5]

    steps: list[dict] = []

    def step(kind, title, rationale, target, status, validation, effort, eids):
        steps.append({"id": f"S:{d}:{len(steps) + 1}", "kind": kind, "title": title, "rationale": rationale,
                      "target": brief(g, target) if target else None, "status": status,
                      "validation_needed": validation, "effort": effort, "edge_ids": eids})
    if exact:
        o = exact[0]
        step("contact_group", f"Contact {o['node']['label']}", f"They support families with {label}.",
             o["node"]["id"], "viable", [], "this_week", [o["edge_id"]])
    else:
        step("build_missing_group", f"Start a community for {label}",
             "We found no patient group for exactly this diagnosis.", None, "viable", [], "this_month", [])
    for c in communities[:2]:
        if c["groups"]:
            o = c["groups"][0]
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
    for c in communities:
        if c["caution"]:
            step("validate_experiment", f"Test whether {label} and {c['disease']['label']} share the mechanism",
                 c["caution"], None, "needs_review", ["Expert review of the mechanism difference"], "this_quarter",
                 [c["similarity_edge_id"]])
            break

    cov = []
    for name, f in COVERAGE_FILES.items():
        c = (read_json(f, {}) or {}).get(d)
        cov.append({"name": name, "checked": c is not None, "query": (c or {}).get("query"),
                    "result_count": (c or {}).get("result_count"), "checked_at": (c or {}).get("checked_at")})
    for name in ("HPO", "Orphanet", "MONDO"):
        cov.append({"name": name, "checked": True, "query": d, "result_count": None, "checked_at": None})
    gaps = []
    if not exact:
        gaps.append({"question": f"Is there a patient group for {label} that our sources missed?",
                     "why_it_matters": "Families need a community and researchers need a partner to recruit through.",
                     "what_would_resolve": "Ask clinicians and related groups; contribute any group you know."})
    if not mech_d:
        gaps.append({"question": f"Which biological pathway is disrupted in {label}?",
                     "why_it_matters": "Without a mechanism, related diseases can only be matched by symptoms.",
                     "what_would_resolve": "Functional studies of the gene product; expert curation."})
    for c in communities:
        if c["caution"]:
            gaps.append({"question": f"Do findings from {c['disease']['label']} transfer to {label}?",
                         "why_it_matters": c["caution"],
                         "what_would_resolve": "Compare the disrupted step in cell or animal models."})
            break
    supported = any(p["kind"] in ("patient_group", "asset", "researcher") and p["weakest_status"] != "hypothesis"
                    and p["min_confidence"] >= 0.3 for p in connections) or bool(exact)

    facts = {"disease": label, "approved_treatment": has_tx, "exact_groups": len(exact),
             "related": [c["disease"]["label"] for c in communities[:3]], "description": dn.get("description")}
    hl, _ = llm.rephrase("headline+plain_summary for a family's disease page", facts, Headline, lambda: Headline(
        headline=(f"{'No approved treatment found yet' if not has_tx else 'An approved treatment exists'}"
                  f" — {label} is connected to {len(communities)} related diseases"),
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
                 key=lambda e: -e["confidence"])
    ranked = []
    for e in dis:
        d = e["src"]
        approved = any((x.get("attrs") or {}).get("approval") == "approved"
                       for x in ix.src_of(d, "intervention_treats_disease"))
        c = cl.get(cl_of.get(d, ""))
        ranked.append({"disease": brief(g, d), "score": e["confidence"], "status": e["status"],
                       "evidence_edge_ids": [e["id"]], "cluster": {"id": c["id"], "label": c["label"]} if c else None,
                       "groups": groups_for(ix, d)[:3], "assets": [],
                       "unmet_need": None if approved else "No approved therapy found in our sources"})
    by_cl = defaultdict(list)
    for r in ranked:
        if r["cluster"]:
            by_cl[r["cluster"]["id"]].append(r["disease"]["id"])
    dset = {r["disease"]["id"] for r in ranked}
    people = sorted(((pid, dm) for pid, dm in plinks.items() if dset & set(dm)), key=lambda x: -len(dset & set(x[1])))
    trials = [trial_card(ix, e, "shared_mechanism", None) for d in list(dset)[:20]
              for e in ix.src_of(d, "trial_studies_disease")][:8]
    label = g.nodes[m]["label"]
    return {"mechanism": full(g, m),
            "headline": f"{label}: {len(ranked)} diseases in the atlas share this mechanism",
            "plain_summary": g.nodes[m].get("plain_summary") or g.nodes[m].get("description") or
            f"{label} is a biological process that is disrupted in several rare diseases.",
            "genes": [brief(g, e["src"]) for e in ix.src_of(m, "gene_in_mechanism")][:30],
            "diseases": ranked[:30],
            "clusters": [{"cluster": {k: cl[c][k] for k in ("id", "label", "summary", "method", "size", "attrs")},
                          "disease_ids": ds, "score": round(len(ds) / max(1, len(ranked)), 3)}
                         for c, ds in sorted(by_cl.items(), key=lambda x: -len(x[1]))],
            "researchers": [person_card(ix, pid, dm) for pid, dm in people[:8]],
            "trials": trials,
            "interventions": [brief(g, e["src"]) for e in ix.src_of(m, "intervention_targets_mechanism")]}


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
    mechs = [m["id"] for m in g.by_type("mechanism")
             if len({e["src"] for e in ix.src_of(m["id"], "disease_involves_mechanism")}) >= min_d
             and fset & {e["src"] for e in ix.src_of(m["id"], "disease_involves_mechanism")}]
    rows += [{"kind": "mechanism", "key": m, "payload": mechanism_view(ix, m, clusters, members, plinks)}
             for m in mechs]
    write_jsonl("views.jsonl", rows)
    log.info("views: %d action views, %d mechanism views", len(focus), len(mechs))
