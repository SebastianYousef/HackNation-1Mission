"""Patient organisations & registries discovered on the web via Bright Data.

Two transports, the first configured one wins:
  1. Scraping Browser (BRIGHTDATA_BROWSER_WSS): a remote Chrome driven over CDP with Playwright.
     Searches Google through Bright Data's unblocker, then VISITS each candidate site and reads it:
     page title/description, whether it mentions a patient registry / natural history study, and a
     contact link. Registries become `asset` nodes (organization_maintains_asset, asset_covers_disease).
  2. SERP API (BRIGHTDATA_API_KEY + BRIGHTDATA_SERP_ZONE): search results only.

Results are UNVERIFIED web leads: edges get status 'hypothesis' (confidence <= 0.4), evidence
source_type 'web' / 'patient_org_site', method 'scrape:brightdata', quote = text found on the page.
A human (or config/curated.yaml) must confirm before they count as 'literature'.

The zone's IP allowlist must contain the public IP of the machine running this (not a 100.x
Tailscale address). Install the extra: pip install -e '.[scrape]'.
"""
from __future__ import annotations

import asyncio
import logging
import re
from urllib.parse import quote_plus, urljoin, urlparse

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


REGISTRY_RX = re.compile(r"[^.!?\n]{0,160}\b(patient registry|natural history (study|registry)|registry)\b[^.!?\n]{0,160}",
                         re.I)
CONTACT_RX = re.compile(r"contact|get in touch|kontakt", re.I)
BROWSER_CONCURRENCY = 3       # parallel remote browser sessions
PAGE_TIMEOUT_MS = 60_000


async def _google(browser, query: str) -> list[dict]:
    page = await browser.new_page()
    try:
        await page.goto(f"https://www.google.com/search?q={quote_plus(query)}&num=20&hl=en",
                        timeout=PAGE_TIMEOUT_MS, wait_until="domcontentloaded")
        rows = await page.eval_on_selector_all(
            "a:has(h3)",
            """els => els.map(a => {
                 const box = a.closest('div.g, div[data-hveid]');
                 const snip = box ? box.innerText.split('\\n').slice(1).join(' ') : '';
                 return {title: a.querySelector('h3').innerText, url: a.href, snippet: snip.slice(0, 400)};
               })""")
        return [r for r in rows if r["url"].startswith("http") and "google." not in (urlparse(r["url"]).hostname or "")]
    finally:
        await page.close()


async def _visit(browser, url: str) -> dict:
    """Read an organisation's site: description, registry mention (quote), contact link."""
    page = await browser.new_page()
    try:
        await page.goto(url, timeout=PAGE_TIMEOUT_MS, wait_until="domcontentloaded")
        title = (await page.title() or "").strip()
        desc = await page.evaluate("() => document.querySelector('meta[name=description]')?.content || ''")
        text = await page.evaluate("() => document.body ? document.body.innerText : ''")
        links = await page.eval_on_selector_all("a[href]", "els => els.map(a => [a.innerText.trim(), a.href])")
        reg = REGISTRY_RX.search(text or "")
        contact = next((h for t, h in links if CONTACT_RX.search(t or "") or h.startswith("mailto:")), None)
        return {"title": title, "description": desc.strip()[:400],
                "registry_quote": " ".join(reg.group(0).split()) if reg else None,
                "contact_url": urljoin(url, contact) if contact else None}
    except Exception as e:  # one bad site must not stop the run
        log.debug("visit failed %s: %s", url, e)
        return {}
    finally:
        await page.close()


async def _browser_run(wss: str, diseases: list[dict], per_disease: int) -> dict[str, tuple[str, list[dict]]]:
    from playwright.async_api import async_playwright
    sem = asyncio.Semaphore(BROWSER_CONCURRENCY)
    out: dict[str, tuple[str, list[dict]]] = {}

    async def one(pw, d: dict) -> None:
        q = f'"{d["label"]}" patient organization OR foundation OR registry'
        async with sem:
            try:
                browser = await pw.chromium.connect_over_cdp(wss, timeout=PAGE_TIMEOUT_MS)
            except Exception as e:
                log.warning("brightdata browser connect failed for %s: %s", d["id"], e)
                return
            try:
                leads = [r for r in await _google(browser, q) if looks_like_org(r)]
                seen, uniq = set(), []
                for r in leads:
                    host = (urlparse(r["url"]).hostname or "").removeprefix("www.")
                    if host not in seen:
                        seen.add(host)
                        uniq.append(r)
                for r in uniq[:per_disease]:
                    r["site"] = await _visit(browser, f"https://{(urlparse(r['url']).hostname)}/")
                out[d["id"]] = (q, uniq[:per_disease])
                log.info("brightdata %s: %d org leads", d["label"], len(uniq[:per_disease]))
            except Exception as e:
                log.warning("brightdata browser failed for %s: %s", d["id"], e)
                out[d["id"]] = (q, [])
            finally:
                await browser.close()

    async with async_playwright() as pw:
        await asyncio.gather(*(one(pw, d) for d in diseases))
    return out


def looks_like_org(r: dict) -> bool:
    host = urlparse(r["url"]).hostname or ""
    if any(s in host for s in SKIP_DOMAINS):
        return False
    text = (r["title"] + " " + r["snippet"]).lower()
    return any(h in text for h in ORG_HINTS)


def download() -> None:
    return None


def _emit_lead(g: GraphWriter, d: dict, r: dict) -> None:
    host = (urlparse(r["url"]).hostname or "").removeprefix("www.")
    site = r.get("site") or {}
    oid = org_id(host)
    g.node(id=oid, type="organization", subtype="patient_group",
           label=(site.get("title") or r["title"])[:120] or host, url=f"https://{host}",
           description=site.get("description") or None,
           attrs={"website": f"https://{host}", "unverified": True, "source": "brightdata",
                  "contact_url": site.get("contact_url"), "has_registry": True if site.get("registry_quote") else None})
    g.edge("organization_serves_disease", oid, d["id"], status="hypothesis", label="may support families with",
           evidence=dict(source_type="web", source_name="Web search (Bright Data)", source_ref=r["url"],
                         url=r["url"], quote=(r.get("snippet") or "")[:500] or None, method="scrape:brightdata"))
    if site.get("registry_quote"):
        aid = f"ASSET:{host.replace('.', '-')}-registry"
        g.node(id=aid, type="asset", subtype="registry", label=f"Registry run by {host}", url=f"https://{host}",
               attrs={"asset_kind": "registry", "owner_id": oid, "unverified": True, "access": "on_request"})
        ev = dict(source_type="patient_org_site", source_name=host, source_ref=f"https://{host}/",
                  url=f"https://{host}/", quote=site["registry_quote"][:500], method="scrape:brightdata")
        g.edge("organization_maintains_asset", oid, aid, status="hypothesis", label="maintains", evidence=ev)
        g.edge("asset_covers_disease", aid, d["id"], status="hypothesis", label="may cover", evidence=ev)


def emit() -> None:
    g = GraphWriter("brightdata_orgs")
    wss, key = env("BRIGHTDATA_BROWSER_WSS"), env("BRIGHTDATA_API_KEY")
    if not (wss or key):
        log.info("brightdata_orgs skipped (neither BRIGHTDATA_BROWSER_WSS nor BRIGHTDATA_API_KEY set)")
        g.close()
        return
    cov = Coverage("brightdata")
    diseases = query_diseases()
    per_disease = 5
    if wss:
        results = asyncio.run(_browser_run(wss, diseases, per_disease))
        for d in diseases:
            q, leads = results.get(d["id"], (None, None))
            cov.add(d["id"], q or "", None if leads is None else len(leads))
            for r in leads or []:
                _emit_lead(g, d, r)
    else:
        for d in diseases:
            q = f'"{d["label"]}" patient organization OR foundation OR registry'
            try:
                leads = [r for r in brightdata_request(q) if looks_like_org(r)]
            except Exception as e:
                log.warning("brightdata failed for %s: %s", d["id"], e)
                cov.add(d["id"], q, None)
                continue
            cov.add(d["id"], q, len(leads))
            for r in leads[:per_disease]:
                _emit_lead(g, d, r)
    cov.save()
    g.close()
