"""Interim storage. Every stage writes `data/interim/<stage>.{nodes,edges,evidence}.jsonl`
(plus free-form tables as parquet/jsonl). `graph.load_graph()` merges all of them.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Iterable, Iterator

import pandas as pd

from .config import INTERIM
from .ids import edge_id, evidence_id
from .models import EDGE_TYPES, Evidence, Node, now_iso

log = logging.getLogger(__name__)


def write_jsonl(path: Path | str, rows: Iterable[dict]) -> int:
    path = Path(path)
    if not path.is_absolute():
        path = INTERIM / path
    path.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w") as f:
        for r in rows:
            f.write(json.dumps(r, default=str, ensure_ascii=False) + "\n")
            n += 1
    tmp.replace(path)
    return n


def read_jsonl(path: Path | str) -> Iterator[dict]:
    path = Path(path)
    if not path.is_absolute():
        path = INTERIM / path
    if not path.exists():
        return
    with open(path) as f:
        for line in f:
            if line.strip():
                yield json.loads(line)


def write_parquet(name: str, df: pd.DataFrame) -> Path:
    p = INTERIM / f"{name}.parquet"
    p.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(p, index=False)
    return p


def read_parquet(name: str) -> pd.DataFrame:
    p = INTERIM / f"{name}.parquet"
    if not p.exists():
        raise FileNotFoundError(f"{p} missing — run the stage that produces it first")
    return pd.read_parquet(p)


def write_json(name: str, obj: Any) -> Path:
    p = INTERIM / f"{name}.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, indent=1, default=str, ensure_ascii=False))
    return p


def read_json(name: str, default: Any = None) -> Any:
    p = INTERIM / f"{name}.json"
    return json.loads(p.read_text()) if p.exists() else default


class GraphWriter:
    """Collects nodes / edges / evidence for one stage and writes them atomically.

    Usage:
        g = GraphWriter("hpo")
        g.node(id=..., type=..., label=...)
        g.edge("disease_has_phenotype", d, p, status="curated",
               evidence=dict(source_type="database", source_name="HPO", source_ref=..., quote=...))
        g.close()
    """

    def __init__(self, stage: str):
        self.stage = stage
        self.nodes: dict[str, dict] = {}
        self.edges: dict[str, dict] = {}
        self.evidence: dict[str, dict] = {}
        self.retrieved_at = now_iso()

    def node(self, **kw: Any) -> str:
        n = Node(**kw).model_dump()
        old = self.nodes.get(n["id"])
        if old:
            old["synonyms"] = sorted(set(old["synonyms"]) | set(n["synonyms"]))
            for k, v in n["xrefs"].items():
                old["xrefs"][k] = sorted(set(old["xrefs"].get(k, [])) | set(v))
            for k, v in n["attrs"].items():
                old["attrs"].setdefault(k, v)
            for k in ("description", "plain_summary", "url", "subtype"):
                old[k] = old[k] or n[k]
        else:
            self.nodes[n["id"]] = n
        return n["id"]

    def edge(self, type_: str, src: str, dst: str, *, status: str, evidence: dict | list[dict],
             score: float | None = None, label: str | None = None, attrs: dict | None = None) -> str:
        assert type_ in EDGE_TYPES, type_
        eid = edge_id(type_, src, dst)
        e = self.edges.get(eid)
        if e is None:
            self.edges[eid] = dict(id=eid, src=src, dst=dst, type=type_, status=status, score=score,
                                   label=label, attrs=dict(attrs or {}))
        else:
            if attrs:
                for k, v in attrs.items():
                    e["attrs"].setdefault(k, v)
            if score is not None:
                e["score"] = max(score, e["score"] or 0)
            from .confidence import strongest_status
            e["status"] = strongest_status([e["status"], status])
        for ev in evidence if isinstance(evidence, list) else [evidence]:
            ev = dict(ev)
            ev.setdefault("retrieved_at", self.retrieved_at)
            ev.setdefault("method", "curated")
            ev.setdefault("stance", "supports")
            vid = evidence_id(eid, ev["source_name"], ev.get("source_ref"), ev["method"], ev.get("quote"))
            self.evidence[vid] = Evidence(id=vid, edge_id=eid, **ev).model_dump(mode="json")
        return eid

    def close(self) -> dict[str, int]:
        quoted = {v["edge_id"] for v in self.evidence.values()
                  if v["stance"] == "supports" and (v.get("quote") or "").strip()}
        unquoted = sum(e["status"] == "literature" and e["id"] not in quoted for e in self.edges.values())
        if unquoted:  # graph.load_graph downgrades these to 'hypothesis'; flag it at the source
            log.warning("[%s] %d literature edges have no quoted supporting evidence", self.stage, unquoted)
        counts = {
            "nodes": write_jsonl(f"{self.stage}.nodes.jsonl", self.nodes.values()),
            "edges": write_jsonl(f"{self.stage}.edges.jsonl", self.edges.values()),
            "evidence": write_jsonl(f"{self.stage}.evidence.jsonl", self.evidence.values()),
        }
        log.info("[%s] wrote %s", self.stage, counts)
        return counts


def write_node_patches(stage: str, patches: Iterable[dict]) -> int:
    """Attribute patches for existing nodes: {id, attrs?, plain_summary?, subtype?}."""
    return write_jsonl(f"{stage}.node_patches.jsonl", patches)
