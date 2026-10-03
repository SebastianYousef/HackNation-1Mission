"""Helpers shared by the API-backed ingesters."""
from __future__ import annotations

import re

from ..config import slice_config
from ..models import now_iso
from ..store import read_json, read_jsonl, write_json

GENERIC = {"disease", "syndrome", "disorder", "ncl", "lsd"}


def query_diseases() -> list[dict]:
    """Disease nodes to query APIs for (literature.scope: focus | slice), focus first."""
    sl = read_json("slice")
    scope = slice_config().literature.get("scope", "focus")
    ids = sl["focus"] if scope == "focus" else sl["diseases"]
    nodes = {n["id"]: n for n in read_jsonl("mondo.nodes.jsonl")}
    return [nodes[i] for i in ids if i in nodes]


def search_names(node: dict, k: int = 4) -> list[str]:
    """Label + up to k-1 distinctive synonyms/abbreviations usable as free-text queries."""
    names = [node["label"]]
    cands = list(node.get("synonyms") or []) + list((node.get("attrs") or {}).get("abbreviations") or [])
    for s in sorted(cands, key=len):
        s2 = s.strip()
        if len(s2) < 4 or s2.lower() in GENERIC or s2.lower() in (n.lower() for n in names):
            continue
        if re.search(r"[\[\]\"]", s2):
            continue
        names.append(s2)
        if len(names) >= k:
            break
    return names


class Coverage:
    """Per-source, per-disease record of what we searched (feeds ActionView.coverage)."""

    def __init__(self, source: str):
        self.source = source
        self.rows: dict[str, dict] = {}

    def add(self, disease_id: str, query: str, count: int | None) -> None:
        self.rows[disease_id] = {"query": query, "result_count": count, "checked_at": now_iso()}

    def save(self) -> None:
        write_json(f"coverage_{self.source}", self.rows)
