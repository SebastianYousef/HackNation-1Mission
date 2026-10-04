"""/api/v1 endpoints (contract/atlas.ts). Ids contain ':' and may arrive URL-encoded;
`{x:path}` params also survive an encoded '/' (%2F)."""
from __future__ import annotations

import ipaddress
import json
import uuid
from collections.abc import Awaitable, Callable
from typing import Annotated, Literal
from urllib.parse import urlencode

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Request, Response
from pydantic import BaseModel, Field
from redis.exceptions import RedisError

from . import jobs
from .cache import StoreUnavailable
from .errors import ApiError, bad_request, not_found, unavailable

router = APIRouter(prefix="/api/v1")

NODE_TYPES = {"disease", "gene", "variant", "phenotype", "mechanism", "intervention", "organization",
              "person", "publication", "trial", "grant", "asset"}
EDGE_TYPES = {
    "gene_associated_with_disease", "variant_of_gene", "variant_associated_with_disease", "gene_in_mechanism",
    "disease_involves_mechanism", "disease_has_phenotype", "disease_subtype_of", "disease_similar_to",
    "intervention_targets_mechanism", "intervention_treats_disease", "trial_studies_disease",
    "trial_tests_intervention", "publication_about", "person_authored", "person_studies",
    "person_affiliated_with", "grant_funds_person", "grant_studies", "organization_funds_grant",
    "organization_serves_disease", "organization_maintains_asset", "asset_covers_disease", "asset_targets_gene"}
EDGE_STATUSES = {"curated", "literature", "inferred", "hypothesis"}
PathKind = Literal["related_disease", "patient_group", "asset", "researcher", "trial", "intervention"]

JSON = "application/json"
MAX_ID = 200
Id = Annotated[str, Field(min_length=1, max_length=MAX_ID)]


def _csv(value: str | None, allowed: set[str], name: str) -> list[str] | None:
    if not value:
        return None
    items = [v.strip() for v in value.split(",") if v.strip()]
    bad = [v for v in items if v not in allowed]
    if bad:
        raise bad_request(f"unknown {name}: {', '.join(bad)}")
    return items or None


def _clamp(v: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, v))


async def _cached_get(request: Request, what: str, load: Callable[[], Awaitable[str | None]]) -> Response:
    """Shared GET path: ETag/304, shared response cache (Redis), 404 on NULL."""
    st = request.app.state
    if any("\x00" in v or len(v) > MAX_ID for v in request.path_params.values()):
        raise not_found(what)  # cannot be a real id; never reaches the filesystem or Postgres
    version = await st.data.dataset_version()
    etag = f'W/"{version}"'
    headers = {"ETag": etag, "Cache-Control": "public, max-age=60", "Vary": "Origin"}
    if etag in request.headers.get("if-none-match", ""):
        return Response(status_code=304, headers=headers)
    # Parsed params (last value of a repeated param wins, as FastAPI binds it), sorted.
    key = f"resp:{version}:{request.url.path}?{urlencode(sorted(request.query_params.items()))}"
    body = await st.store.get(key)
    headers["X-Cache"] = "HIT" if body is not None else "MISS"
    if body is None:
        text = await load()
        if text is None:
            raise not_found(what)
        body = text.encode()
        await st.store.set(key, body, st.settings.cache_ttl_seconds)
    return Response(content=body, media_type=JSON, headers=headers)


def client_ip(request: Request) -> str:
    """Client address for rate limits and logs. Behind TRUSTED_PROXY_HOPS proxies the
    client is the entry that many hops from the RIGHT of X-Forwarded-For (each proxy
    appends what it saw); the leftmost entries are whatever the client sent."""
    peer = request.client.host if request.client else "unknown"
    hops = request.app.state.settings.trusted_proxy_hops
    xff = ",".join(request.headers.getlist("x-forwarded-for"))
    entries = [e.strip() for e in xff.split(",") if e.strip()]
    if hops <= 0 or not entries:
        return peer
    candidate = entries[-min(hops, len(entries))]
    try:
        return str(ipaddress.ip_address(candidate))
    except ValueError:
        return peer  # garbage never becomes a rate-limit key


def rate_limit(bucket: str):
    async def dep(request: Request) -> None:
        st = request.app.state
        ip = client_ip(request)
        limit = st.settings.ai_rate_limit_per_minute
        if await st.store.incr_window(f"rl:{bucket}:{ip}", 60) > limit:
            raise ApiError(429, "rate_limited", f"too many requests: max {limit}/min for {bucket}",
                           {"Retry-After": "60"})
    return Depends(dep)


# ---- GET --------------------------------------------------------------------------

@router.get("/meta")
async def meta(request: Request) -> Response:
    return await _cached_get(request, "meta", request.app.state.data.meta)


@router.get("/search")
async def search(request: Request, q: Annotated[str, Query(max_length=200)] = "",
                 types: str | None = None, limit: int = 20) -> Response:
    t = _csv(types, NODE_TYPES, "types")
    if len(q.strip()) < 2:  # contract: q min length 2 -> nothing to match yet
        return Response(content=b"[]", media_type=JSON, headers={"Cache-Control": "public, max-age=60"})
    return await _cached_get(request, "search", lambda: request.app.state.data.search(q, t, _clamp(limit, 1, 50)))


@router.get("/nodes/{node_id:path}/neighborhood")
async def neighborhood(request: Request, node_id: str,
                       depth: Annotated[int, Query(ge=1, le=3)] = 1,
                       edge_types: str | None = None, statuses: str | None = None,
                       min_confidence: Annotated[float, Query(ge=0, le=1)] = 0.0,
                       max_nodes: Annotated[int, Query(ge=2, le=300)] = 60) -> Response:
    et, ss = _csv(edge_types, EDGE_TYPES, "edge_types"), _csv(statuses, EDGE_STATUSES, "statuses")
    return await _cached_get(request, f"node {node_id}", lambda: request.app.state.data.neighborhood(
        node_id, depth, et, ss, min_confidence, max_nodes))


@router.get("/nodes/{node_id:path}")
async def node(request: Request, node_id: str) -> Response:
    return await _cached_get(request, f"node {node_id}", lambda: request.app.state.data.node(node_id))


@router.get("/edges/{edge_id:path}")
async def edge(request: Request, edge_id: str) -> Response:
    return await _cached_get(request, f"edge {edge_id}", lambda: request.app.state.data.edge(edge_id))


@router.get("/diseases/{disease_id:path}/similar")
async def similar(request: Request, disease_id: str, limit: int = 10) -> Response:
    return await _cached_get(request, f"disease {disease_id}",
                             lambda: request.app.state.data.similar(disease_id, _clamp(limit, 1, 50)))


@router.get("/diseases/{disease_id:path}/action-view")
async def action_view(request: Request, disease_id: str) -> Response:
    return await _cached_get(request, f"action view for {disease_id}",
                             lambda: request.app.state.data.action_view(disease_id))


@router.get("/mechanisms/{mech_id:path}/view")
async def mechanism_view(request: Request, mech_id: str) -> Response:
    return await _cached_get(request, f"mechanism view for {mech_id}",
                             lambda: request.app.state.data.mechanism_view(mech_id))


@router.get("/paths")
async def paths(request: Request, from_: Annotated[str, Query(alias="from", min_length=1, max_length=MAX_ID)],
                to: Annotated[str | None, Query(max_length=MAX_ID)] = None, kind: PathKind | None = None, limit: int = 10) -> Response:
    return await _cached_get(request, f"node {from_}", lambda: request.app.state.data.paths(
        from_, to or None, kind, _clamp(limit, 1, 50)))


@router.get("/clusters")
async def clusters(request: Request) -> Response:
    return await _cached_get(request, "clusters", request.app.state.data.clusters)


@router.get("/clusters/{cluster_id:path}")
async def cluster(request: Request, cluster_id: str) -> Response:
    return await _cached_get(request, f"cluster {cluster_id}", lambda: request.app.state.data.cluster(cluster_id))


# ---- AI ---------------------------------------------------------------------------

class ExplainRequest(BaseModel):
    edge_ids: list[Id] = Field(min_length=1, max_length=8)
    audience: Literal["family", "expert"]


class OutreachRequest(BaseModel):
    disease_id: Id
    target_id: Id
    edge_ids: list[Id] = Field(min_length=1, max_length=12)


NO_STORE = {"Cache-Control": "no-store"}


@router.post("/explain", dependencies=[rate_limit("ai")])
async def explain(request: Request, body: ExplainRequest, response: Response) -> dict:
    response.headers.update(NO_STORE)
    return await request.app.state.ai.explain(body.edge_ids, body.audience)


@router.post("/outreach-draft", dependencies=[rate_limit("ai")])
async def outreach_draft(request: Request, body: OutreachRequest, response: Response) -> dict:
    response.headers.update(NO_STORE)
    return await request.app.state.ai.outreach(body.disease_id, body.target_id, body.edge_ids)


# ---- jobs -------------------------------------------------------------------------

class GapSearchRequest(BaseModel):
    disease_id: Id


@router.post("/gap-search", status_code=202, dependencies=[rate_limit("jobs")])
async def gap_search(request: Request, body: GapSearchRequest, response: Response,
                     background: BackgroundTasks) -> dict:
    st = request.app.state
    job_id = jobs.new_job_id()
    response.headers.update({**NO_STORE, "Location": f"/api/v1/jobs/{job_id}"})
    if not st.settings.uses_db("gap_search"):
        return {"job_id": job_id}  # fixtures mode: GET /jobs/{id} serves gap-search-job.json
    n = await st.data.node_obj(body.disease_id)
    if n is None:
        raise not_found(f"node {body.disease_id}")
    try:
        await jobs.save(st.store, st.settings, jobs.status(job_id, "queued"))
    except StoreUnavailable as exc:  # no status = the client would poll a 404
        raise unavailable("job queue unavailable") from exc
    payload = {"kind": "gap_search", "job_id": job_id, "disease_id": body.disease_id, "label": n["node"]["label"]}
    if st.store.redis is not None:
        try:
            await st.store.redis.lpush(jobs.QUEUE, json.dumps(payload))
        except RedisError as exc:
            raise unavailable("job queue unavailable") from exc
    else:  # dev without Redis: run in this process after the response is sent
        background.add_task(jobs.run_gap_search, st.settings, st.store, job_id, body.disease_id, payload["label"])
    return {"job_id": job_id}


@router.get("/jobs/{job_id}")
async def job(request: Request, job_id: str, response: Response) -> dict:
    st = request.app.state
    response.headers.update(NO_STORE)
    try:
        found = await jobs.load(st.store, job_id)
    except StoreUnavailable as exc:
        if st.settings.uses_db("gap_search"):  # a Redis error is not "no such job"
            raise unavailable("job store unavailable") from exc
        found = None  # fixtures mode: the canned job below
    if found is None and not st.settings.uses_db("gap_search"):
        fx = st.data.fx.load("gap-search-job.json")
        found = {**fx, "job_id": job_id} if fx else None
    if found is None:
        raise not_found(f"job {job_id}")
    return found


# ---- submissions ------------------------------------------------------------------

class SubmissionRequest(BaseModel):
    node_id: str | None = Field(default=None, max_length=MAX_ID)
    kind: Literal["missing_group", "missing_asset", "correction", "new_evidence", "other"]
    url: str | None = Field(default=None, max_length=2000)
    note: str = Field(min_length=1, max_length=2000)
    contact: str | None = Field(default=None, max_length=300)


@router.post("/submissions", status_code=201, dependencies=[rate_limit("submissions")])
async def submit(request: Request, body: SubmissionRequest, response: Response) -> dict:
    st = request.app.state
    response.headers.update(NO_STORE)
    if not st.settings.uses_db("submissions") or st.data.db is None:
        return {"id": str(uuid.uuid4())}  # fixtures mode: accepted, not stored
    node_id = (body.node_id or "").strip() or None  # "" from an empty form select = no node
    if node_id is not None and await st.data.db.scalar("select 1 from nodes where id = %s", (node_id,)) is None:
        raise bad_request(f"unknown node_id {node_id}")
    new_id = await st.data.db.scalar(
        "insert into submissions(node_id, kind, url, note, contact) values (%s,%s,%s,%s,%s) returning id::text",
        (node_id, body.kind, body.url, body.note, body.contact))
    return {"id": new_id}
