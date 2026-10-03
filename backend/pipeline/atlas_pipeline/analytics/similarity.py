"""disease_similar_to (inferred): weighted mix of
  phenotype  IC-weighted Jaccard over ancestor-propagated HPO sets (informative symptoms dominate)
  mechanism  Jaccard over disease_involves_mechanism targets (hypothesis edges excluded)
  gene       Jaccard over associated genes
plus counterexamples: phenotypically close but mechanistically different -> attrs.caution.
Edges are stored once per pair (src < dst); top_k per disease above min_score.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from itertools import combinations

from ..config import slice_config
from ..graph import Graph
from ..store import GraphWriter
from .ic import hpo_ancestors

log = logging.getLogger(__name__)


def _targets(g: Graph, type_: str, from_src: bool) -> dict[str, set[str]]:
    out: dict[str, set[str]] = defaultdict(set)
    for e in g.edges_of(type_):
        if e["status"] == "hypothesis":
            continue
        if from_src:
            out[e["src"]].add(e["dst"])
        else:
            out[e["dst"]].add(e["src"])
    return out


def _jaccard(a: set[str], b: set[str]) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def run(g: Graph, ic: dict[str, float], w: GraphWriter) -> None:
    cfg = slice_config().analytics
    wts = cfg.get("weights", {"phenotype": 0.6, "mechanism": 0.25, "gene": 0.15})
    top_k, min_score = int(cfg.get("top_k", 10)), float(cfg.get("min_score", 0.15))
    min_ph = int(cfg.get("min_phenotypes", 3))
    caut = cfg.get("caution", {"min_phenotype": 0.35, "max_mechanism": 0.1})

    anc = hpo_ancestors()
    direct = _targets(g, "disease_has_phenotype", from_src=True)
    mech = _targets(g, "disease_involves_mechanism", from_src=True)
    genes = _targets(g, "gene_associated_with_disease", from_src=False)
    diseases = sorted(n["id"] for n in g.by_type("disease"))
    prop = {d: set().union(*(anc.get(t, frozenset()) | {t} for t in direct[d])) if direct[d] else set()
            for d in diseases}

    def ic_jaccard(a: set[str], b: set[str]) -> float:
        union = sum(ic.get(t, 0.0) for t in a | b)
        return sum(ic.get(t, 0.0) for t in a & b) / union if union else 0.0

    scored: dict[str, list[tuple[float, str, dict]]] = defaultdict(list)
    for a, b in combinations(diseases, 2):
        ph = ic_jaccard(prop[a], prop[b]) if len(direct[a]) >= min_ph and len(direct[b]) >= min_ph else 0.0
        me = _jaccard(mech[a], mech[b])
        ge = _jaccard(genes[a], genes[b])
        s = wts["phenotype"] * ph + wts["mechanism"] * me + wts["gene"] * ge
        if s < min_score:
            continue
        comp = {"phenotype": round(ph, 3), "mechanism": round(me, 3), "gene": round(ge, 3)}
        scored[a].append((s, b, comp))
        scored[b].append((s, a, comp))

    keep: dict[tuple[str, str], tuple[float, dict]] = {}
    for d, lst in scored.items():
        for s, o, comp in sorted(lst, reverse=True)[:top_k]:
            keep[(min(d, o), max(d, o))] = (s, comp)

    n_caution = 0
    for (a, b), (s, comp) in keep.items():
        la, lb = g.nodes[a]["label"], g.nodes[b]["label"]
        shared_ph = sorted(set(direct[a]) & set(direct[b]), key=lambda t: -ic.get(t, 0))[:5]
        shared_me = sorted(mech[a] & mech[b])[:3]
        shared_ge = sorted(genes[a] & genes[b])
        parts = []
        if shared_ph:
            parts.append("shared informative symptoms: " + ", ".join(g.nodes[t]["label"] for t in shared_ph if t in g.nodes))
        if shared_me:
            parts.append("shared pathways: " + ", ".join(g.nodes[m]["label"] for m in shared_me))
        if shared_ge:
            parts.append("shared genes: " + ", ".join(g.nodes[x]["label"] for x in shared_ge))
        attrs: dict = {"components": comp}
        if comp["phenotype"] >= caut["min_phenotype"] and comp["mechanism"] <= caut["max_mechanism"] \
                and mech[a] and mech[b]:
            attrs["caution"] = (f"{la} and {lb} look alike clinically but act through different known pathways; "
                                "findings or therapies from one may not transfer to the other.")
            n_caution += 1
        w.edge("disease_similar_to", a, b, status="inferred", score=round(s, 4), label="similar to", attrs=attrs,
               evidence=dict(source_type="computed", source_name="Atlas analytics", source_ref=None,
                             quote=f"{la} ~ {lb} (score {s:.2f}; " + ("; ".join(parts) or "weak overlap") + ")",
                             method="algorithm:ic_jaccard+mechanism+gene:v1"))
    log.info("similarity: %d disease pairs kept (%d with caution) over %d diseases", len(keep), n_caution, len(diseases))
