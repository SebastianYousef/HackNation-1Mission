"""Patient organisations & registries discovered on the web via Bright Data (BRIGHTDATA_API_KEY).

All request code lives in `brightdata_request()`.  Results are UNVERIFIED web leads:
organisation nodes subtype patient_group, organization_serves_disease edges with status 'hypothesis'
(confidence <= 0.4), evidence source_type 'web', method 'scrape:brightdata', quote = result snippet.
A human (or the curated.yaml file) must confirm before they count as 'literature'.

Env: BRIGHTDATA_API_KEY (required), BRIGHTDATA_SERP_ZONE (default 'serp_api1').
"""
from __future__ import annotations

import logging
from urllib.parse import quote_plus, urlparse

from ..config import env
from ..http import post_json
from ..ids import org_id
from ..store import GraphWriter
from ._common import Coverage, query_diseases

log = logging.getLogger(__name__)
API = "https://api.brightdata.com/request"
ORG_HINTS = ("foundation", "association", "alliance", "society", "support", "families", "family", "registry",
             "network", "charity", "trust", "stiftung", "fund", "hope", "cure")
SKIP_DOMAINS = ("wikipedia.org", "ncbi.nlm.nih.gov", "pubmed", "medlineplus", "rarediseases.info.nih.gov",
                "orpha.net", "omim.org", "mayoclinic", "clevelandclinic", "malacards", "youtube.com",
                "facebook.com", "nature.com", "sciencedirect", "springer", "wiley")


def brightdata_request(query: str) -> list[dict]:
    """Google SERP via Bright Data SERP API; returns [{title, url, snippet}].

    TODO(verify): request format against the current Bright Data docs for your zone type.
    Assumed: POST https://api.brightdata.com/request, Bearer token,
             {"zone": <serp zone>, "url": "https://www.google.com/search?q=...&brd_json=1", "format": "raw"}
             -> JSON with an "organic" list of {title, link, description}.
    For a Web Unlocker zone use the same endpoint with the target URL instead of a Google URL.
    """
    key = env("BRIGHTDATA_API_KEY")
    if not key:
        raise RuntimeError("BRIGHTDATA_API_KEY not set")
    body = {"zone": env("BRIGHTDATA_SERP_ZONE", "serp_api1"),
            "url": f"https://www.google.com/search?q={quote_plus(query)}&brd_json=1&num=20", "format": "raw"}
    data = post_json(API, body, ns="brightdata", headers={"Authorization": f"Bearer {key}"})
    if isinstance(data, dict) and "body" in data and isinstance(data["body"], dict):
        data = data["body"]
    return [{"title": r.get("title", ""), "url": r.get("link") or r.get("url", ""),
             "snippet": r.get("description") or r.get("snippet", "")} for r in (data.get("organic") or [])]


def looks_like_org(r: dict) -> bool:
    host = urlparse(r["url"]).hostname or ""
    if any(s in host for s in SKIP_DOMAINS):
        return False
    text = (r["title"] + " " + r["snippet"]).lower()
    return any(h in text for h in ORG_HINTS)


def download() -> None:
    return None


def emit() -> None:
    g = GraphWriter("brightdata_orgs")
    if not env("BRIGHTDATA_API_KEY"):
        log.info("brightdata_orgs skipped (BRIGHTDATA_API_KEY not set)")
        g.close()
        return
    cov = Coverage("brightdata")
    for d in query_diseases():
        q = f'"{d["label"]}" patient organization OR foundation OR registry'
        try:
            results = brightdata_request(q)
        except Exception as e:
            log.warning("brightdata failed for %s: %s", d["id"], e)
            cov.add(d["id"], q, None)
            continue
        leads = [r for r in results if looks_like_org(r)]
        cov.add(d["id"], q, len(leads))
        for r in leads[:5]:
            host = (urlparse(r["url"]).hostname or "").removeprefix("www.")
            oid = org_id(host)
            g.node(id=oid, type="organization", subtype="patient_group", label=r["title"][:120] or host, url=r["url"],
                   attrs={"website": f"https://{host}", "unverified": True, "source": "brightdata"})
            g.edge("organization_serves_disease", oid, d["id"], status="hypothesis", label="may support families with",
                   evidence=dict(source_type="web", source_name="Web search (Bright Data)", source_ref=r["url"],
                                 url=r["url"], quote=r["snippet"][:500] or None, method="scrape:brightdata"))
    cov.save()
    g.close()
