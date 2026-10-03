"""Snapshot real API responses for the demo ids into data/snapshot/ using the contract/fixtures layout,
so the frontend can run the real demo fully offline (copy into src/mocks/fixtures, open with ?mock=1)."""
from __future__ import annotations

import json
import logging
from urllib.parse import quote

import httpx

from .config import SNAPSHOT
from .store import read_json

log = logging.getLogger(__name__)


def run(api_base: str | None, database_url: str | None = None) -> None:
    if not api_base:
        raise SystemExit("set ATLAS_API_BASE (e.g. http://localhost:8000) — snapshots are taken through the API")
    focus = (read_json("slice", {}) or {}).get("focus", [])
    safe = lambda i: i.replace(":", "_")  # noqa: E731
    with httpx.Client(base_url=api_base.rstrip("/") + "/api/v1", timeout=30) as c:
        def put(rel: str, path: str) -> dict | list | None:
            r = c.get(path)
            if r.status_code != 200:
                return None
            f = SNAPSHOT / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(json.dumps(r.json(), indent=1, ensure_ascii=False))
            return r.json()
        put("meta.json", "/meta")
        put("clusters.json", "/clusters")
        search = {q: c.get("/search", params={"q": q}).json() for q in ("batten", "cln", "lysosom", "ceroid")}
        (SNAPSHOT / "search.json").write_text(json.dumps(search, indent=1, ensure_ascii=False))
        edges: set[str] = set()
        for d in focus:
            q = quote(d, safe="")
            av = put(f"action-view/{safe(d)}.json", f"/diseases/{q}/action-view")
            put(f"nodes/{safe(d)}.json", f"/nodes/{q}")
            nb = put(f"neighborhood/{safe(d)}.json", f"/nodes/{q}/neighborhood")
            put(f"similar/{safe(d)}.json", f"/diseases/{q}/similar")
            ps = put(f"paths/{safe(d)}.json", f"/paths?from={q}")
            for p in ps or []:
                edges.update(p["edge_ids"])
            edges.update(e["id"] for e in (nb or {}).get("edges", [])[:50])
            for c_ in (av or {}).get("related_communities", []):
                edges.add(c_["similarity_edge_id"])
        for e in edges:
            put(f"edges/{safe(e)}.json", f"/edges/{quote(e, safe='')}")
    log.info("snapshot: %d focus diseases, %d edges -> %s", len(focus), len(edges), SNAPSHOT)
