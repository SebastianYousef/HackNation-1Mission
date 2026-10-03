"""Leiden communities on the disease similarity graph (fallback: networkx Louvain).
Writes clusters.jsonl + cluster_members.jsonl; returns {cluster_id: [disease ids]}."""
from __future__ import annotations

import logging
from collections import Counter, defaultdict

from ..graph import Graph
from ..ids import slug
from ..store import write_jsonl

log = logging.getLogger(__name__)
SEED = 42


def _communities(nodes: list[str], wedges: list[tuple[str, str, float]]) -> tuple[list[set[str]], str]:
    try:
        import igraph as ig
        import leidenalg
        idx = {n: i for i, n in enumerate(nodes)}
        gr = ig.Graph(n=len(nodes), edges=[(idx[a], idx[b]) for a, b, _ in wedges])
        part = leidenalg.find_partition(gr, leidenalg.RBConfigurationVertexPartition,
                                        weights=[w for *_, w in wedges], resolution_parameter=1.0, seed=SEED)
        return [{nodes[i] for i in c} for c in part], "leiden:rb-config:similarity:v1"
    except ImportError:
        import networkx as nx
        gx = nx.Graph()
        gx.add_nodes_from(nodes)
        gx.add_weighted_edges_from(wedges)
        return [set(c) for c in nx.community.louvain_communities(gx, weight="weight", seed=SEED)], \
            "louvain:networkx:similarity:v1"


def run(g: Graph) -> dict[str, list[str]]:
    sim = g.edges_of("disease_similar_to")
    nodes = sorted({x for e in sim for x in (e["src"], e["dst"])})
    if not nodes:
        log.warning("clusters: no similarity edges")
        write_jsonl("clusters.jsonl", [])
        write_jsonl("cluster_members.jsonl", [])
        return {}
    wedges = [(e["src"], e["dst"], float(e["score"] or 0.0)) for e in sim]
    comms, method = _communities(nodes, wedges)

    strength: dict[str, dict[str, float]] = defaultdict(dict)   # node -> neighbour -> weight
    for a, b, w in wedges:
        strength[a][b] = w
        strength[b][a] = w
    pheno, mech = defaultdict(set), defaultdict(set)
    for e in g.edges_of("disease_has_phenotype"):
        pheno[e["src"]].add(e["dst"])
    for e in g.edges_of("disease_involves_mechanism"):
        if e["status"] != "hypothesis":
            mech[e["src"]].add(e["dst"])

    clusters, members, out = [], [], {}
    used: set[str] = set()
    for comm in sorted(comms, key=len, reverse=True):
        if len(comm) < 2:
            continue
        ph = Counter(t for d in comm for t in pheno[d])
        me = Counter(m for d in comm for m in mech[d])
        # informative = frequent inside the cluster AND specific (high IC)
        top_ph = sorted((t for t, c in ph.items() if c >= 2 and t in g.nodes),
                        key=lambda t: -(ph[t] * (g.nodes[t]["attrs"].get("ic") or 0)))[:6]
        top_me = [m for m, c in me.most_common(6) if c >= 2]
        anchor = g.nodes[top_me[0]]["label"] if top_me else (g.nodes[top_ph[0]]["label"] if top_ph else None)
        hub = max(comm, key=lambda d: sum(strength[d].get(o, 0) for o in comm))
        label = anchor or g.nodes[hub]["label"]
        cid = "CL:" + slug(label, 40)
        while cid in used:
            cid += "-2"
        used.add(cid)
        inner = [w for a, b, w in wedges if a in comm and b in comm]
        cohesion = round(sum(inner) / max(1, len(inner)), 3)
        ex = ", ".join(sorted(g.nodes[d]["label"] for d in comm)[:3])
        clusters.append({
            "id": cid, "label": f"{label} cluster", "method": method, "size": len(comm),
            "summary": f"{len(comm)} diseases (e.g. {ex}) grouped by shared informative symptoms and pathways.",
            "attrs": {"cohesion": cohesion, "top_phenotypes": top_ph, "top_mechanisms": top_me, "hub": hub}})
        for d in comm:
            tot = sum(strength[d].values()) or 1.0
            members.append({"cluster_id": cid, "node_id": d,
                            "membership": round(sum(strength[d].get(o, 0) for o in comm) / tot, 3)})
        out[cid] = sorted(comm)
    write_jsonl("clusters.jsonl", clusters)
    write_jsonl("cluster_members.jsonl", members)
    log.info("clusters: %d clusters (%s) over %d diseases", len(clusters), method, len(nodes))
    return out
