"""Precomputed explainable routes from every focus disease to patient groups, assets, researchers,
trials, interventions and related diseases.

Cost of an edge = -ln(confidence) + hub penalty (high-degree phenotype nodes are weak bridges:
"both have seizures" connects half the slice). A hop-bounded shortest-path search from the disease
finds the cheapest route with <= max_path_len edges to each target; the search never expands *out of*
a patient group / person / trial / asset, so a target whose cheapest unconstrained route crosses one
is re-routed instead of dropped. disease_subtype_of is walked child -> parent only, so a subtype
reaches groups and studies of its broader family (CLN5 -> NCL -> BDSRA) but never hops to siblings.
Then the best `paths_per_kind` per kind are kept.
Score = product of edge confidences x 0.85^(hops-1) x hub factor.

Route choice prefers a hop through a quoted mechanism (a curated / literature disease_involves_mechanism edge,
e.g. CLN5 -> soluble lysosomal protein deficiency -> CLN1) by MECH_PREF per such edge; the reported score
still comes from the real edge confidences. Per kind, routes that hinge on a shared symptom rank last.
attrs.bridge = "shared_phenotype" | "shared_mechanism" (quoted) | "shared_pathway" (computed propagation).

Honesty caps (in attrs; edges keep their own status). path_strength applies them once, here: every row carries
weakest_status / min_confidence, which the loader stores and both GET /paths and the views serve unchanged:
  * a route through a phenotype hinges on one shared symptom -> status_cap "inferred", confidence_cap 0.25
  * an intervention reached through another, unrelated disease (not an ancestor, no shared gene) is a
    therapy-transfer guess -> dropped; a trial reached that way -> status_cap "hypothesis" + caution.
  * a trial / intervention reached only through d's broader family or a same-gene disease (it does not name
    d itself) -> status_cap "inferred", confidence_cap 0.25 + caution: it may not apply to d.
  * a therapy (an intervention, or an interventional trial) that does not name d is dropped when it names a
    counterexample of d (similarity edge with a caution), or when it is filed under a broader term but names
    only gene-defined subtypes that share no gene with d (CLN2 gene therapy listed under "late infantile
    NCL", seen from CLN5). Observational studies (registries, natural history, biobanks) are kept.
    For a trial, what it "names" includes the diseases its tested interventions treat: a CLN2 enzyme
    replacement trial whose CT.gov condition links are only 'hypothesis' still names CLN2.
  * a trial reached *through* an intervention is dropped when that intervention would be dropped itself
    (it treats a counterexample of d and not d, or it is reached through an unrelated disease): the trial
    is the same therapy-transfer guess, observational or not.
  * a counterexample's caution goes on the route to it; an "approved_therapy" caution only when the target
    is the disease that has the therapy (attrs.therapy_for), matching views.py.
Beyond paths_per_kind, up to two routes through a quoted mechanism are kept: routes without a caution and not
through a counterexample first, then a mechanism not already shown, then the closest quoted-mechanism overlap
(Jaccard) with d.
Writes paths.jsonl."""
from __future__ import annotations

import json
import logging
import math
from collections import defaultdict

from ..config import slice_config
from ..graph import Graph
from ..ids import path_id
from ..store import read_json, write_jsonl

log = logging.getLogger(__name__)

UPWARD_ONLY = {"disease_subtype_of"}       # traversed src (subtype) -> dst (parent) only
GROUP_SUBTYPES = {"patient_group", "foundation"}
NO_THROUGH = {"patient_group", "researcher", "trial", "asset"}  # may end a route, never sit inside one
PHENO_BRIDGE = 0.35  # extra cost per edge touching a phenotype: one shared symptom is a weak bridge
WEAK_CONF = 0.25     # confidence cap for routes that hinge on one symptom or a therapy-transfer guess
MECH_PREF = 0.35     # route-choice bonus per quoted disease -> mechanism edge (not used in the score)
QUOTED = {"curated", "literature"}

Adj = dict[str, list[tuple[str, float, float, str]]]   # u -> [(v, choice cost, score cost, edge id)]


def quoted_mechanism(e: dict) -> bool:
    """A disease -> mechanism link backed by a curated database or a verbatim quote (not propagation)."""
    return e["type"] == "disease_involves_mechanism" and e["status"] in QUOTED


def target_kind(n: dict) -> str | None:
    t, st = n["type"], n.get("subtype")
    if t == "organization":
        return "patient_group" if st in GROUP_SUBTYPES else None
    return {"asset": "asset", "person": "researcher", "trial": "trial",
            "intervention": "intervention", "disease": "related_disease"}.get(t)


def build_adj(g: Graph, hub_degree: int) -> Adj:
    """Directed adjacency u -> [(v, choice cost, score cost, edge id)], cheapest edge per pair, sorted for
    determinism. Choice cost = score cost minus MECH_PREF for a quoted mechanism edge."""
    deg: dict[str, int] = defaultdict(int)
    for e in g.edges.values():
        deg[e["src"]] += 1
        deg[e["dst"]] += 1
    best: dict[tuple[str, str], tuple[float, float, str]] = {}
    for e in g.edges.values():
        if e["status"] == "hypothesis":
            continue
        a, b = e["src"], e["dst"]
        cost = -math.log(max(e["confidence"], 0.01))
        for x in (a, b):
            if g.nodes[x]["type"] == "phenotype":
                cost += PHENO_BRIDGE
                if deg[x] > hub_degree:
                    cost += math.log(deg[x] / hub_degree) + 0.5
        choice = max(0.01, cost - MECH_PREF) if quoted_mechanism(e) else cost
        arcs = [(a, b)] if e["type"] in UPWARD_ONLY else [(a, b), (b, a)]
        for arc in arcs:
            old = best.get(arc)
            if old is None or (choice, cost, e["id"]) < old:
                best[arc] = (choice, cost, e["id"])
    adj: Adj = defaultdict(list)
    for (u, v), (choice, cost, eid) in sorted(best.items()):
        adj[u].append((v, choice, cost, eid))
    return adj


def bounded_routes(g: Graph, adj: Adj, src: str, max_len: int) -> dict[str, tuple[float, list[str], list[str]]]:
    """Route with the lowest choice cost and <= max_len edges from src to every reachable node (hop-bounded
    Bellman-Ford), returned as {node: (score cost, nodes, edge ids)}. Nodes of a NO_THROUGH kind are reached
    but never expanded."""
    best: dict[str, tuple[float, float, list[str], list[str]]] = {src: (0.0, 0.0, [src], [])}
    frontier = dict(best)
    for _ in range(max_len):
        nxt: dict[str, tuple[float, float, list[str], list[str]]] = {}
        for u in sorted(frontier):
            su, cu, nodes, eids = frontier[u]
            if u != src and target_kind(g.nodes[u]) in NO_THROUGH:
                continue
            for v, choice, cost, eid in adj.get(u, ()):
                if v in nodes:
                    continue
                sc = su + choice
                if sc < best.get(v, (math.inf,))[0] and sc < nxt.get(v, (math.inf,))[0]:
                    nxt[v] = (sc, cu + cost, nodes + [v], eids + [eid])
        best.update(nxt)
        frontier = nxt
        if not frontier:
            break
    return {v: (cost, nodes, eids) for v, (_, cost, nodes, eids) in best.items()}


def counterexample_therapy(d: str, serves_t: set[str], therapy: bool, partners_d: set[str]) -> bool:
    """A therapy that does not name d but names one of d's counterexamples (shared with views.py)."""
    return therapy and d not in serves_t and bool(serves_t & partners_d)


def other_subtype_therapy(d: str, serves_t: set[str], therapy: bool, cond_genes: list[set[str]],
                          genes_d: set[str]) -> bool:
    """A therapy that does not name d and whose gene-defined conditions all have other genes than d:
    aimed at other subtypes even when it is also filed under a broader term (shared with views.py)."""
    named = [s for s in cond_genes if s]
    return therapy and d not in serves_t and bool(named) and not any(s & genes_d for s in named)


STATUS_RANK = ["curated", "literature", "inferred", "hypothesis"]


def path_strength(g: Graph, edge_ids: list[str], attrs: dict) -> tuple[str, float]:
    """The one rule for a path's strength: the weakest edge status and the lowest edge confidence, lowered
    (never raised) by attrs.status_cap / attrs.confidence_cap."""
    es = [g.edges[x] for x in edge_ids]
    weakest = max([e["status"] for e in es] + ([attrs["status_cap"]] if attrs.get("status_cap") else []),
                  key=STATUS_RANK.index)
    min_conf = min([e["confidence"] for e in es] +
                   ([attrs["confidence_cap"]] if attrs.get("confidence_cap") is not None else []))
    return weakest, min_conf


def _jaccard(a: set[str], b: set[str]) -> float:
    return len(a & b) / len(a | b) if a | b else 0.0


def _cap(attrs: dict, status: str, caution: str) -> None:
    """Lower the path's honesty caps (never raise them) and record why."""
    if STATUS_RANK.index(status) > STATUS_RANK.index(attrs.get("status_cap") or "curated"):
        attrs["status_cap"] = status
    attrs["confidence_cap"] = min(attrs.get("confidence_cap", WEAK_CONF), WEAK_CONF)
    attrs["caution"] = f"{attrs['caution']} {caution}" if attrs.get("caution") else caution


def _ancestors(g: Graph) -> dict[str, set[str]]:
    parents: dict[str, set[str]] = defaultdict(set)
    for e in g.edges_of("disease_subtype_of"):
        if e["status"] != "hypothesis":
            parents[e["src"]].add(e["dst"])
    out: dict[str, set[str]] = {}

    def walk(d: str) -> set[str]:
        if d not in out:
            out[d] = set()   # cycle guard
            out[d] = set().union(*({p} | walk(p) for p in parents[d])) if parents[d] else set()
        return out[d]
    for d in list(parents):
        walk(d)
    return out


def run(g: Graph) -> list[dict]:
    cfg = slice_config().analytics
    per_kind, max_len = int(cfg.get("paths_per_kind", 5)), int(cfg.get("max_path_len", 4))
    adj = build_adj(g, int(cfg.get("hub_degree", 25)))
    anc = _ancestors(g)
    genes: dict[str, set[str]] = defaultdict(set)
    for e in g.edges_of("gene_associated_with_disease"):
        if e["status"] != "hypothesis":
            genes[e["dst"]].add(e["src"])
    partners: dict[str, set[str]] = defaultdict(set)   # disease -> counterexample diseases
    caution_of: dict[tuple[str, str], dict] = {}        # (disease, counterexample) -> similarity edge attrs
    for e in g.edges_of("disease_similar_to"):
        if (e.get("attrs") or {}).get("caution"):
            partners[e["src"]].add(e["dst"])
            partners[e["dst"]].add(e["src"])
            caution_of[(e["src"], e["dst"])] = caution_of[(e["dst"], e["src"])] = e["attrs"]
    serves: dict[str, set[str]] = defaultdict(set)     # trial / intervention -> diseases it studies / treats
    treats: dict[str, set[str]] = defaultdict(set)     # intervention -> diseases it treats
    tests: dict[str, set[str]] = defaultdict(set)      # trial -> interventions it tests
    for e in g.edges_of("trial_studies_disease") + g.edges_of("intervention_treats_disease"):
        if e["status"] != "hypothesis":
            serves[e["src"]].add(e["dst"])
            if e["type"] == "intervention_treats_disease":
                treats[e["src"]].add(e["dst"])
    for e in g.edges_of("trial_tests_intervention"):
        if e["status"] != "hypothesis":
            tests[e["src"]].add(e["dst"])
    qmech: dict[str, set[str]] = defaultdict(set)      # disease -> quoted mechanisms
    for e in g.edges_of("disease_involves_mechanism"):
        if quoted_mechanism(e):
            qmech[e["src"]].add(e["dst"])
    focus = [d for d in (read_json("slice", {}) or {}).get("focus", []) if d in g.nodes]
    rows: list[dict] = []
    n_dropped = 0
    for d in focus:
        best: dict[str, list[tuple[float, list[str], list[str], dict]]] = {}
        for t, (cost, nodes, eids) in sorted(bounded_routes(g, adj, d, max_len).items()):
            kind = target_kind(g.nodes[t])
            if t == d or kind is None:
                continue
            attrs: dict = {"hops": len(eids)}
            inner = nodes[1:-1]
            ld = g.nodes[d]["label"]
            pheno = [x for x in inner if g.nodes[x]["type"] == "phenotype"]
            mechs = [x for x in inner if g.nodes[x]["type"] == "mechanism"]
            if mechs:
                dim = [g.edges[x] for x in eids if g.edges[x]["type"] == "disease_involves_mechanism"]
                quoted = bool(dim) and all(map(quoted_mechanism, dim))
                attrs.update(bridge="shared_mechanism" if quoted else "shared_pathway", via_mechanism=mechs)
            if pheno:
                attrs.update(bridge="shared_phenotype", via_phenotype=pheno)
                _cap(attrs, "inferred", f"This route hinges on one shared symptom "
                                        f"({g.nodes[pheno[0]]['label']}); many unrelated diseases share it.")
            ca = caution_of.get((d, t)) if kind == "related_disease" else None
            if ca and (ca.get("caution_kind") != "approved_therapy" or ca.get("therapy_for") == t):
                # a counterexample stays visible (e.g. CLN5 -> soluble protein deficiency -> CLN2), with its caution;
                # the route's own edges keep their status. "CLN2 has an approved therapy ... whether it helps CLN5"
                # belongs on CLN5 -> CLN2 only, not on CLN2's own routes to CLN5
                c = ca["caution"]
                attrs["caution"] = f"{attrs['caution']} {c}" if attrs.get("caution") else c
            if kind in ("intervention", "trial") and d not in serves[t]:
                therapy = kind == "intervention" or g.nodes[t].get("subtype") == "interventional"
                # what the therapy names: its conditions, plus (for a trial) what its tested interventions treat
                scope = serves[t].union(*(treats[i] for i in tests[t]))
                ivs = [x for x in inner if g.nodes[x]["type"] == "intervention"]
                if counterexample_therapy(d, scope, therapy, partners[d]) or \
                        any(counterexample_therapy(d, treats[i], True, partners[d]) for i in ivs):
                    n_dropped += 1
                    continue
                # another disease in the middle that is neither d's broader family nor shares d's gene:
                # reaching its therapy or trial assumes the finding transfers
                via = [x for x in inner if g.nodes[x]["type"] == "disease"
                       and x not in anc.get(d, ()) and not genes[x] & genes[d]]
                if via:
                    if kind == "intervention" or ivs:   # a trial of a therapy-transfer guess is one too
                        n_dropped += 1
                        continue
                    attrs.update(via_disease=via[0], transfer="speculative")
                    _cap(attrs, "hypothesis", f"This trial studies {g.nodes[via[0]]['label']}; whether it is "
                                              f"relevant to {ld} is unproven.")
                else:
                    if other_subtype_therapy(d, scope, therapy, [genes[x] for x in scope], genes[d]):
                        n_dropped += 1
                        continue
                    # reached through d's broader family or a same-gene disease: d itself is not named
                    dis = [x for x in inner if g.nodes[x]["type"] == "disease"]
                    x = dis[-1] if dis else None
                    _cap(attrs, "inferred",
                         f"Filed under the broader {g.nodes[x]['label']}; whether it applies to {ld} is unproven."
                         if x in anc.get(d, ()) else
                         f"Studies {g.nodes[x]['label']}, which shares a gene with {ld}; whether it applies to "
                         f"{ld} is unproven." if x else
                         f"Linked to {g.nodes[mechs[0]]['label']}, which is involved in {ld}; our sources show no "
                         f"test in {ld}, so whether it applies must be checked." if mechs else
                         f"Tests {g.nodes[ivs[0]]['label']}; ClinicalTrials.gov does not list {ld} as a condition "
                         f"of this study, so check that it enrols {ld}." if ivs and kind == "trial" else
                         f"Not studied in {ld} itself; whether it applies is unproven.")
            hops = len(eids)
            score = math.exp(-cost) * 0.85 ** (hops - 1)
            best.setdefault(kind, []).append((score, nodes, eids, attrs))
        for kind in sorted(best):
            # a route that hinges on one shared symptom ranks after every route that does not; the best two
            # routes through a documented (quoted) mechanism are always kept, even beyond paths_per_kind
            ranked = sorted(best[kind], key=lambda x: (x[3].get("bridge") == "shared_phenotype", -x[0], x[1]))
            keep = ranked[:per_kind]
            is_mech = lambda x: x[3].get("bridge") == "shared_mechanism"  # noqa: E731
            pool = [x for x in ranked[per_kind:] if is_mech(x)]
            shown = {tuple(x[3]["via_mechanism"]) for x in keep if is_mech(x)}
            for _ in range(max(0, 2 - sum(map(is_mech, keep)))):
                if not pool:
                    break
                # a route with a caution or through a counterexample last (CLN5 -> SCMAS storage -> CLN2), then a
                # mechanism not shown yet, then the closest quoted-mechanism overlap with d; equal scores never fall
                # back to node-id order first
                x = min(pool, key=lambda x: (bool(x[3].get("caution")) or bool(partners[d] & set(x[1])),
                                             tuple(x[3]["via_mechanism"]) in shown,
                                             -_jaccard(qmech[d], qmech[x[1][-1]]), -x[0], x[1]))
                pool.remove(x)
                shown.add(tuple(x[3]["via_mechanism"]))
                keep.append(x)
            for score, nodes, eids, attrs in keep:
                labels = [g.nodes[n]["label"] for n in nodes]
                weakest, min_conf = path_strength(g, eids, attrs)
                rows.append({"id": path_id(kind, nodes), "from_id": d, "to_id": nodes[-1], "kind": kind,
                             "title": " → ".join(labels), "node_ids": nodes, "edge_ids": eids,
                             "score": round(score, 4), "weakest_status": weakest, "min_confidence": min_conf,
                             "attrs": attrs})
    write_jsonl("paths.jsonl", rows)
    log.info("paths: %d paths from %d focus diseases (%d therapy-transfer routes dropped)",
             len(rows), len(focus), n_dropped)
    log.debug("example: %s", json.dumps(rows[:1]))
    return rows
