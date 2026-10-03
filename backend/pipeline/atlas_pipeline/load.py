"""Validate the merged graph + analytics + views and load them into Postgres in ONE transaction
(delete + COPY). Invariants are checked before anything is written; a violation aborts the load.
Requires the migrations in backend/db/migrations to be applied."""
from __future__ import annotations

import logging
from collections import Counter

from .analytics.paths import path_strength
from .config import INTERIM, slice_config
from .graph import Graph, has_quoted_support, load_graph
from .llm import usage
from .models import EDGE_STATUSES, EDGE_TYPES, NODE_TYPES, PATH_KINDS, SOURCE_TYPES, STANCES, now_iso
from .store import read_json, read_jsonl

log = logging.getLogger(__name__)

# Replaced on every load, children first so no FK action fires. submissions is never touched (its
# node_id is a plain CURIE since migration 20261003000003, so it survives reloads).
TABLES = ["evidence", "node_names", "cluster_members", "paths", "edges", "clusters", "nodes", "views",
          "explanations", "dataset_meta"]

ACTION_KEYS = {"disease", "headline", "plain_summary", "treatment_status", "exact_groups", "related_communities",
               "connections", "assets", "trials", "researchers", "shared_people", "next_steps", "coverage"}
MECH_KEYS = {"mechanism", "headline", "plain_summary", "genes", "diseases", "clusters", "researchers", "trials",
             "interventions"}


def _rows(name: str) -> list[dict]:
    return list(read_jsonl(name)) if (INTERIM / name).exists() else []


def node_names(g: Graph) -> list[tuple[str, str, str]]:
    out: dict[tuple[str, str], str] = {}
    rank = {"id": 0, "label": 1, "abbreviation": 2, "synonym": 3, "xref": 4}

    def add(nid: str, name: str | None, kind: str) -> None:
        name = (name or "").strip()
        if not name:
            return
        old = out.get((nid, name))
        if old is None or rank[kind] < rank[old]:
            out[(nid, name)] = kind
    for n in g.nodes.values():
        add(n["id"], n["id"], "id")
        add(n["id"], n["label"], "label")
        for s in n.get("synonyms") or []:
            add(n["id"], s, "abbreviation" if len(s) <= 6 and s.isupper() else "synonym")
        for k, vs in (n.get("xrefs") or {}).items():
            for v in vs:
                add(n["id"], v if ":" in v else f"{k}:{v}", "xref")
    return [(nid, name, kind) for (nid, name), kind in out.items()]


def validate(g: Graph, clusters: list[dict], members: list[dict], paths: list[dict], views: list[dict]) -> list[str]:
    errs: list[str] = []
    for n in g.nodes.values():
        if n["type"] not in NODE_TYPES:
            errs.append(f"node {n['id']}: bad type {n['type']}")
    for e in g.edges.values():
        if e["type"] not in EDGE_TYPES or e["status"] not in EDGE_STATUSES:
            errs.append(f"edge {e['id']}: bad type/status {e['type']}/{e['status']}")
        if not 0 <= e["confidence"] <= 1:
            errs.append(f"edge {e['id']}: confidence {e['confidence']}")
        if not g.evidence.get(e["id"]):
            errs.append(f"edge {e['id']}: no evidence")
        elif e["status"] == "literature" and not has_quoted_support(g.evidence[e["id"]]):
            # graph.load_graph downgrades these to 'hypothesis'; reaching here means something re-upgraded one
            errs.append(f"edge {e['id']}: literature without a quoted (non-scraped) supporting evidence row")
        for v in g.evidence.get(e["id"], []):
            if v["stance"] not in STANCES or v["source_type"] not in SOURCE_TYPES:
                errs.append(f"evidence {v['id']}: bad stance/source_type")
    cids = {c["id"] for c in clusters}
    errs += [f"member {m['node_id']}: unknown cluster/node" for m in members
             if m["cluster_id"] not in cids or m["node_id"] not in g.nodes]
    for p in paths:
        ok = p["kind"] in PATH_KINDS and len(p["edge_ids"]) == len(p["node_ids"]) - 1 >= 1 \
            and len(set(p["node_ids"])) == len(p["node_ids"])
        for i, eid in enumerate(p["edge_ids"]):
            e = g.edges.get(eid)
            ok = ok and e is not None and {e["src"], e["dst"]} == {p["node_ids"][i], p["node_ids"][i + 1]}
        if not ok:
            errs.append(f"path {p['id']}: invalid")
        elif (p.get("weakest_status"), p.get("min_confidence")) != path_strength(g, p["edge_ids"], p.get("attrs") or {}):
            errs.append(f"path {p['id']}: weakest_status/min_confidence missing or stale (re-run analytics)")
    for v in views:
        need = ACTION_KEYS if v["kind"] == "action" else MECH_KEYS
        if set(v["payload"]) != need:
            errs.append(f"view {v['kind']}/{v['key']}: keys differ from contract: {sorted(set(v['payload']) ^ need)}")
    return errs


def run(database_url: str | None, dry_run: bool = False) -> None:
    import psycopg
    from psycopg.types.json import Jsonb

    g = load_graph()
    clusters, members = _rows("clusters.jsonl"), _rows("cluster_members.jsonl")
    paths, views = _rows("paths.jsonl"), _rows("views.jsonl")
    for e in g.edges.values():
        ev = g.evidence[e["id"]]
        e["support_count"] = sum(v["stance"] == "supports" for v in ev)
        e["contradict_count"] = sum(v["stance"] == "contradicts" for v in ev)
        e["sources"] = sorted({v["source_name"] for v in ev})

    errs = validate(g, clusters, members, paths, views)
    if errs:
        for x in errs[:30]:
            log.error(x)
        raise SystemExit(f"load aborted: {len(errs)} invariant violations")
    names = node_names(g)
    cfg = slice_config()
    built = now_iso()
    # unique per load: the API cache is keyed on this, so every reload invalidates it
    version = f"{cfg.version}+{built[:19].replace('-', '').replace(':', '').replace('T', '.')}"
    meta = {"dataset": {"version": version, "slice": cfg.name, "built_at": built,
                        "focus_diseases": len((read_json("slice", {}) or {}).get("focus", []))},
            "openai_usage": usage()}
    log.info("validated: %d nodes, %d names, %d edges (%s), %d clusters, %d paths, %d views",
             len(g.nodes), len(names), len(g.edges), dict(Counter(e["status"] for e in g.edges.values())),
             len(clusters), len(paths), len(views))
    if dry_run:
        return
    if not database_url:
        raise SystemExit("DATABASE_URL is not set")

    with psycopg.connect(database_url, prepare_threshold=None) as conn, conn.cursor() as cur:
        # DELETE, not TRUNCATE: TRUNCATE takes ACCESS EXCLUSIVE until commit and blocks every API read
        # (even dataset_version()); DELETE lets readers keep the old snapshot until the single commit.
        # The advisory lock serialises concurrent loads.
        cur.execute("select pg_advisory_xact_lock(hashtext('atlas_pipeline.load'))")
        for t in TABLES:
            cur.execute(f"delete from {t}")

        def copy(table: str, cols: list[str], rows) -> None:
            with cur.copy(f"copy {table} ({', '.join(cols)}) from stdin") as cp:
                for r in rows:
                    cp.write_row(r)

        copy("nodes", ["id", "type", "subtype", "label", "description", "plain_summary", "synonyms", "xrefs",
                       "attrs", "url"],
             ((n["id"], n["type"], n.get("subtype"), n["label"], n.get("description"), n.get("plain_summary"),
               list(n.get("synonyms") or []), Jsonb(n.get("xrefs") or {}), Jsonb(n.get("attrs") or {}), n.get("url"))
              for n in g.nodes.values()))
        copy("node_names", ["node_id", "name", "kind"], names)
        copy("edges", ["id", "src", "dst", "type", "status", "confidence", "score", "label", "attrs",
                       "support_count", "contradict_count", "sources"],
             ((e["id"], e["src"], e["dst"], e["type"], e["status"], e["confidence"], e.get("score"), e.get("label"),
               Jsonb(e.get("attrs") or {}), e["support_count"], e["contradict_count"], e["sources"])
              for e in g.edges.values()))
        copy("evidence", ["id", "edge_id", "stance", "source_type", "source_name", "source_ref", "url", "quote",
                          "method", "published_at", "retrieved_at"],
             ((v["id"], v["edge_id"], v["stance"], v["source_type"], v["source_name"], v.get("source_ref"),
               v.get("url"), v.get("quote"), v["method"], v.get("published_at"), v["retrieved_at"])
              for vs in g.evidence.values() for v in vs))
        copy("clusters", ["id", "label", "summary", "method", "size", "attrs"],
             ((c["id"], c["label"], c.get("summary"), c["method"], c["size"], Jsonb(c.get("attrs") or {}))
              for c in clusters))
        copy("cluster_members", ["cluster_id", "node_id", "membership"],
             ((m["cluster_id"], m["node_id"], m["membership"]) for m in members))
        copy("paths", ["id", "from_id", "to_id", "kind", "title", "node_ids", "edge_ids", "score",
                       "weakest_status", "min_confidence", "attrs"],
             ((p["id"], p["from_id"], p["to_id"], p["kind"], p["title"], p["node_ids"], p["edge_ids"], p["score"],
               p["weakest_status"], p["min_confidence"], Jsonb(p.get("attrs") or {})) for p in paths))
        copy("views", ["kind", "key", "payload"], ((v["kind"], v["key"], Jsonb(v["payload"])) for v in views))
        copy("dataset_meta", ["key", "value"], ((k, Jsonb(v)) for k, v in meta.items()))
    log.info("loaded dataset %s into Postgres", version)
