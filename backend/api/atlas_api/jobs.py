"""Async jobs (gap-search). API enqueues to a Redis list; atlas_api.worker consumes.
Without Redis (dev) the job runs in-process as a background task."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from typing import Any
from urllib.parse import quote_plus

import httpx

from .cache import Store
from .config import Settings

log = logging.getLogger("atlas_api.jobs")
QUEUE = "atlas:jobs"
# A job is popped before it runs (no lease): if the worker dies mid-job, "running"
# expires after this and polling gets a 404 instead of spinning for job_ttl_seconds.
RUNNING_TTL_SECONDS = 120
DISCLAIMER = ("Unverified web search results. They have not been reviewed by the Atlas team; "
              "check each source before acting on it.")


def job_key(job_id: str) -> str:
    return f"job:{job_id}"


def new_job_id() -> str:
    return "job_" + uuid.uuid4().hex[:20]


def status(job_id: str, state: str, result: dict | None = None, error: str | None = None) -> dict:
    return {"job_id": job_id, "state": state, "result": result, "error": error}


async def save(store: Store, settings: Settings, st: dict, ttl: int | None = None) -> None:
    await store.set(job_key(st["job_id"]), json.dumps(st).encode(), ttl or settings.job_ttl_seconds)


async def load(store: Store, job_id: str) -> dict | None:
    raw = await store.get(job_key(job_id))
    return json.loads(raw) if raw else None


# ---- Bright Data SERP ---------------------------------------------------------

BRIGHTDATA_REQUEST_URL = "https://api.brightdata.com/request"
# Per request: connect 10 s, then 35 s for the rest. The whole search (3 queries in
# parallel) is capped by GAP_SEARCH_DEADLINE_SECONDS so a job always ends inside the
# worker's 60 s stop_grace_period (infra/docker-compose.yml) and well before
# RUNNING_TTL_SECONDS: a SIGTERMed worker finishes its job instead of leaving it "running".
SERP_TIMEOUT = httpx.Timeout(35, connect=10)
GAP_SEARCH_DEADLINE_SECONDS = 50.0
_BRD_ERROR_HEADERS = ("x-brd-error", "x-brd-err-code", "x-brd-err-msg")


class UpstreamError(RuntimeError):
    """Web search failed upstream. str(exc) is safe to show in JobStatus.error."""


async def brightdata_serp(settings: Settings, query: str, client: httpx.AsyncClient) -> list[dict[str, Any]]:
    """One Google search through Bright Data -> [{title, url, snippet}].

    Verified 2026-10-03 against the live API: POST https://api.brightdata.com/request with
    `Authorization: Bearer <BRIGHTDATA_API_KEY>` and body {zone, url, format: "raw"}, where
    url is a Google search URL with `brd_json=1`. The answer is Google's results parsed to
    JSON; `organic` is a list of {title, link, description, ...} and may be absent or empty.
    The zone is BRIGHTDATA_SERP_ZONE: a SERP API zone works, and so does a Web Unlocker zone
    (e.g. "mcp_unlocker"). Do not send Google's `num` param: Bright Data rejects it
    (x-brd-serp-warn). Bright Data answers HTTP 200 even when it fails: the error is in the
    x-brd-error / x-brd-err-code / x-brd-err-msg headers and the body is empty (seen live:
    "redirect location was rejected"). An unknown zone is an HTTP 400 with a plain-text
    reason. Every failure raises UpstreamError with a message that never contains the API key.
    """
    google = f"https://www.google.com/search?q={quote_plus(query)}&hl=en&brd_json=1"
    try:
        resp = await client.post(
            BRIGHTDATA_REQUEST_URL,
            headers={"Authorization": f"Bearer {settings.brightdata_api_key}"},
            json={"zone": settings.brightdata_serp_zone, "url": google, "format": "raw"},
            timeout=SERP_TIMEOUT,
        )
    except httpx.TimeoutException as exc:
        raise UpstreamError("web search timed out (Bright Data)") from exc
    except httpx.HTTPError as exc:
        raise UpstreamError(f"web search request failed (Bright Data): {type(exc).__name__}") from exc
    brd = {h: resp.headers[h].strip() for h in _BRD_ERROR_HEADERS if resp.headers.get(h, "").strip()}
    if brd:
        code = brd.get("x-brd-err-code", "")
        msg = brd.get("x-brd-err-msg") or brd.get("x-brd-error", "")
        detail = f"{code}: {msg}" if code and msg and msg != code else (code or msg)
        raise UpstreamError(f"web search failed (Bright Data {detail[:200]})")
    if resp.status_code >= 400:  # e.g. 400 with a short plain-text reason for an unknown zone
        reason = " ".join(resp.text.split())[:150]
        if settings.brightdata_api_key:
            reason = reason.replace(settings.brightdata_api_key, "***")
        raise UpstreamError(f"web search failed (Bright Data HTTP {resp.status_code}"
                            + (f": {reason})" if reason else ")"))
    if not resp.content.strip():
        raise UpstreamError("web search failed (Bright Data returned an empty response)")
    try:
        data = resp.json()
    except ValueError as exc:
        raise UpstreamError("web search failed (Bright Data returned non-JSON; check the zone and brd_json=1)") from exc
    organic = data.get("organic") if isinstance(data, dict) else None
    if not isinstance(organic, list):
        return []
    return [{"title": str(o.get("title") or ""), "url": str(o.get("link") or o.get("url") or ""),
             "snippet": str(o.get("description") or o.get("snippet") or "")}
            for o in organic if isinstance(o, dict) and (o.get("link") or o.get("url"))]


_KINDS = [
    ("registry", r"\bregistr(y|ies)\b|natural history"),
    ("patient_group", r"foundation|association|alliance|society|support group|patient (group|organi[sz]ation)|charity|network"),
    ("study", r"clinicaltrials|pubmed|ncbi|\bstudy\b|\btrial\b|journal|doi\.org"),
    ("news", r"\bnews\b|press release|announces"),
]


def classify(lead: dict) -> str:
    text = f"{lead['title']} {lead['url']} {lead['snippet']}".lower()
    return next((kind for kind, rx in _KINDS if re.search(rx, text)), "other")


async def run_gap_search(settings: Settings, store: Store, job_id: str, disease_id: str, label: str) -> None:
    await save(store, settings, status(job_id, "running"), RUNNING_TTL_SECONDS)
    try:
        if not settings.brightdata_api_key:
            raise RuntimeError("web search is not configured (BRIGHTDATA_API_KEY missing)")
        queries = [f'"{label}" patient organization', f'"{label}" patient registry',
                   f'"{label}" natural history study']
        async with httpx.AsyncClient() as client:
            try:
                results = await asyncio.wait_for(
                    asyncio.gather(*(brightdata_serp(settings, q, client) for q in queries),
                                   return_exceptions=True),
                    GAP_SEARCH_DEADLINE_SECONDS)
            except TimeoutError as exc:
                raise UpstreamError(f"web search timed out after {GAP_SEARCH_DEADLINE_SECONDS:.0f}s") from exc
        pages = [r for r in results if not isinstance(r, BaseException)]
        if not pages:  # every query failed: report the first reason
            raise next(r for r in results if isinstance(r, BaseException))
        for r in results:
            if isinstance(r, BaseException):
                log.warning("gap-search %s: one query failed, keeping the others: %s", job_id, r)
        seen: set[str] = set()
        leads = []
        for hit in (h for page in pages for h in page):
            if hit["url"] in seen:
                continue
            seen.add(hit["url"])
            leads.append({**hit, "kind": classify(hit)})
        await save(store, settings, status(job_id, "done", {"leads": leads[:25], "disclaimer": DISCLAIMER}))
    except Exception as exc:
        log.warning("gap-search %s for %s failed: %s", job_id, disease_id, exc)
        msg = str(exc) if isinstance(exc, RuntimeError) and str(exc) else "web search failed"
        await save(store, settings, status(job_id, "failed", error=msg[:300]))
