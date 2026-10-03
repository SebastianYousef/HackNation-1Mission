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
DISCLAIMER = ("Unverified web search results. They have not been reviewed by the Atlas team; "
              "check each source before acting on it.")


def job_key(job_id: str) -> str:
    return f"job:{job_id}"


def new_job_id() -> str:
    return "job_" + uuid.uuid4().hex[:20]


def status(job_id: str, state: str, result: dict | None = None, error: str | None = None) -> dict:
    return {"job_id": job_id, "state": state, "result": result, "error": error}


async def save(store: Store, settings: Settings, st: dict) -> None:
    await store.set(job_key(st["job_id"]), json.dumps(st).encode(), settings.job_ttl_seconds)


async def load(store: Store, job_id: str) -> dict | None:
    raw = await store.get(job_key(job_id))
    return json.loads(raw) if raw else None


# ---- Bright Data SERP ---------------------------------------------------------

async def brightdata_serp(settings: Settings, query: str, client: httpx.AsyncClient) -> list[dict[str, Any]]:
    """One Google search through the Bright Data SERP API -> [{title, url, snippet}].

    TODO(verify): request format follows Bright Data's "Direct API access" docs as we
    understood them: POST https://api.brightdata.com/request with Bearer API key,
    body {zone, url, format:"raw"}, and `brd_json=1` on the Google URL to get parsed JSON
    with an `organic` array of {title, link, description}. Check the zone name and the
    response shape against the dashboard/playground before the demo.
    """
    google = f"https://www.google.com/search?q={quote_plus(query)}&hl=en&num=10&brd_json=1"
    resp = await client.post(
        "https://api.brightdata.com/request",
        headers={"Authorization": f"Bearer {settings.brightdata_api_key}"},
        json={"zone": settings.brightdata_serp_zone, "url": google, "format": "raw"},
        timeout=45,
    )
    resp.raise_for_status()
    data = resp.json()
    return [{"title": o.get("title") or "", "url": o.get("link") or o.get("url") or "",
             "snippet": o.get("description") or o.get("snippet") or ""}
            for o in data.get("organic", []) if o.get("link") or o.get("url")]


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
    await save(store, settings, status(job_id, "running"))
    try:
        if not settings.brightdata_api_key:
            raise RuntimeError("web search is not configured (BRIGHTDATA_API_KEY missing)")
        queries = [f'"{label}" patient organization', f'"{label}" patient registry',
                   f'"{label}" natural history study']
        async with httpx.AsyncClient() as client:
            pages = await asyncio.gather(*(brightdata_serp(settings, q, client) for q in queries))
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
        await save(store, settings, status(job_id, "failed", error=str(exc)[:300]))
