"""Data access: every GET endpoint = one api_* SQL function (DATA_MODE=db) or one
fixture file under contract/fixtures (DATA_MODE=fixtures). Methods return the
response body as JSON *text* (served without re-serialising) or None (-> 404).
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from psycopg_pool import AsyncConnectionPool

from .config import Settings

log = logging.getLogger("atlas_api")


def safe_id(node_id: str) -> str:
    """Fixture naming rule: ':' -> '_' (and never allow path traversal)."""
    return node_id.replace(":", "_").replace("/", "_").replace("\\", "_").replace("..", "_")


class Db:
    def __init__(self, url: str, pool_size: int):
        # prepare_threshold=None: Supabase's transaction pooler (PgBouncer) cannot
        # handle server-side prepared statements.
        self.pool = AsyncConnectionPool(
            url, min_size=1, max_size=pool_size, open=False, timeout=5,
            kwargs={"prepare_threshold": None, "autocommit": True},
        )

    async def open(self) -> None:
        await self.pool.open(wait=False)

    async def close(self) -> None:
        await self.pool.close()

    async def scalar(self, sql: str, params: tuple[Any, ...] = ()) -> Any:
        async with self.pool.connection() as conn:
            cur = await conn.execute(sql, params)
            row = await cur.fetchone()
            return row[0] if row else None

    async def ping(self) -> bool:
        """Fast readiness probe (the LB health check times out after ~2 s)."""
        try:
            async with self.pool.connection(timeout=1.5) as conn:
                cur = await conn.execute("select 1")
                return (await cur.fetchone()) == (1,)
        except Exception:
            return False


class Fixtures:
    def __init__(self, root: Path):
        self.root = root
        self._cache: dict[Path, tuple[float, Any]] = {}

    def load(self, rel: str) -> Any | None:
        path = self.root / rel
        try:
            mtime = path.stat().st_mtime
        except (OSError, ValueError):  # missing, name too long, NUL byte -> 404
            return None
        hit = self._cache.get(path)
        if hit and hit[0] == mtime:
            return hit[1]
        data = json.loads(path.read_text(encoding="utf-8"))
        self._cache[path] = (mtime, data)
        return data

    def version(self) -> str:
        meta = self.load("meta.json") or {}
        v = str((meta.get("dataset") or {}).get("version") or "")
        stamp = max((p.stat().st_mtime for p in self.root.rglob("*.json")), default=0.0)
        return f"fx-{v}-{hashlib.sha1(str(stamp).encode()).hexdigest()[:8]}"


# Dataset version + a token of the SQL the API runs: md5 over every function in the API's
# schema (api_* and their _helpers, i.e. what the migrations define; extension functions
# excluded). A migration that changes a function body changes the ETag and the response
# cache key at once, without waiting for the next data load. ~2 ms; run at most every 15 s.
VERSION_SQL = """
select coalesce((select coalesce(value->>'version', md5(value::text)) from dataset_meta where key = 'dataset'), '0')
  || '-s' || left(md5(coalesce((
       select string_agg(p.oid::regprocedure::text || '=' || md5(p.prosrc), ',' order by p.oid::regprocedure::text)
         from pg_proc p
        where p.pronamespace = current_schema()::regnamespace
          and not exists (select 1 from pg_depend d where d.classid = 'pg_proc'::regclass and d.objid = p.oid
                                                      and d.deptype = 'e')), '')), 8)"""


def _dump(obj: Any) -> str | None:
    return None if obj is None else json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


class Data:
    def __init__(self, settings: Settings, db: Db | None):
        self.s, self.db = settings, db
        self.fx = Fixtures(settings.fixtures_dir)
        self._version: tuple[float, str] = (0.0, "")

    def _use_db(self, endpoint: str) -> bool:
        return self.s.uses_db(endpoint) and self.db is not None

    async def _sql(self, sql: str, *params: Any) -> str | None:
        assert self.db is not None
        return await self.db.scalar(sql, params)

    async def dataset_version(self) -> str:
        """Cache-busting token (dataset version + SQL schema token); refreshed at most every 15 s."""
        ts, v = self._version
        if time.monotonic() - ts < 15 and v:
            return v
        dbv = None
        if self.s.needs_db and self.db is not None:  # also mixed mode (DB_ENDPOINTS): a load must bust caches
            try:
                dbv = "db-" + str(await self.db.scalar(VERSION_SQL) or "0")
            except Exception as exc:
                if self.s.data_mode == "db":
                    raise
                log.warning("dataset version from DB unavailable: %s", exc)  # mixed mode: keep fixtures up
        if self.s.data_mode == "db":
            v = dbv or "db-0"
        else:
            v = self.fx.version() + (f"+{dbv}" if dbv else "")
        self._version = (time.monotonic(), v)
        return v

    def _fx_one(self, folder: str, ident: str, payload_id: Callable[[Any], Any]) -> Any | None:
        """Fixture for `ident`, or None unless the payload's own id is exactly `ident`
        (safe_id is many-to-one: MONDO_1 and MONDO/1 would otherwise alias MONDO:1)."""
        data = self.fx.load(f"{folder}/{safe_id(ident)}.json")
        try:
            return data if data is not None and payload_id(data) == ident else None
        except (KeyError, TypeError, IndexError):
            return None

    def _fx_list(self, folder: str, ident: str) -> list | None:
        """List fixtures (similar, paths) carry no source id: check it against the node fixture."""
        data = self.fx.load(f"{folder}/{safe_id(ident)}.json")
        if data is None:
            return None
        node = self.fx.load(f"nodes/{safe_id(ident)}.json")
        real = (node or {}).get("node", {}).get("id")
        if (real is not None and real != ident) or (real is None and ":" not in ident):
            return None
        return data

    # ---- GET endpoints ----------------------------------------------------
    async def meta(self) -> str | None:
        if self._use_db("meta"):
            return await self._sql("select api_meta()::text")
        return _dump(self.fx.load("meta.json"))

    async def search(self, q: str, types: list[str] | None, limit: int) -> str | None:
        if self._use_db("search"):
            return await self._sql("select api_search(%s::text, %s::text[], %s::int)::text", q, types, limit)
        table: dict[str, list[dict]] = self.fx.load("search.json") or {}
        key = q.strip().lower()
        hits = next((v for k, v in table.items() if k.strip().lower() == key), None)
        if hits is None:
            seen: set[str] = set()
            hits = []
            for h in (h for v in table.values() for h in v):
                hay = " ".join(str(h.get(f) or "") for f in ("id", "label", "matched_name")).lower()
                if key in hay and h["id"] not in seen:
                    seen.add(h["id"])
                    hits.append(h)
            hits.sort(key=lambda h: -float(h.get("score") or 0))
        if types:
            hits = [h for h in hits if h.get("type") in types]
        return _dump(hits[:limit])

    async def node(self, node_id: str) -> str | None:
        if self._use_db("node"):
            return await self._sql("select api_node(%s::text)::text", node_id)
        return _dump(self._fx_one("nodes", node_id, lambda d: d["node"]["id"]))

    async def neighborhood(self, node_id: str, depth: int, edge_types: list[str] | None,
                           statuses: list[str] | None, min_confidence: float, max_nodes: int) -> str | None:
        if self._use_db("neighborhood"):
            return await self._sql(
                "select api_neighborhood(%s::text, %s::int, %s::text[], %s::text[], %s::real, %s::int)::text",
                node_id, depth, edge_types, statuses, min_confidence, max_nodes)
        nb = self._fx_one("neighborhood", node_id, lambda d: d["center"])
        if nb is None:
            return None
        if edge_types or statuses or min_confidence > 0:  # approximate the filters on the static fixture
            edges = [e for e in nb["edges"]
                     if (not edge_types or e["type"] in edge_types)
                     and (not statuses or e["status"] in statuses)
                     and e["confidence"] >= min_confidence]
            keep = {nb["center"]} | {e["src"] for e in edges} | {e["dst"] for e in edges}
            nb = {**nb, "nodes": [n for n in nb["nodes"] if n["id"] in keep], "edges": edges}
        others = [n for n in nb["nodes"] if n["id"] != nb["center"]]
        if len(others) > max_nodes - 1:  # honour max_nodes: keep the best-connected neighbours (depth ignored)
            best: dict[str, float] = {}
            for e in nb["edges"]:
                for x in (e["src"], e["dst"]):
                    best[x] = max(best.get(x, 0.0), e["confidence"])
            others.sort(key=lambda n: -best.get(n["id"], 0.0))
            keep = {nb["center"]} | {n["id"] for n in others[:max_nodes - 1]}
            nb = {**nb, "nodes": [n for n in nb["nodes"] if n["id"] in keep],
                  "edges": [e for e in nb["edges"] if e["src"] in keep and e["dst"] in keep], "truncated": True}
        return _dump(nb)

    async def edge(self, edge_id: str) -> str | None:
        if self._use_db("edge"):
            return await self._sql("select api_edge(%s::text)::text", edge_id)
        return _dump(self._fx_one("edges", edge_id, lambda d: d["edge"]["id"]))

    async def similar(self, disease_id: str, limit: int) -> str | None:
        if self._use_db("similar"):
            return await self._sql("select api_similar_diseases(%s::text, %s::int)::text", disease_id, limit)
        sim = self._fx_list("similar", disease_id)
        return None if sim is None else _dump(sim[:limit])

    async def paths(self, from_id: str, to_id: str | None, kind: str | None, limit: int) -> str | None:
        if self._use_db("paths"):
            return await self._sql("select api_paths(%s::text, %s::text, %s::text, %s::int)::text",
                                   from_id, to_id, kind, limit)
        ps = self._fx_list("paths", from_id)
        if ps is None:
            return None
        ps = [p for p in ps if (not to_id or p["to"] == to_id) and (not kind or p["kind"] == kind)]
        return _dump(ps[:limit])

    async def clusters(self) -> str | None:
        if self._use_db("clusters"):
            return await self._sql("select api_clusters()::text")
        return _dump(self.fx.load("clusters.json"))

    async def cluster(self, cluster_id: str) -> str | None:
        if self._use_db("cluster"):
            return await self._sql("select api_cluster(%s::text)::text", cluster_id)
        return _dump(self._fx_one("clusters", cluster_id, lambda d: d["cluster"]["id"]))

    async def action_view(self, disease_id: str) -> str | None:
        if self._use_db("action_view"):
            return await self._sql("select api_action_view(%s::text)::text", disease_id)
        return _dump(self._fx_one("action-view", disease_id, lambda d: d["disease"]["id"]))

    async def mechanism_view(self, mech_id: str) -> str | None:
        if self._use_db("mechanism_view"):
            return await self._sql("select api_mechanism_view(%s::text)::text", mech_id)
        return _dump(self._fx_one("mechanism-view", mech_id, lambda d: d["mechanism"]["id"]))

    # ---- helpers for AI / jobs ---------------------------------------------
    async def edge_obj(self, edge_id: str) -> dict | None:
        raw = await self.edge(edge_id)
        return json.loads(raw) if raw else None

    async def node_obj(self, node_id: str) -> dict | None:
        raw = await self.node(node_id)
        return json.loads(raw) if raw else None
