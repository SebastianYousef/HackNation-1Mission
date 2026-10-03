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

Adj = dict[str, list[tuple[str, float, str]]]


def target_kind(n: dict) -> str | None:
    t, st = n["type"], n.get("subtype")
    if t == "organization":
        return "patient_group" if st in GROUP_SUBTYPES else None
    return {"asset": "asset", "person": "researcher", "trial": "trial",
            "intervention": "intervention", "disease": "related_disease"}.get(t)


def build_adj(g: Graph, hub_degree: int) -> Adj:
    """Directed adjacency u -> [(v, cost, edge id)], cheapest edge per pair, sorted for determinism."""
    deg: dict[str, int] = defaultdict(int)
    for e in g.edges.values():
        deg[e["src"]] += 1
        deg[e["dst"]] += 1
    best: dict[tuple[str, str], tuple[float, str]] = {}
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
        arcs = [(a, b)] if e["type"] in UPWARD_ONLY else [(a, b), (b, a)]
        for arc in arcs:
            old = best.get(arc)
            if old is None or (cost, e["id"]) < old:
                best[arc] = (cost, e["id"])
    adj: Adj = defaultdict(list)
    for (u, v), (cost, eid) in sorted(best.items()):
        adj[u].append((v, cost, eid))
    return adj


def bounded_routes(g: Graph, adj: Adj, src: str, max_len: int) -> dict[str, tuple[float, list[str], list[str]]]:
    """Cheapest route with <= max_len edges from src to every reachable node (hop-bounded Bellman-Ford).
    Nodes of a NO_THROUGH kind are reached but never expanded."""
    best: dict[str, tuple[float, list[str], list[str]]] = {src: (0.0, [src], [])}
    frontier = dict(best)
    for _ in range(max_len):
        nxt: dict[str, tuple[float, list[str], list[str]]] = {}
        for u in sorted(frontier):
            cu, nodes, eids = frontier[u]
            if u != src and target_kind(g.nodes[u]) in NO_THROUGH:
                continue
            for v, cost, eid in adj.get(u, ()):
                if v in nodes:
                    continue
                c = cu + cost
                if c < best.get(v, (math.inf,))[0] and c < nxt.get(v, (math.inf,))[0]:
                    nxt[v] = (c, nodes + [v], eids + [eid])
        best.update(nxt)
        frontier = nxt
        if not frontier:
            break
    return best


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
    for e in g.edges_of("disease_similar_to"):
        if (e.get("attrs") or {}).get("caution"):
            partners[e["src"]].add(e["dst"])
            partners[e["dst"]].add(e["src"])
    serves: dict[str, set[str]] = defaultdict(set)     # trial / intervention -> diseases it studies / treats
    for e in g.edges_of("trial_studies_disease") + g.edges_of("intervention_treats_disease"):
        if e["status"] != "hypothesis":
            serves[e["src"]].add(e["dst"])
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
            if pheno:
                attrs.update(bridge="shared_phenotype", via_phenotype=pheno)
                _cap(attrs, "inferred", f"This route hinges on one shared symptom "
                                        f"({g.nodes[pheno[0]]['label']}); many unrelated diseases share it.")
            if kind in ("intervention", "trial") and d not in serves[t]:
                therapy = kind == "intervention" or g.nodes[t].get("subtype") == "interventional"
                if counterexample_therapy(d, serves[t], therapy, partners[d]):
                    n_dropped += 1
                    continue
                # another disease in the middle that is neither d's broader family nor shares d's gene:
                # reaching its therapy or trial assumes the finding transfers
                via = [x for x in inner if g.nodes[x]["type"] == "disease"
                       and x not in anc.get(d, ()) and not genes[x] & genes[d]]
                if via:
                    if kind == "intervention":
                        n_dropped += 1
                        continue
                    attrs.update(via_disease=via[0], transfer="speculative")
                    _cap(attrs, "hypothesis", f"This trial studies {g.nodes[via[0]]['label']}; whether it is "
                                              f"relevant to {ld} is unproven.")
                else:
                    if other_subtype_therapy(d, serves[t], therapy, [genes[x] for x in serves[t]], genes[d]):
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
                         f"Not studied in {ld} itself; whether it applies is unproven.")
            hops = len(eids)
            score = math.exp(-cost) * 0.85 ** (hops - 1)
            best.setdefault(kind, []).append((score, nodes, eids, attrs))
        for kind in sorted(best):
            for score, nodes, eids, attrs in sorted(best[kind], key=lambda x: (-x[0], x[1]))[:per_kind]:
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
