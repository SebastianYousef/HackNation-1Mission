"""Precomputed explainable routes from every focus disease to patient groups, assets, researchers,
trials, interventions and related diseases.

Cost of an edge = -ln(confidence) + hub penalty (high-degree phenotype nodes are weak bridges:
"both have seizures" connects half the slice). Dijkstra from the disease; the cheapest route to each
target is kept, then the best `paths_per_kind` per kind with <= max_path_len edges.
Score = product of edge confidences x 0.85^(hops-1) x hub factor. Writes paths.jsonl."""
from __future__ import annotations

import json
import logging
import math

import networkx as nx

from ..config import slice_config
from ..graph import Graph
from ..ids import path_id
from ..store import read_json, write_jsonl

log = logging.getLogger(__name__)

# person_authored/publication_about make researcher paths; skip structural edges that add noise
SKIP_TYPES = {"disease_subtype_of"}
GROUP_SUBTYPES = {"patient_group", "foundation"}
PHENO_BRIDGE = 0.35  # extra cost per edge touching a phenotype: one shared symptom is a weak bridge


def target_kind(n: dict) -> str | None:
    t, st = n["type"], n.get("subtype")
    if t == "organization":
        return "patient_group" if st in GROUP_SUBTYPES else None
    return {"asset": "asset", "person": "researcher", "trial": "trial",
            "intervention": "intervention", "disease": "related_disease"}.get(t)


def build_nx(g: Graph, hub_degree: int) -> nx.Graph:
    deg: dict[str, int] = {}
    for e in g.edges.values():
        for x in (e["src"], e["dst"]):
            deg[x] = deg.get(x, 0) + 1
    gx = nx.Graph()
    for e in g.edges.values():
        if e["type"] in SKIP_TYPES or e["status"] == "hypothesis":
            continue
        a, b = e["src"], e["dst"]
        cost = -math.log(max(e["confidence"], 0.01))
        for x in (a, b):
            if g.nodes[x]["type"] == "phenotype":
                cost += PHENO_BRIDGE
                if deg[x] > hub_degree:
                    cost += math.log(deg[x] / hub_degree) + 0.5
        old = gx.get_edge_data(a, b)
        if old is None or cost < old["cost"]:
            gx.add_edge(a, b, cost=cost, eid=e["id"])
    return gx


def run(g: Graph) -> list[dict]:
    cfg = slice_config().analytics
    per_kind, max_len = int(cfg.get("paths_per_kind", 5)), int(cfg.get("max_path_len", 4))
    gx = build_nx(g, int(cfg.get("hub_degree", 25)))
    focus = [d for d in (read_json("slice", {}) or {}).get("focus", []) if d in gx]
    rows: list[dict] = []
    for d in focus:
        dist, routes = nx.single_source_dijkstra(gx, d, cutoff=None, weight="cost")
        best: dict[str, list[tuple[float, list[str]]]] = {}
        for t, nodes in routes.items():
            if t == d or len(nodes) - 1 > max_len:
                continue
            kind = target_kind(g.nodes[t])
            if kind is None:
                continue
            # a route must not pass *through* another disease's patient group/person/trial etc.
            if any(target_kind(g.nodes[x]) in {"patient_group", "researcher", "trial", "asset"} for x in nodes[1:-1]):
                continue
            hops = len(nodes) - 1
            score = math.exp(-dist[t]) * 0.85 ** (hops - 1)
            best.setdefault(kind, []).append((score, nodes))
        for kind, lst in best.items():
            for score, nodes in sorted(lst, key=lambda x: (-x[0], x[1]))[:per_kind]:
                eids = [gx.edges[a, b]["eid"] for a, b in zip(nodes, nodes[1:])]
                labels = [g.nodes[n]["label"] for n in nodes]
                rows.append({"id": path_id(kind, nodes), "from_id": d, "to_id": nodes[-1], "kind": kind,
                             "title": " → ".join(labels), "node_ids": nodes, "edge_ids": eids,
                             "score": round(score, 4), "attrs": {"hops": len(eids)}})
    write_jsonl("paths.jsonl", rows)
    log.info("paths: %d paths from %d focus diseases", len(rows), len(focus))
    log.debug("example: %s", json.dumps(rows[:1]))
    return rows
