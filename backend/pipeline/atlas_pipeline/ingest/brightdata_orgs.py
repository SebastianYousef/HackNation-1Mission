"""Patient organisations & registries discovered on the web via Bright Data.

Per focus disease (diseases with no curated patient group for exactly that disease first):
  1. Search Google: an organisation query ('"CLN5" disease foundation families support'), then, for diseases with
     no exact group, a registry query ('"CLN5" patient registry'). Identical query strings are sent once (cache). With BRIGHTDATA_API_KEY: POST https://api.brightdata.com/request (zone
     BRIGHTDATA_SERP_ZONE, a SERP API or Web Unlocker zone such as `mcp_unlocker`), cached under
     data/raw/http_cache/brightdata. Without a key, or if that search fails, Google is opened in the
     Scraping Browser instead.
  2. Keep org-looking results (ORG_HINTS, not SKIP_DOMAINS), one per host.
  3. VISIT each result page Google found and read the site name/description, the sentences that name the
     disease, a registry / natural history mention and a contact link. The page is fetched directly (one
     cached GET, data/raw/http_cache/brightdata_pages, redirects followed; the final url decides the host
     and the curated match). The Scraping Browser (remote Chrome over CDP, Playwright) only searches for
     diseases whose API search failed; it re-visits pages the direct fetch could not read only with
     ATLAS_BRIGHTDATA_BROWSER_VISITS=1, because Bright Data's Scraping Browser refuses sites it classifies
     as "Philanthropy & Non-Profit Organizations" (proxy_error, residential network policy), i.e. most
     patient-org sites, and every session is billed. Registries become `asset` nodes
     (organization_maintains_asset, asset_covers_disease).
  4. Keep a lead only if the disease is actually named (search_names, `mentions`) in the result title/snippet
     or on the visited page; a generic "Foundation" hit is dropped.

Honesty: results are UNVERIFIED web leads. Every edge is 'hypothesis' (confidence <= 0.4), method
'scrape:brightdata', and nodes carry attrs.unverified; graph.has_quoted_support never lets these rows
back a 'literature' edge. An evidence `quote` is only ever text read from the visited page (whitespace
collapsed), and only a sentence that is about the organisation itself (pick_quote: it names the org or
speaks as 'we/our', or it is on the homepage / about page) and names no other organisation ('Noah's
Hope was founded by ...' on a directory page is about another charity). Google's snippet is not page
text, so a lead without such a sentence gets quote null (the snippet is kept in edge attrs.search_snippet). A human (or
config/curated.yaml) must confirm before anything counts as 'literature'. A lead whose url falls under a
curated organisation's `website` (curated.yaml) reuses that ORG id, writes no node and no asset, and
only adds an edge when the page quote names the disease. A registry asset needs page text saying the
site runs or enrols into it ('join our patient registry').

Bright Data answers HTTP 200 even on failure, with the reason in the x-brd-error / x-brd-err-code /
x-brd-err-msg headers and an empty body; Google's `num` param is rejected (x-brd-serp-warn), so it is
not sent. Verified against the live API on 2026-10-03 (zone mcp_unlocker, brd_json=1 -> JSON with an
`organic` list of {title, link, description, rank}; an unknown zone gives HTTP 400). Every request is
handled on its own: a failed search or visit is logged and that disease gets coverage result_count null.
A search is sent at most twice (emit's loop; brightdata_request itself does not retry), so a failing
disease costs at most 2 billed requests.

The zone's IP allowlist must contain the public IP of the machine running this (not a 100.x Tailscale
address). Install the extra for the browser: pip install -e '.[scrape]'.
Limit a test run with ATLAS_BRIGHTDATA_DISEASES=<id,id> and ATLAS_BRIGHTDATA_PER_DISEASE=<n>.
ATLAS_BRIGHTDATA_BROWSER_VISITS=1 lets the Scraping Browser re-visit pages the direct fetch failed on.
ATLAS_BRIGHTDATA_UNLOCKER_PAGES=1 re-fetches pages the direct GET could not read through the Web Unlocker
(zone BRIGHTDATA_UNLOCKER_ZONE, else BRIGHTDATA_SERP_ZONE), at most ATLAS_BRIGHTDATA_UNLOCKER_MAX (default 80).
ATLAS_BRIGHTDATA_REGISTRY_MAX (default 80) caps the registry searches of one run.
ATLAS_BRIGHTDATA_CACHE_ONLY=1 rebuilds the leads from cached searches without sending anything; a configuration
error (IP not on the zone allowlist, unknown zone, bad key) switches a run to that mode after the first rejection.

Budget: every billed Bright Data call (search, unlocker page, browser session; curated --verify --unlocker too) is
counted BEFORE it is sent in DATA_DIR/brightdata_budget.json, which persists across runs. At
ATLAS_BRIGHTDATA_MAX_REQUESTS (default 400) Budget.take raises BudgetExhausted and the stage stops sending, keeps
what it found and records result_count null for diseases it could not search. Cache hits are free.

Query names (search_query): a family with `query_by_gene: true` in config/slice.yaml (mitochondrial, epileptic
encephalopathies) searches a numbered subtype ('developmental and epileptic encephalopathy, 11') by its single
causal gene ('"SCN2A" foundation families support'), what families and foundations write, and rolls a
'<X> caused by mutation in <gene>' subtype up to X (all MELAS subtypes share one search). Other families keep the
original rule (a digit-bearing acronym such as CLN5, else the label), so earlier cached searches are reused.
"""
from __future__ import annotations

import asyncio
import html
import json
import logging
import re
from html.parser import HTMLParser
from urllib.parse import quote_plus, urljoin, urlparse

import httpx

from ..config import DATA_DIR, curated_config, env, slice_config
from ..http import REFRESH, _cache_path, request
from ..ids import asset_id, org_id
from ..models import now_iso
from ..store import GraphWriter, read_json, read_jsonl
from ._common import Coverage, is_abbrev, mentions, query_diseases, search_names

log = logging.getLogger(__name__)
API = "https://api.brightdata.com/request"
# whole words in title/snippet (substrings would hit 'fundamental', 'secure'); substrings in the host
ORG_HINTS = ("foundation", "association", "alliance", "society", "support", "families", "family", "registry",
             "network", "charity", "charities", "trust", "stiftung", "fund", "hope", "cure")
_HINT_RX = re.compile(r"\b(" + "|".join(ORG_HINTS) + r")s?\b", re.I)
SKIP_DOMAINS = ("wikipedia.org", "ncbi.nlm.nih.gov", "pubmed", "medlineplus", "rarediseases.info.nih.gov",
                "orpha.net", "omim.org", "mayoclinic", "clevelandclinic", "malacards", "youtube.com",
                "facebook.com", "nature.com", "sciencedirect", "springer", "wiley", "linkedin.com",
                "instagram.com", "twitter.com", "x.com", "reddit.com", "clinicaltrials.gov", "google.",
                "biorxiv.org", "medrxiv.org", "researchgate.net", "frontiersin.org", "mdpi.com", "plos.org",
                "tandfonline", "sagepub", "karger", "oup.com", "cell.com", "bmj.com", "thelancet", "jamanetwork",
                "ukri.org", "cordis.europa.eu", "medscape", "news",
                # literature, repositories and databases are sources, not organisations
                "europepmc.org", "genecards.org", "dokumen.pub", "ovid.com", "researchsquare.com", "nih.gov",
                "nanbyodata.jp", "pedneur.com", "ardentapp.com", "citizen.health", "lunas.health", "genezen.com",
                "semanticscholar.org", "scholar.", "academia.edu", "iris.", "pure.", "openresearch.", "elib.",
                "eprints.", "repository.", "dspace.", "hal.science", "zenodo.org", "figshare.com",
                # disease encyclopedias, symptom checkers, health news and genetic-test vendors describe a disease,
                # they do not serve its families
                "rarediseases.org", "webmd.com", "healthline.com", "verywellhealth.com", "medicalnewstoday.com",
                "drugs.com", "statpearls", "msdmanuals.com", "merckmanuals.com", "uptodate.com", "emedicine",
                "sciencedaily.com", "invitae.com", "blueprintgenetics.com", "preventiongenetics.com", "genedx.com",
                "fulgentgenetics.com", "centogene.com", "ambrygen.com", "3billion.io", "uniprot.org", "ebi.ac.uk",
                "ensembl.org", "gene.vision", "simplywiki", "wikiwand.com", "britannica.com", "amazon.",
                "epilepsydiagnosis.org", "neuromuscular.wustl.edu", "patient.info", "nhs.uk", "gov.uk")
_BRD_ERROR_HEADERS = ("x-brd-error", "x-brd-err-code", "x-brd-err-msg")
PER_DISEASE = 5


class BrightDataError(RuntimeError):
    """A Bright Data request failed; the message never contains the API key. `fatal` marks account / zone
    configuration errors (IP not on the zone allowlist, unknown zone, bad key) that no retry can fix."""

    def __init__(self, msg: str, fatal: bool = False):
        super().__init__(msg)
        self.fatal = fatal


# x-brd-err-code values / messages that mean the zone or account is misconfigured for this machine
FATAL_CODES = {"client_10030", "client_10000", "client_10010"}
_FATAL_MSG = re.compile(r"not whitelisted|zone not found|invalid (?:api )?(?:key|token)|unauthori[sz]ed", re.I)
# after a fatal error emit only reads cached searches (set per run by emit; cache hits are free)
_CACHE_ONLY = False


class BudgetExhausted(BrightDataError):
    """The persistent Bright Data request budget is used up; nothing was sent."""


class Budget:
    """Persistent count of billed Bright Data requests (DATA_DIR/brightdata_budget.json). take() is called before
    every billed call; it records the call and raises BudgetExhausted once `limit` calls have been made."""

    def __init__(self, path=None, limit: int | None = None):
        self.path = path or (DATA_DIR / "brightdata_budget.json")
        self.limit = int(limit if limit is not None else env("ATLAS_BRIGHTDATA_MAX_REQUESTS", "400"))

    def _read(self) -> dict:
        try:
            return json.loads(self.path.read_text())
        except (OSError, ValueError):
            return {"used": 0, "by_kind": {}, "log": []}

    @property
    def used(self) -> int:
        return int(self._read().get("used", 0))

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.used)

    def take(self, kind: str, ref: str = "") -> None:
        st = self._read()
        if int(st.get("used", 0)) >= self.limit:
            raise BudgetExhausted(f"Bright Data budget exhausted ({st.get('used', 0)}/{self.limit} requests used)")
        st["used"] = int(st.get("used", 0)) + 1
        st.setdefault("by_kind", {})[kind] = int(st.get("by_kind", {}).get(kind, 0)) + 1
        st.setdefault("log", []).append({"at": now_iso(), "kind": kind, "ref": ref[:200]})
        st["limit"] = self.limit
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(st, indent=1))
        tmp.replace(self.path)


_GENE_SUBTYPE = re.compile(r"^(.+?)\s+(?:caused by (?:a )?mutations? in|due to (?:a )?mutations? in)\s+\S+$", re.I)
_NUMBERED = re.compile(r"(?:,|\s)\s*(?:type\s+)?\d+[a-z]?$", re.I)


def query_name(d: dict, by_gene: bool = False, genes: list[str] | None = None,
               rollup: str | None = None) -> tuple[str, bool]:
    """(name to search for, whether it is an acronym that needs the word 'disease'). See search_query.
    rollup: the label of a broader focus disease of the same family that speaks for d ('Leigh syndrome' for
    'Leigh syndrome with cardiomyopathy'), used by query_by_gene families when no gene name fits."""
    names = search_names(d)
    if by_gene:
        label = d["label"]
        if m := _GENE_SUBTYPE.match(label):     # 'MELAS syndrome caused by mutation in MTTL1' -> 'MELAS syndrome'
            return m.group(1).strip(" ,"), False
        if _NUMBERED.search(label) and genes and len(genes) == 1:
            return genes[0], False               # 'developmental and epileptic encephalopathy, 11' -> 'SCN2A'
        return rollup or label, False
    primary = next((n for n in names[1:] if is_abbrev(n) and any(c.isdigit() for c in n)), None)
    return (primary, True) if primary else (names[0], False)


def search_query(d: dict, by_gene: bool = False, genes: list[str] | None = None, rollup: str | None = None) -> str:
    """'"CLN5" disease foundation families support': the first digit-bearing acronym (what org sites
    write), else the label; with by_gene (families with query_by_gene) a numbered subtype's single causal gene,
    and a gene-defined subtype rolled up to its syndrome. No OR operators: Google parses `a OR b OR c` loosely
    and drops the quoted phrase ('"CLN5 disease" patient organization OR foundation' returned the UN Foundation)."""
    name, acronym = query_name(d, by_gene, genes, rollup)
    return f'"{name}" disease foundation families support' if acronym else f'"{name}" foundation families support'


def registry_query(d: dict, by_gene: bool = False, genes: list[str] | None = None, rollup: str | None = None) -> str:
    """'"CLN5" patient registry': registries and natural history studies run by families' organisations."""
    return f'"{query_name(d, by_gene, genes, rollup)[0]}" patient registry'


def brightdata_request(query: str, budget: Budget | None = None) -> list[dict]:
    """One Google search via the Bright Data request API -> [{title, url, snippet}] (cached; a cache hit is free).
    Raises BrightDataError / BudgetExhausted / httpx.HTTPError; nothing is cached on failure."""
    key = env("BRIGHTDATA_API_KEY")
    if not key:
        raise BrightDataError("BRIGHTDATA_API_KEY not set")
    body = {"zone": env("BRIGHTDATA_SERP_ZONE", "mcp_unlocker"),
            "url": f"https://www.google.com/search?q={quote_plus(query)}&hl=en&brd_json=1", "format": "raw"}
    p = _cache_path("brightdata", API + "#" + json.dumps(body, sort_keys=True))
    if p.exists() and not REFRESH:
        data = json.loads(p.read_text())
    elif _CACHE_ONLY:
        raise BrightDataError("not cached; Bright Data disabled for this run after a configuration error")
    else:
        # retries=1: emit() owns the retry policy (2 attempts); http.request's default 5 retries with
        # backoff would multiply billed requests
        (budget or Budget()).take("serp", query)
        r = request("POST", API, json=body, headers={"Authorization": f"Bearer {key}"}, retries=1)
        data = parse_response(r)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data))
    organic = data.get("organic") if isinstance(data, dict) else None
    return [{"title": str(o.get("title") or ""), "url": str(o.get("link") or o.get("url") or ""),
             "snippet": str(o.get("description") or o.get("snippet") or "")}
            for o in organic or [] if isinstance(o, dict) and (o.get("link") or o.get("url"))]


def parse_response(r: httpx.Response) -> dict:
    """Body of a Bright Data answer, or BrightDataError (it answers 200 with x-brd-err-* on failure)."""
    brd = {h: r.headers[h].strip() for h in _BRD_ERROR_HEADERS if r.headers.get(h, "").strip()}
    if brd:
        code = brd.get("x-brd-err-code", "")
        msg = brd.get("x-brd-err-msg") or brd.get("x-brd-error", "")
        raise BrightDataError(f"Bright Data error {code}: {msg}"[:300],
                              fatal=code in FATAL_CODES or bool(_FATAL_MSG.search(msg)))
    if r.headers.get("x-brd-serp-warn"):
        log.warning("brightdata serp warning: %s", r.headers["x-brd-serp-warn"][:200])
    if not r.content.strip():
        raise BrightDataError("Bright Data returned an empty body")
    try:
        data = r.json()
    except ValueError:
        raise BrightDataError("Bright Data returned non-JSON (check the zone and brd_json=1)") from None
    if not isinstance(data, dict):
        raise BrightDataError("Bright Data returned unexpected JSON")
    return data


# a registry the site itself runs or enrols into ('join our patient registry'), not any mention of one
# ('natural history study news' on a news page is not a registry)
REGISTRY_RX = re.compile(r"[^.!?\n]{0,160}\b(?:our|join (?:the|our)|enrol(?:l)?(?:ing|ment)? in (?:the|our)|"
                         r"sign up (?:for|to) (?:the|our)|register (?:for|with) (?:the|our))\b[^.!?\n]{0,60}?"
                         r"\b(?:patient registry|natural history (?:study|registry)|registry)\b[^.!?\n]{0,160}", re.I)
CONTACT_RX = re.compile(r"contact|get in touch|kontakt", re.I)
_SENTENCE = re.compile(r"(?<=[.!?])\s+|\n+")
BROWSER_CONCURRENCY = 3       # parallel remote browser sessions
PAGE_TIMEOUT_MS = 60_000


MAX_SENTENCES = 30


def sentences_naming(text: str | None, names: list[str]) -> list[str]:
    """Sentences/lines of page text that name the disease, whitespace collapsed (<= 500 chars each)."""
    out: list[str] = []
    for s in _SENTENCE.split(text or ""):
        s = " ".join(s.split())
        if len(s) >= 20 and mentions(s, names) and s[:500] not in out:
            out.append(s[:500])
            if len(out) >= MAX_SENTENCES:
                break
    return out


# 'Noah's Hope', 'Neurogene Inc', 'Beyond Batten Disease Foundation': a capitalised name + an org word
_ORG_NAME_RX = re.compile(r"\b(?:[A-Z][\w'’&.-]*\s+){1,5}(?:Foundation|Fund|Hope|Trust|Association|Society|"
                          r"Alliance|Charity|Network|Institute|Inc|Ltd|LLC|Therapeutics|Pharmaceuticals?|"
                          r"Biosciences|Biotherapeutics|Biologics)\b")
_FIRST_PERSON_RX = re.compile(r"\b(?:[Ww]e|[Oo]ur|us)\b")
_ABOUT_PATH_RX = re.compile(r"about|who-we-are|our-story|mission", re.I)


def _org_tokens(org_names: list[str]) -> list[str]:
    """Lower-case strings that identify the organisation itself: its full names (site name, curated label
    and synonyms), acronyms in them ('BDSRA'; not digit-bearing ones like 'CLN5', which are disease
    names) and the host's first label when it is distinctive (>= 5 chars). Generic words ('Batten',
    'disease') are not tokens, or every disease sentence would 'name the org'."""
    out: set[str] = set()
    for n in org_names:
        n = " ".join((n or "").strip(" -|\u2013:").split())
        if len(n) < 3:
            continue
        for full in {n, " ".join(re.sub(r"\([^)]*\)", " ", n).split())}:  # with and without '(BDSRA)'
            if " " in full or len(full) >= 5:
                out.add(full.lower())
        out.update(w.lower() for w in (x.strip("(),.:;") for x in n.split())
                   if len(w) >= 3 and w.isupper() and w.isalpha())
    return sorted(out, key=len, reverse=True)


def _mask_org(s: str, tokens: list[str]) -> tuple[str, bool]:
    """s with this organisation's names blanked out (so they are not taken for another org), and
    whether any was found."""
    found = False
    for t in tokens:
        s, n = re.subn(r"(?<![\w])" + re.escape(t) + r"(?![\w])", " \u2026 ", s, flags=re.I)
        found = found or n > 0
    return s, found


def pick_quote(sentences: list[str], org_names: list[str], url: str | None) -> str | None:
    """The first disease-naming sentence that is about this organisation: it names no OTHER organisation
    and either names this one / speaks as 'we/our', or sits on the homepage or about page. None if no
    sentence qualifies (a directory or news page about someone else must not back the edge)."""
    tokens = _org_tokens(org_names)
    path = urlparse(url or "").path
    about_page = path in ("", "/") or bool(_ABOUT_PATH_RX.search(path))
    for s in sentences:
        masked, names_self = _mask_org(s, tokens)
        if _ORG_NAME_RX.search(masked):
            continue  # names another organisation: about someone else
        if about_page or names_self or _FIRST_PERSON_RX.search(s):
            return s
    return None


class _PageReader(HTMLParser):
    """Visible text (block elements -> newlines; script/style skipped), <title>, meta, links."""
    BLOCK = {"p", "div", "br", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "td", "th", "section",
             "article", "header", "footer", "nav", "main", "aside", "blockquote", "table", "form", "dd", "dt"}
    SKIP = {"script", "style", "noscript", "template", "svg", "head"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title = ""
        self.meta: dict[str, str] = {}
        self.links: list[tuple[str, str]] = []
        self._skip = 0
        self._in_title = False
        self._href: str | None = None
        self._atext: list[str] = []

    def handle_starttag(self, tag, attrs):
        a = {k: v or "" for k, v in attrs}
        if tag == "meta":
            k = (a.get("name") or a.get("property") or "").lower()
            if k in ("description", "og:site_name"):
                self.meta[k] = a.get("content", "")
        elif tag == "title":
            self._in_title = True
        elif tag == "a" and a.get("href"):
            self._href, self._atext = a["href"], []
        if tag in self.SKIP:
            self._skip += 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip:
            self._skip -= 1
        elif tag == "title":
            self._in_title = False
        elif tag == "a" and self._href is not None:
            self.links.append(("".join(self._atext).strip(), self._href))
            self._href = None
        if tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif not self._skip:
            self.parts.append(data)
            if self._href is not None:
                self._atext.append(data)


def read_page(url: str, body: str, names: list[str]) -> dict:
    """The same fields as the browser visit, from raw HTML. Quotes come from the visible text."""
    p = _PageReader()
    p.feed(body)
    text = "".join(p.parts)
    reg = REGISTRY_RX.search(text)
    contact = next((h for t, h in p.links if CONTACT_RX.search(t or "") or h.startswith("mailto:")), None)
    return {"final_url": url, "title": " ".join(html.unescape(p.title).split()),
            "site_name": " ".join(p.meta.get("og:site_name", "").split())[:120],
            "description": " ".join(p.meta.get("description", "").split())[:400],
            "disease_sentences": sentences_naming(text, names),
            "registry_quote": " ".join(reg.group(0).split()) if reg else None,
            "contact_url": urljoin(url, contact) if contact else None}


def fetch_page(url: str) -> tuple[str, str]:
    """(final url after redirects, body) for a result page, without Bright Data. Cached with the final
    url, so a redirect from an old domain to a curated org's website is seen on every run."""
    p = _cache_path("brightdata_pages", "final#" + url)
    if p.exists() and not REFRESH:
        d = json.loads(p.read_text())
        return d["final_url"], d["body"]
    r = request("GET", url, retries=2)
    final = str(r.url)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"final_url": final, "body": r.text}))
    return final, r.text


def unlocker_fetch(url: str, budget: Budget | None = None) -> tuple[str, str]:
    """(url, body) of a page fetched through the Bright Data Web Unlocker (one billed request, cached like
    fetch_page). Raises BrightDataError / BudgetExhausted / httpx.HTTPError."""
    p = _cache_path("brightdata_pages", "unlocker#" + url)
    if p.exists() and not REFRESH:
        d = json.loads(p.read_text())
        return d["final_url"], d["body"]
    key = env("BRIGHTDATA_API_KEY")
    if not key:
        raise BrightDataError("BRIGHTDATA_API_KEY not set")
    zone = env("BRIGHTDATA_UNLOCKER_ZONE") or env("BRIGHTDATA_SERP_ZONE") or "mcp_unlocker"
    (budget or Budget()).take("unlocker", url)
    r = request("POST", API, json={"zone": zone, "url": url, "format": "raw"},
                headers={"Authorization": f"Bearer {key}"}, retries=1)
    brd = {h: r.headers[h].strip() for h in _BRD_ERROR_HEADERS if r.headers.get(h, "").strip()}
    if brd or not r.text.strip():
        raise BrightDataError(f"Bright Data unlocker: {brd.get('x-brd-err-msg') or brd.get('x-brd-error') or 'empty body'}"[:300])
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"final_url": url, "body": r.text}))
    return url, r.text


def _direct_visit(url: str, names: list[str]) -> dict:
    """Fetch and read a result page (cached); {} on failure."""
    try:
        final, body = fetch_page(url)
        return read_page(final, body, names)
    except Exception as e:  # one bad site must not stop the run
        log.info("direct visit failed %s: %s", url, type(e).__name__)
        return {}


def host_of(url: str | None) -> str:
    return (urlparse(url or "").hostname or "").lower().removeprefix("www.")


async def _google(browser, query: str) -> list[dict]:
    page = await browser.new_page()
    try:
        await page.goto(f"https://www.google.com/search?q={quote_plus(query)}&hl=en",
                        timeout=PAGE_TIMEOUT_MS, wait_until="domcontentloaded")
        rows = await page.eval_on_selector_all(
            "a:has(h3)",
            """els => els.map(a => {
                 const box = a.closest('div.g, div[data-hveid]');
                 const snip = box ? box.innerText.split('\\n').slice(1).join(' ') : '';
                 return {title: a.querySelector('h3').innerText, url: a.href, snippet: snip.slice(0, 400)};
               })""")
        return [r for r in rows if r["url"].startswith("http") and "google." not in host_of(r["url"])]
    finally:
        await page.close()


async def _visit(browser, url: str, names: list[str]) -> dict:
    """Read a result page: final url, site name, description, the sentences naming the disease, a registry
    mention, a contact link. Quotes are text from the page body."""
    page = await browser.new_page()
    try:
        await page.goto(url, timeout=PAGE_TIMEOUT_MS, wait_until="domcontentloaded")
        title = (await page.title() or "").strip()
        desc = await page.evaluate("() => document.querySelector('meta[name=description]')?.content || ''")
        site_name = await page.evaluate(
            "() => document.querySelector('meta[property=\"og:site_name\"]')?.content || ''")
        text = await page.evaluate("() => document.body ? document.body.innerText : ''")
        links = await page.eval_on_selector_all("a[href]", "els => els.map(a => [a.innerText.trim(), a.href])")
        reg = REGISTRY_RX.search(text or "")
        contact = next((h for t, h in links if CONTACT_RX.search(t or "") or h.startswith("mailto:")), None)
        return {"final_url": page.url, "title": title, "site_name": " ".join((site_name or "").split())[:120],
                "description": (desc or "").strip()[:400],
                "disease_sentences": sentences_naming(text, names),
                "registry_quote": " ".join(reg.group(0).split()) if reg else None,
                "contact_url": urljoin(page.url, contact) if contact else None}
    except Exception as e:  # one bad site must not stop the run
        log.info("brightdata visit failed %s: %s", url, type(e).__name__)
        return {}
    finally:
        await page.close()


def candidates(rows: list[dict], n: int) -> list[dict]:
    """Org-looking results, one per host, at most n."""
    seen: set[str] = set()
    out = []
    for r in rows:
        h = host_of(r["url"])
        if h and h not in seen and looks_like_org(r):
            seen.add(h)
            out.append(r)
    return out[:n]


async def _browser_run(wss: str, jobs: list[dict], per_disease: int, visit: bool = False) -> None:
    """jobs: {disease, query, names, leads|None}; fills leads in place (searching in the browser when None)
    and, with visit=True, lead['site'] for leads whose direct fetch failed. Failures are per disease / site."""
    from playwright.async_api import async_playwright
    sem = asyncio.Semaphore(BROWSER_CONCURRENCY)

    async def one(pw, job: dict) -> None:
        d = job["disease"]
        async with sem:
            try:
                Budget().take("browser", d["id"])
            except BudgetExhausted as e:
                log.warning("brightdata browser skipped for %s: %s", d["id"], e)
                return
            try:
                browser = await pw.chromium.connect_over_cdp(wss, timeout=PAGE_TIMEOUT_MS)
            except Exception as e:
                log.warning("brightdata browser connect failed for %s: %s", d["id"], type(e).__name__)
                return
            try:
                if job["leads"] is None:
                    job["leads"] = candidates(await _google(browser, job["query"]), per_disease)
                for r in job["leads"] if visit else []:
                    if not r.get("site"):
                        r["site"] = await _visit(browser, r["url"], job["names"])
            except Exception as e:
                log.warning("brightdata browser failed for %s: %s", d["id"], type(e).__name__)
            finally:
                await browser.close()

    async with async_playwright() as pw:
        await asyncio.gather(*(one(pw, j) for j in jobs))


def _skip(host: str, s: str) -> bool:
    """'x.com' matches x.com / www.x.com but not fedex.com; 'pubmed', 'google.', 'news' match anywhere."""
    if "." in s.strip("."):
        return host == s or host.endswith("." + s)
    return s in host


def looks_like_org(r: dict) -> bool:
    """Not a journal/preprint/news/encyclopedia host, and an org word in the title, snippet or host."""
    host = host_of(r["url"])
    if not host or host.startswith("hcp.") or any(_skip(host, s) for s in SKIP_DOMAINS):  # hcp.: pharma HCP portals
        return False
    return bool(_HINT_RX.search(f"{r['title']} {r['snippet']}")) or any(h in host for h in ORG_HINTS)


def curated_sites() -> list[tuple[str, str, str, list[str]]]:
    """[(host, path prefix, curated ORG id, names)] from config/curated.yaml `website`s, so web leads reuse
    curated org ids instead of minting ORG:<host> duplicates (bdsrafoundation.org -> ORG:bdsra)."""
    out = []
    for o in (curated_config() or {}).get("organizations") or []:
        if isinstance(o, dict) and o.get("website"):
            u = urlparse(str(o["website"]))
            names = [str(o.get("label") or "")] + [str(x) for x in o.get("synonyms") or []]
            out.append((host_of(str(o["website"])), u.path.rstrip("/"),
                        str(o.get("id") or org_id(str(o.get("label") or ""))), names))
    return out


def curated_match(url: str | None, sites: list[tuple]) -> tuple[str, list[str]] | None:
    """(curated ORG id, its names) whose website (host + path prefix, e.g. ucl.ac.uk/ncl-disease) covers url."""
    h, path = host_of(url), urlparse(url or "").path
    return next(((s[2], list(s[3]) if len(s) > 3 else []) for s in sites
                 if h == s[0] and (not s[1] or path == s[1] or path.startswith(s[1] + "/"))), None)


def download() -> None:
    return None


def _emit_lead(g: GraphWriter, d: dict, r: dict, names: list[str], query: str,
               curated: list[tuple]) -> bool:
    """Write one lead; False if it is dropped (does not name the disease, or adds nothing to a curated org)."""
    site = r.get("site") or {}
    page = site.get("final_url") or r["url"]
    host = host_of(page)
    sentences = site.get("disease_sentences") or []
    if not (sentences or mentions(f"{r['title']} {r['snippet']}", names)):
        return False
    # the post-redirect url decides (an old domain that redirects to a curated org's website is that org)
    match = curated_match(page, curated) if site.get("final_url") else None
    match = match or curated_match(r["url"], curated)
    cid = match[0] if match else None
    org_names = (match[1] if match else []) + [site.get("site_name") or "", host.split(".")[0]]
    page_quote = pick_quote(sentences, org_names, page)
    if cid and not page_quote:
        return False  # curated already describes this org; a bare search hit adds nothing honest
    oid = cid or org_id(host)
    if not cid:
        homepage = urlparse(page).path in ("", "/")  # a subpage's meta description is about that page
        g.node(id=oid, type="organization", subtype="patient_group",
               label=(site.get("site_name") or "").strip(" -|–:") or host, url=f"https://{host}",
               description=(site.get("description") or None) if homepage else None,
               attrs={"website": f"https://{host}", "unverified": True, "source": "brightdata",
                      "contact_url": site.get("contact_url"),
                      "has_registry": True if site.get("registry_quote") else None})
    if page_quote:
        ev = dict(source_type="patient_org_site", source_name=host, source_ref=page, url=page,
                  quote=page_quote, method="scrape:brightdata")
    else:
        ev = dict(source_type="web", source_name="Web search (Bright Data)", source_ref=r["url"], url=r["url"],
                  quote=None, method="scrape:brightdata")
    g.edge("organization_serves_disease", oid, d["id"], status="hypothesis", label="may support families with",
           evidence=ev, attrs={"search_query": query, "search_snippet": (r.get("snippet") or "")[:500] or None})
    if site.get("registry_quote") and not cid:  # curated orgs' assets come from curated.yaml
        aid = asset_id(f"{host} registry")
        g.node(id=aid, type="asset", subtype="registry", label=f"Registry run by {host}", url=f"https://{host}",
               attrs={"asset_kind": "registry", "owner_id": oid, "unverified": True, "access": "on_request"})
        ev = dict(source_type="patient_org_site", source_name=host, source_ref=page, url=page,
                  quote=site["registry_quote"][:500], method="scrape:brightdata")
        g.edge("organization_maintains_asset", oid, aid, status="hypothesis", label="maintains", evidence=ev)
        g.edge("asset_covers_disease", aid, d["id"], status="hypothesis", label="may cover", evidence=ev)
    return True


def _run_browser(wss: str, jobs: list[dict], per_disease: int, visit: bool) -> None:
    try:
        asyncio.run(_browser_run(wss, jobs, per_disease, visit=visit))
    except Exception as e:  # e.g. playwright not installed: keep the search-only leads
        log.warning("brightdata browser unavailable: %s", type(e).__name__)


class _Context:
    """What emit needs to know about each disease from earlier stages (all optional: absent files -> empty):
    its family (query_by_gene), its curated causal genes, whether a curated (non-hypothesis) group serves exactly
    it, and its parents (MONDO is_a)."""

    def __init__(self) -> None:
        sl = read_json("slice", {}) or {}
        fams = {f.key: f for f in slice_config().families}
        self.by_gene = {d: bool(getattr(fams.get(k), "query_by_gene", False))
                        for d, k in (sl.get("family_of") or {}).items()}
        labels: dict[str, str] = {}
        self.genes: dict[str, set[str]] = {}
        self.parents: dict[str, set[str]] = {}
        for st in ("mondo", "hgnc", "hpo", "orphanet"):
            for n in read_jsonl(f"{st}.nodes.jsonl"):
                if n.get("type") == "gene":
                    labels.setdefault(n["id"], n["label"])
        for st in ("mondo", "hpo", "orphanet"):
            for e in read_jsonl(f"{st}.edges.jsonl"):
                if e["type"] == "gene_associated_with_disease" and e["status"] == "curated" and e["src"] in labels:
                    self.genes.setdefault(e["dst"], set()).add(labels[e["src"]])
                elif e["type"] == "disease_subtype_of" and st == "mondo":
                    self.parents.setdefault(e["src"], set()).add(e["dst"])
        self.grouped = {e["dst"] for e in read_jsonl("curated.edges.jsonl")
                        if e["type"] == "organization_serves_disease" and e["status"] != "hypothesis"}
        self.family_of = sl.get("family_of") or {}
        self.focus = set(sl.get("focus") or [])
        self.labels = {n["id"]: n["label"] for n in read_jsonl("mondo.nodes.jsonl") if n["id"] in self.focus}

    def rollup(self, d: str) -> str | None:
        """Label of a shorter focus disease of d's family that d's label contains, else of a focus parent."""
        fam, label = self.family_of.get(d), self.labels.get(d, "")
        same = [x for x in sorted(self.focus) if x != d and self.family_of.get(x) == fam and x in self.labels]
        inside = sorted((self.labels[x] for x in same if len(self.labels[x]) < len(label)
                         and re.search(r"(?<!\w)" + re.escape(self.labels[x]) + r"(?!\w)", label, re.I)), key=len)
        if inside:
            return inside[0]
        return next((self.labels[p] for p in sorted(self.parents.get(d, ())) if p in same), None)

    def query_args(self, d: str) -> dict:
        return {"by_gene": self.by_gene.get(d, False), "genes": sorted(self.genes.get(d, ())),
                "rollup": self.rollup(d) if self.by_gene.get(d) else None}

    def covered_by_parent(self, d: str, focus: set[str]) -> bool:
        """A focus parent with the same (non-empty) causal genes speaks for d ('juvenile CLN5' -> CLN5 disease)."""
        mine = self.genes.get(d)
        return bool(mine) and any(p in focus and self.genes.get(p) == mine for p in self.parents.get(d, ()))


def match_names(d: dict, ctx: _Context) -> list[str]:
    """Names a result or page must contain to count as a lead for d: its label, synonyms and acronyms, plus the
    causal gene when d is searched by that gene ('SCN2A' for DEE11: foundations write 'SCN2A', not 'DEE11').
    A rolled-up broader name never counts (a 'Leigh syndrome' page is not about Leigh syndrome with cardiomyopathy)."""
    names = search_names(d, k=8)
    args = ctx.query_args(d["id"])
    qn = query_name(d, **args)[0]
    if args["by_gene"] and qn in args["genes"] and qn not in names:
        names.append(qn)
    return names


def _search(j: dict, query: str, per_disease: int) -> bool:
    """Run one search for job j (2 attempts) and merge its org-looking results, one per host, into j['leads'].
    False if the budget is exhausted (the caller stops searching). A configuration error (fatal: IP not on the
    zone allowlist, unknown zone, bad key; HTTP 400/401/403) switches the run to cached searches only."""
    global _CACHE_ONLY
    for attempt in (1, 2):  # x-brd errors such as 'redirect location was rejected' are often transient
        try:
            rows = candidates(brightdata_request(query), per_disease)
        except BudgetExhausted as e:
            log.warning("brightdata: %s; stopping searches", e)
            return False
        except (BrightDataError, httpx.HTTPError) as e:
            status = getattr(getattr(e, "response", None), "status_code", None)
            if getattr(e, "fatal", False) or status in (400, 401, 403):
                log.error("brightdata: configuration error (%s); no more requests this run, cached searches only",
                          str(e)[:200])
                _CACHE_ONLY = True
                break
            if _CACHE_ONLY:   # not in the cache and nothing may be sent
                break
            log.warning("brightdata search failed for %s (attempt %d): %s", j["disease"]["id"], attempt, e)
            continue
        hosts = {host_of(r["url"]) for r in j["leads"] or []}
        j["leads"] = (j["leads"] or []) + [dict(r, query=query) for r in rows if host_of(r["url"]) not in hosts]
        j["searched"].append(query)
        return True
    j["failed"].append(query)
    return True


def emit(diseases: list[dict] | None = None, per_disease: int | None = None) -> None:
    g = GraphWriter("brightdata_orgs")
    wss, key = env("BRIGHTDATA_BROWSER_WSS"), env("BRIGHTDATA_API_KEY")
    if not (wss or key):
        log.info("brightdata_orgs skipped (neither BRIGHTDATA_BROWSER_WSS nor BRIGHTDATA_API_KEY set)")
        g.close()
        return
    if diseases is None:
        diseases = query_diseases()
        only = [x.strip() for x in (env("ATLAS_BRIGHTDATA_DISEASES") or "").split(",") if x.strip()]
        if only:
            diseases = [d for d in diseases if d["id"] in only]
    per_disease = per_disease or int(env("ATLAS_BRIGHTDATA_PER_DISEASE", str(PER_DISEASE)))
    global _CACHE_ONLY
    _CACHE_ONLY = env("ATLAS_BRIGHTDATA_CACHE_ONLY") == "1"   # rebuild leads from cached searches, send nothing
    ctx = _Context()
    budget = Budget()
    used0 = budget.used
    # diseases without a curated group for exactly them first, so a budget stop hits the best-served ones
    diseases = sorted(diseases, key=lambda d: d["id"] in ctx.grouped)
    jobs = [{"disease": d, "query": search_query(d, **ctx.query_args(d["id"])), "names": match_names(d, ctx),
             "leads": None, "searched": [], "failed": []} for d in diseases]
    if key:
        ok = True
        for j in jobs:                                   # 1. organisation search for every disease
            if not (ok := _search(j, j["query"], per_disease)):
                break
        focus = {j["disease"]["id"] for j in jobs}
        reg_max = int(env("ATLAS_BRIGHTDATA_REGISTRY_MAX", "80"))
        n_reg = 0
        for j in jobs if ok else []:                     # 2. registry search where no exact group is known
            d = j["disease"]["id"]
            if d in ctx.grouped or ctx.covered_by_parent(d, focus) or n_reg >= reg_max:
                continue
            q = registry_query(j["disease"], **ctx.query_args(d))
            if q in j["searched"]:
                continue
            n_reg += 1
            if not _search(j, q, per_disease):
                break
    if wss and any(j["leads"] is None for j in jobs):  # search in the browser only where the API failed
        _run_browser(wss, [j for j in jobs if j["leads"] is None], per_disease, visit=False)
    for j in jobs:
        for r in j["leads"] or []:
            r["site"] = _direct_visit(r["url"], j["names"])
    # pages the direct GET could not read: Web Unlocker (opt-in, billed, capped)
    if key and env("ATLAS_BRIGHTDATA_UNLOCKER_PAGES") == "1" and not _CACHE_ONLY:
        cap, n = int(env("ATLAS_BRIGHTDATA_UNLOCKER_MAX", "80")), 0
        tried: dict[str, tuple[str, str] | None] = {}
        for j in jobs:
            for r in j["leads"] or []:
                if r["site"] or n >= cap:
                    continue
                if r["url"] not in tried:
                    n += 1
                    try:
                        tried[r["url"]] = unlocker_fetch(r["url"])
                    except BudgetExhausted as e:
                        log.warning("brightdata: %s; no more unlocker fetches", e)
                        n = cap
                        continue
                    except (BrightDataError, httpx.HTTPError) as e:
                        log.info("unlocker failed %s: %s", r["url"], str(e)[:120])
                        tried[r["url"]] = None
                        if getattr(e, "fatal", False):
                            n = cap
                if tried.get(r["url"]):
                    final, body = tried[r["url"]]
                    r["site"] = read_page(final, body, j["names"])
    # re-visiting failed pages in the browser is opt-in: it refuses most non-profit sites and is billed
    retry = [j for j in jobs if any(not r["site"] for r in j["leads"] or [])]
    if wss and retry and env("ATLAS_BRIGHTDATA_BROWSER_VISITS") == "1":
        _run_browser(wss, retry, per_disease, visit=True)
    cov, curated = Coverage("brightdata"), curated_sites()
    for j in jobs:
        query = " | ".join(j["searched"] or [j["query"]])
        if j["leads"] is None:
            cov.add(j["disease"]["id"], query, None)
            continue
        kept = sum(_emit_lead(g, j["disease"], r, j["names"], r.get("query") or j["query"], curated)
                   for r in j["leads"])
        cov.add(j["disease"]["id"], query, kept)
        log.info("brightdata %s: %d candidates, %d leads kept", j["disease"]["label"], len(j["leads"]), kept)
    cov.save()
    log.info("brightdata: %d billed requests this run, %d of %d used in total", budget.used - used0, budget.used,
             budget.limit)
    g.close()
