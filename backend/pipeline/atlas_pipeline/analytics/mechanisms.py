"""disease_involves_mechanism by propagation: disease <-[gene_associated_with_disease]- gene
-[gene_in_mechanism]-> pathway, for pathways with <= analytics.max_pathway_genes genes.

Database gene -> pathway links (status curated, e.g. Reactome TAS) always propagate. A hand-curated mechanism
(ATLAS:mech-* from curated.yaml, literature gene links) lists its diseases explicitly with quotes, so its gene
links propagate only through a gene of one of those quoted diseases: CLN5 gene -> soluble lysosomal protein
deficiency reaches the CLN5 subtypes because CLN5 disease is quoted there, but CLN5 interacting with the
CLN6-CLN8 EGRESS complex does not make CLN5 disease an ER-to-Golgi trafficking defect."""
from __future__ import annotations

import logging
from collections import defaultdict

from ..config import slice_config
from ..graph import Graph
from ..store import GraphWriter

log = logging.getLogger(__name__)


def run(g: Graph) -> None:
    maxg = int(slice_config().analytics.get("max_pathway_genes", 200))
    gene_dis = defaultdict(set)
    for e in g.edges_of("gene_associated_with_disease"):
        if e["status"] == "curated":
            gene_dis[e["src"]].add(e["dst"])
    dis_genes = defaultdict(set)   # disease -> genes (any non-hypothesis association)
    for e in g.edges_of("gene_associated_with_disease"):
        if e["status"] != "hypothesis":
            dis_genes[e["dst"]].add(e["src"])
    vouched = defaultdict(set)     # hand-curated mechanism -> genes of its quoted diseases
    for e in g.edges_of("disease_involves_mechanism"):
        if e["status"] in ("curated", "literature"):
            vouched[e["dst"]] |= dis_genes[e["src"]]
    w = GraphWriter("mechanisms")
    for e in g.edges_of("gene_in_mechanism"):
        if e["status"] == "hypothesis" or (e["status"] != "curated" and e["src"] not in vouched[e["dst"]]):
            continue
        m = g.nodes[e["dst"]]
        n = (m.get("attrs") or {}).get("n_genes")
        if n is not None and n > maxg:
            continue
        gene = g.nodes[e["src"]]["label"]
        for d in sorted(gene_dis.get(e["src"], ())):
            w.edge("disease_involves_mechanism", d, e["dst"], status="inferred", score=0.7, label="involves",
                   attrs={"via_genes": [gene]},
                   evidence=dict(source_type="computed", source_name="Atlas analytics", source_ref=e["id"],
                                 quote=f"{gene} is linked to {g.nodes[d]['label']} and acts in {m['label']}",
                                 method="algorithm:gene_pathway_propagation"))
    # merge via_genes lists for diseases reached through several genes
    for e in w.edges.values():
        genes = sorted({v["quote"].split(" is linked")[0] for v in w.evidence.values() if v["edge_id"] == e["id"]})
        e["attrs"]["via_genes"] = genes
    w.close()
