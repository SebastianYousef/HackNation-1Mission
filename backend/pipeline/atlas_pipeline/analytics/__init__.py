"""Analytics stage.

 0. mechanisms   disease_involves_mechanism (inferred) = disease <-gene-> Reactome pathway (size-capped)
 a. ic           phenotype information content from HPO annotation frequency (all HPOA diseases,
                 ancestor-propagated): ic(t) = -ln(#diseases annotated with t or a descendant / #diseases)
 b. similarity   disease_similar_to = weighted mix of IC-weighted Jaccard over ancestor-propagated
                 phenotype sets, mechanism Jaccard and shared-gene Jaccard
 c. caution      counterexamples: high phenotype similarity but low mechanism similarity
 d. clusters     Leiden (igraph+leidenalg) on the similarity graph; fallback networkx Louvain
 e. overlap      people linked to diseases in different clusters
 f. paths        best explainable routes from focus diseases to groups/assets/people/trials/diseases
Outputs: data/interim/{mechanisms,analytics}.*.jsonl, clusters.jsonl, cluster_members.jsonl,
         overlap.jsonl, paths.jsonl
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)


def run() -> None:
    from ..graph import load_graph
    from . import clusters, ic, mechanisms, overlap, paths, similarity
    from ..store import GraphWriter, write_node_patches

    g = load_graph(exclude={"analytics", "mechanisms"})
    mechanisms.run(g)
    g = load_graph(exclude={"analytics"})
    ic_map = ic.compute()
    write_node_patches("analytics", [{"id": t, "attrs": {"ic": round(v, 4)}} for t, v in ic_map.items() if t in g.nodes])
    w = GraphWriter("analytics")
    similarity.run(g, ic_map, w)
    w.close()
    g = load_graph()
    cl = clusters.run(g)
    overlap.run(g, cl)
    paths.run(g)
