"""Network overlap: people (researchers, clinicians, funders) linked to diseases in different
clusters — the brief's "two unrelated communities already share a key opinion leader".
A person is linked to a disease by person_studies, person_authored -> publication_about,
or grant_funds_person <- grant -> grant_studies. Writes overlap.jsonl."""
from __future__ import annotations

import logging
from collections import defaultdict

from ..graph import Graph
from ..store import write_jsonl

log = logging.getLogger(__name__)


def person_disease_links(g: Graph) -> dict[str, dict[str, set[str]]]:
    """person -> disease -> edge ids that justify the link."""
    links: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    is_dis = lambda x: g.nodes[x]["type"] == "disease"  # noqa: E731
    about = defaultdict(list)    # publication -> [(disease, edge)]
    studies = defaultdict(list)  # grant -> [(disease, edge)]
    for e in g.edges_of("publication_about"):
        if is_dis(e["dst"]):
            about[e["src"]].append((e["dst"], e["id"]))
    for e in g.edges_of("grant_studies"):
        if is_dis(e["dst"]):
            studies[e["src"]].append((e["dst"], e["id"]))
    for e in g.edges_of("person_studies"):
        if is_dis(e["dst"]):
            links[e["src"]][e["dst"]].add(e["id"])
    for e in g.edges_of("person_authored"):
        for d, e2 in about.get(e["dst"], ()):
            links[e["src"]][d].update((e["id"], e2))
    for e in g.edges_of("grant_funds_person"):
        for d, e2 in studies.get(e["src"], ()):
            links[e["dst"]][d].update((e["id"], e2))
    return links


def run(g: Graph, clusters: dict[str, list[str]]) -> list[dict]:
    cluster_of = {d: c for c, ds in clusters.items() for d in ds}
    rows = []
    for person, dmap in person_disease_links(g).items():
        cl = {cluster_of[d] for d in dmap if d in cluster_of}
        if len(cl) < 2:
            continue
        rows.append({"person": person, "diseases": sorted(dmap), "clusters": sorted(cl),
                     "edge_ids": sorted({x for s in dmap.values() for x in s})})
    rows.sort(key=lambda r: (-len(r["clusters"]), -len(r["diseases"]), r["person"]))
    write_jsonl("overlap.jsonl", rows)
    log.info("overlap: %d people bridge >= 2 clusters", len(rows))
    return rows
