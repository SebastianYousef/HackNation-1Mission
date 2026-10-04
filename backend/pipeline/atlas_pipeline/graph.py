"""Merge every stage's interim graph files into one consistent graph.

Rules:
 * nodes merge by id; first stage in STAGE_ORDER wins for label/type; synonyms/xrefs union; attrs fill.
 * edges merge by id; status = strongest; attrs fill; evidence = union (dedup by evidence id).
 * edge confidence is recomputed here from status + evidence (confidence.py).
 * quote guardrail: a 'literature' edge needs >= 1 'supports' evidence row with a non-empty verbatim
   quote that is not a web scrape (method scrape:*, e.g. Bright Data leads). One without is downgraded
   to 'hypothesis' (counted in `downgraded`, logged) instead of failing the load; load.validate then
   re-checks the invariant on the final graph.
 * edges whose endpoints are missing are dropped (counted in `dropped`), and so are edges without
   evidence and literature/hypothesis edges with no 'supports' row (their text evidence argues against them).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from collections import defaultdict

from .config import INTERIM
from .confidence import edge_confidence, strongest_status
from .store import read_jsonl

log = logging.getLogger(__name__)

STAGE_ORDER = ["mondo", "hgnc", "hpo", "orphanet", "clinvar", "reactome", "curated", "medlineplus", "pubmed",
               "clinicaltrials", "nih_reporter", "brightdata_orgs", "reconcile", "mechanisms", "analytics"]


@dataclass
class Graph:
    nodes: dict[str, dict] = field(default_factory=dict)
    edges: dict[str, dict] = field(default_factory=dict)
    evidence: dict[str, list[dict]] = field(default_factory=lambda: defaultdict(list))  # edge_id -> rows
    dropped: int = 0
    downgraded: int = 0    # literature -> hypothesis by the quote guardrail

    # ---- helpers used by analytics / views -------------------------------------------------
    def by_type(self, t: str) -> list[dict]:
        return [n for n in self.nodes.values() if n["type"] == t]

    def adjacency(self) -> dict[str, list[dict]]:
        adj: dict[str, list[dict]] = defaultdict(list)
        for e in self.edges.values():
            adj[e["src"]].append(e)
            adj[e["dst"]].append(e)
        return adj

    def edges_of(self, type_: str) -> list[dict]:
        return [e for e in self.edges.values() if e["type"] == type_]


# evidence methods that may never back a 'literature' claim: web scrapes are unverified leads (the
# brightdata_orgs stage writes them as hypothesis-only), so their quotes do not satisfy the guardrail
NON_LITERATURE_METHODS = ("scrape:",)


def has_quoted_support(evidence: list[dict] | None) -> bool:
    """True if some 'supports' row from a stage that may assert literature carries a non-empty quote
    (what 'literature' status requires). Scraped rows (method scrape:*) never count."""
    return any(v["stance"] == "supports" and (v.get("quote") or "").strip()
               and not str(v.get("method") or "").startswith(NON_LITERATURE_METHODS) for v in evidence or [])


def _stages_present(exclude: set[str]) -> list[str]:
    found = {p.name.split(".")[0] for p in INTERIM.glob("*.nodes.jsonl")} | \
            {p.name.split(".")[0] for p in INTERIM.glob("*.edges.jsonl")}
    ordered = [s for s in STAGE_ORDER if s in found] + sorted(found - set(STAGE_ORDER))
    return [s for s in ordered if s not in exclude]


def load_graph(exclude: set[str] | None = None) -> Graph:
    g = Graph()
    stages = _stages_present(exclude or set())
    for st in stages:
        for n in read_jsonl(f"{st}.nodes.jsonl"):
            old = g.nodes.get(n["id"])
            if old is None:
                g.nodes[n["id"]] = n
                continue
            old["synonyms"] = sorted(set(old["synonyms"]) | set(n.get("synonyms") or []))
            for k, v in (n.get("xrefs") or {}).items():
                old["xrefs"][k] = sorted(set(old["xrefs"].get(k, [])) | set(v))
            for k, v in (n.get("attrs") or {}).items():
                old["attrs"].setdefault(k, v)
            for k in ("description", "plain_summary", "url", "subtype"):
                old[k] = old.get(k) or n.get(k)
    for st in stages:
        for p in read_jsonl(f"{st}.node_patches.jsonl"):
            n = g.nodes.get(p["id"])
            if n is None:
                continue
            n["attrs"].update(p.get("attrs") or {})
            for k in ("plain_summary", "subtype", "description"):
                if p.get(k):
                    n[k] = p[k]
    for st in stages:
        for e in read_jsonl(f"{st}.edges.jsonl"):
            old = g.edges.get(e["id"])
            if old is None:
                g.edges[e["id"]] = e
                continue
            old["status"] = strongest_status([old["status"], e["status"]])
            for k, v in (e.get("attrs") or {}).items():
                old["attrs"].setdefault(k, v)
            if e.get("score") is not None:
                old["score"] = max(old.get("score") or 0, e["score"])
            old["label"] = old.get("label") or e.get("label")
        seen: set[str] = set()
        for v in read_jsonl(f"{st}.evidence.jsonl"):
            if v["id"] in seen:
                continue
            seen.add(v["id"])
            g.evidence[v["edge_id"]].append(v)
    # quote guardrail: 'literature' without a quoted supporting row is not literature
    downgraded_by: dict[str, int] = defaultdict(int)
    for e in g.edges.values():
        if e["status"] == "literature" and not has_quoted_support(g.evidence.get(e["id"])):
            e["status"] = "hypothesis"
            g.downgraded += 1
            downgraded_by[e["type"]] += 1
    if g.downgraded:
        log.warning("graph: %d literature edges have no quoted supporting evidence -> hypothesis (%s)",
                    g.downgraded, dict(downgraded_by))
    # drop dangling edges, edges without evidence, and text-derived edges nothing supports
    for eid in list(g.edges):
        e = g.edges[eid]
        ev = g.evidence.get(eid)
        unsupported = e["status"] in ("literature", "hypothesis") and not any(v["stance"] == "supports" for v in ev or [])
        if e["src"] not in g.nodes or e["dst"] not in g.nodes or not ev or unsupported:
            del g.edges[eid]
            g.dropped += 1
    for eid in list(g.evidence):
        if eid not in g.edges:
            del g.evidence[eid]
        else:
            uniq = {v["id"]: v for v in g.evidence[eid]}
            g.evidence[eid] = list(uniq.values())
    for e in g.edges.values():
        e["confidence"] = edge_confidence(e["status"], g.evidence[e["id"]], e.get("score"))
    log.info("graph: %d nodes, %d edges, %d evidence, %d dropped, %d downgraded (stages: %s)", len(g.nodes),
             len(g.edges), sum(len(v) for v in g.evidence.values()), g.dropped, g.downgraded, ",".join(stages))
    return g
