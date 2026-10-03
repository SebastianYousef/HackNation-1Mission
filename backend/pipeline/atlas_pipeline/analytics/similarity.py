"""disease_similar_to (inferred): weighted mix of
  phenotype  IC-weighted Jaccard over ancestor-propagated HPO sets (informative symptoms dominate)
  mechanism  Jaccard over disease_involves_mechanism targets (hypothesis edges excluded)
  gene       Jaccard over associated genes
Phenotype is only scored when both diseases have >= min_phenotypes direct annotations; otherwise
components.phenotype is omitted (not 0: "not measured" is not "weak overlap") and contributes nothing.
Counterexamples -> attrs.caution (+ attrs.caution_kind), when
  * phenotypically close but mechanistically different (both mechanisms known): "mechanism", or
  * one disease has an approved therapy and the other does not, and they share no gene and are not
    subtype/parent of each other (CLN2 enzyme replacement vs CLN5): "approved_therapy", with
    attrs.therapy_for = the disease that has the therapy (the caution only applies in that direction).
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
    approved: dict[str, list[str]] = defaultdict(list)   # disease -> approved intervention labels
    for e in g.edges_of("intervention_treats_disease"):
        if (e.get("attrs") or {}).get("approval") == "approved" and e["status"] != "hypothesis":
            approved[e["dst"]].append(g.nodes[e["src"]]["label"])
    parents = _targets(g, "disease_subtype_of", from_src=True)

    def lineage(a: str, b: str) -> bool:
        """True if a is b's ancestor or descendant in the MONDO hierarchy."""
        def up(x: str, seen: set[str]) -> set[str]:
            for p in parents.get(x, ()):
                if p not in seen:
                    seen.add(p)
                    up(p, seen)
            return seen
        return a in up(b, set()) or b in up(a, set())
    prop = {d: set().union(*(anc.get(t, frozenset()) | {t} for t in direct[d])) if direct[d] else set()
            for d in diseases}

    def ic_jaccard(a: set[str], b: set[str]) -> float:
        union = sum(ic.get(t, 0.0) for t in a | b)
        return sum(ic.get(t, 0.0) for t in a & b) / union if union else 0.0

    scored: dict[str, list[tuple[float, str, dict]]] = defaultdict(list)
    for a, b in combinations(diseases, 2):
        scored_ph = len(direct[a]) >= min_ph and len(direct[b]) >= min_ph
        ph = ic_jaccard(prop[a], prop[b]) if scored_ph else None
        me = _jaccard(mech[a], mech[b])
        ge = _jaccard(genes[a], genes[b])
        # unscored phenotype contributes 0 (deliberately conservative: no renormalisation)
        s = wts["phenotype"] * (ph or 0.0) + wts["mechanism"] * me + wts["gene"] * ge
        if s < min_score:
            continue
        comp = ({"phenotype": round(ph, 3)} if ph is not None else {}) | {"mechanism": round(me, 3), "gene": round(ge, 3)}
        scored[a].append((s, b, comp))
        scored[b].append((s, a, comp))

    keep: dict[tuple[str, str], tuple[float, dict]] = {}
    for d, lst in scored.items():
        for s, o, comp in sorted(lst, key=lambda x: (-x[0], x[1]))[:top_k]:
            keep[(min(d, o), max(d, o))] = (s, comp)

    n_caution = 0
    for (a, b), (s, comp) in keep.items():
        la, lb = g.nodes[a]["label"], g.nodes[b]["label"]
        shared_ph = sorted(set(direct[a]) & set(direct[b]), key=lambda t: (-ic.get(t, 0), t))[:5]
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
        if comp.get("phenotype") is not None and comp["phenotype"] >= caut["min_phenotype"] \
                and comp["mechanism"] <= caut["max_mechanism"] and mech[a] and mech[b]:
            attrs["caution_kind"] = "mechanism"
            attrs["caution"] = (f"{la} and {lb} look alike clinically but act through different known pathways; "
                                "findings or therapies from one may not transfer to the other.")
        elif bool(approved[a]) != bool(approved[b]) and not genes[a] & genes[b] and not lineage(a, b):
            (x, lx), (y, ly) = ((a, la), (b, lb)) if approved[a] else ((b, lb), (a, la))
            attrs.update(caution_kind="approved_therapy", therapy_for=x)
            attrs["caution"] = (f"{lx} has an approved therapy ({', '.join(sorted(approved[x]))}); it is approved "
                                f"for {lx} only, and whether it helps {ly} is untested.")
        n_caution += "caution" in attrs
        w.edge("disease_similar_to", a, b, status="inferred", score=round(s, 4), label="similar to", attrs=attrs,
               evidence=dict(source_type="computed", source_name="Atlas analytics", source_ref=None,
                             quote=f"{la} ~ {lb} (score {s:.2f}; " + ("; ".join(parts) or "weak overlap") + ")",
                             method="algorithm:ic_jaccard+mechanism+gene:v1"))
    log.info("similarity: %d disease pairs kept (%d with caution) over %d diseases", len(keep), n_caution, len(diseases))
