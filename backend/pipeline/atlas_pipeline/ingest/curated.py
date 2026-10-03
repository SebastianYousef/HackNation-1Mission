"""Hand-curated facts from config/curated.yaml: patient organisations, mechanisms, assets, interventions.
The YAML schema is documented at the top of that file.

Status rule: an edge is 'literature' when one of its supporting evidence entries carries a verbatim quote
(publication, regulator or organisation page); without a quote it is loaded as 'hypothesis' and logged.
Never 'curated' — that status belongs to curated databases. Entries without evidence are skipped.

Disease refs may be MONDO ids or xrefs (OMIM:/ORPHA:), mapped through MONDO equivalentTo; gene refs may be
HGNC ids or approved symbols, mapped through the HGNC table.

`python -m atlas_pipeline.ingest.curated --verify` re-fetches every evidence url and checks each quote is a
substring of the page text (tags stripped, whitespace collapsed). PubMed/PMC urls are checked against the NCBI
efetch abstract / full text and ClinicalTrials.gov urls against its API v2 record (both pages render client-side);
PDFs are reported as not checkable. `--unlocker` retries bot-blocked pages through the Bright Data Web Unlocker.
Network-dependent, so not part of `emit`.
"""
from __future__ import annotations

import html
import json
import logging
import re
import sys
from typing import Any, Callable, Iterator

from ..config import curated_config
from ..http import redact
from ..ids import asset_id, intervention_id, mech_id, normalize_curie, org_id
from ..models import SOURCE_TYPES, STANCES
from ..store import GraphWriter, read_json
from . import hgnc

log = logging.getLogger(__name__)

MECH_SUBTYPES = {"biological_process", "pathway"}
ASSET_SUBTYPES = {"registry", "natural_history_study", "animal_model", "cell_model", "biomarker", "biobank"}
ASSET_ACCESS = {"open", "on_request", "restricted", None}


def download() -> None:
    return None


def _ws(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def _entries(raw: Any, where: str, what: str) -> list[dict]:
    """A YAML list of mappings. A single mapping counts as a one-item list; anything else (a bare string such as the
    old `serves: [MONDO:…]` format, a number, …) is dropped with a warning instead of crashing the stage."""
    if raw is None:
        return []
    items = [raw] if isinstance(raw, dict) else raw if isinstance(raw, list) else [raw]
    out = [x for x in items if isinstance(x, dict)]
    for x in items:
        if not isinstance(x, dict):
            log.warning("curated %s: %s entry must be a mapping, got %r; skipped", where, what, x)
    return out


def _evidence(raw: Any, where: str) -> list[dict]:
    """YAML evidence entries -> GraphWriter evidence dicts. Malformed entries are dropped with a warning."""
    out: list[dict] = []
    for ev in _entries(raw, where, "evidence"):
        st, name = ev.get("source_type"), ev.get("source_name")
        if st not in SOURCE_TYPES or st == "computed" or not name or not (ev.get("url") or ev.get("source_ref")) \
                or (ev.get("stance") or "supports") not in STANCES:
            log.warning("curated %s: bad evidence entry dropped (source_type/source_name/url/stance): %s", where, ev)
            continue
        row = dict(source_type=st, source_name=str(name), source_ref=ev.get("source_ref") or ev.get("url"),
                   url=ev.get("url"), quote=_ws(str(ev["quote"])) if ev.get("quote") else None,
                   method=ev.get("method") or "manual", stance=ev.get("stance") or "supports",
                   published_at=None if ev.get("published_at") is None else str(ev["published_at"]))
        if ev.get("retrieved_at"):
            row["retrieved_at"] = str(ev["retrieved_at"])
        out.append(row)
    return out


class _Curated:
    def __init__(self, sl: dict):
        self.sl = sl
        self.diseases = set(sl["diseases"])
        self.genes = set(sl.get("genes") or [])
        self.g = GraphWriter("curated")
        self.skipped = 0

    # ---- reference resolution -----------------------------------------------------------------
    def disease(self, ref: Any, where: str) -> str | None:
        ref = normalize_curie(str(ref or ""))
        d = ref if ref and ref.startswith("MONDO:") else self.sl["xref_to_mondo"].get(ref)
        if d not in self.diseases:
            log.warning("curated %s: disease %s not in slice", where, ref)
            return None
        return d

    def gene(self, ref: Any, where: str) -> str | None:
        ref = str(ref or "").strip()
        rec = hgnc.resolve(normalize_curie(ref) if ref.upper().startswith("HGNC") else ref)
        if rec is None:
            log.warning("curated %s: gene %s not found in HGNC", where, ref)
            return None
        if rec["hgnc_id"] not in self.genes:
            log.info("curated %s: gene %s is outside the slice; adding its HGNC node", where, rec["symbol"])
            hgnc.gene_node(self.g, rec)
        return rec["hgnc_id"]

    @staticmethod
    def section(items: Any, name: str) -> list[dict]:
        """Section entries that are mappings with a label; others are skipped with a warning."""
        out = []
        for x in _entries(items, name, "section"):
            if not str(x.get("label") or "").strip():
                log.warning("curated %s: entry without label skipped: %s", name, x)
                continue
            x["label"] = str(x["label"]).strip()
            out.append(x)
        return out

    @staticmethod
    def node_id(entry: dict, prefix: str, default: str, where: str) -> str | None:
        nid = str(entry.get("id") or default).strip()
        if not nid.startswith(prefix) or nid == prefix:
            log.warning("curated %s: id %s must start with %s; entry skipped", where, nid, prefix)
            return None
        return nid

    # ---- edges ---------------------------------------------------------------------------------
    def link(self, type_: str, src: str | None, dst: str | None, raw_ev: Any, where: str, label: str,
             attrs: dict | None = None) -> str | None:
        if not src or not dst:
            self.skipped += 1
            return None
        ev = _evidence(raw_ev, where)
        if not ev:
            log.warning("curated %s: %s %s -> %s has no usable evidence; skipped", where, type_, src, dst)
            self.skipped += 1
            return None
        quoted = any(v["quote"] and v["stance"] == "supports" for v in ev)
        if not quoted:
            log.warning("curated %s: %s %s -> %s has no verbatim quote; loaded as hypothesis", where, type_, src, dst)
        return self.g.edge(type_, src, dst, status="literature" if quoted else "hypothesis", label=label,
                           attrs=attrs, evidence=ev)

    # ---- sections ------------------------------------------------------------------------------
    def organizations(self, items: Any) -> None:
        for o in self.section(items, "organizations"):
            oid = self.node_id(o, "ORG:", org_id(o["label"]), o["label"])
            if not oid:
                continue
            self.g.node(id=oid, type="organization", subtype=o.get("subtype"), label=o["label"],
                        description=o.get("description"), url=o.get("website"),
                        synonyms=[str(x) for x in o.get("synonyms") or []],
                        attrs={"website": o.get("website"), "country": o.get("country"),
                               "has_registry": o.get("has_registry"), "contact_url": o.get("contact_url"),
                               "source": "curated"})
            w = f"org {oid}"
            for s in _entries(o.get("serves"), w, "serves"):
                self.link("organization_serves_disease", oid, self.disease(s.get("disease"), w), s.get("evidence"),
                          w, "supports families with")

    def mechanisms(self, items: Any) -> None:
        for m in self.section(items, "mechanisms"):
            mid = self.node_id(m, "ATLAS:mech-", mech_id(m["label"]), m["label"])
            if not mid:
                continue
            if m.get("subtype") not in MECH_SUBTYPES:
                log.warning("curated mechanism %s: subtype %s not one of %s", mid, m.get("subtype"), MECH_SUBTYPES)
            self.g.node(id=mid, type="mechanism", subtype=m.get("subtype"), label=m["label"],
                        description=m.get("description"), attrs={"source": "curated"})
            w = f"mechanism {mid}"
            for x in _entries(m.get("genes"), w, "genes"):
                self.link("gene_in_mechanism", self.gene(x.get("gene"), w), mid, x.get("evidence"), w, "acts in")
            for x in _entries(m.get("diseases"), w, "diseases"):
                self.link("disease_involves_mechanism", self.disease(x.get("disease"), w), mid, x.get("evidence"),
                          w, "involves")

    def assets(self, items: Any, known_orgs: set[str]) -> None:
        for a in self.section(items, "assets"):
            aid = self.node_id(a, "ASSET:", asset_id(a["label"]), a["label"])
            if not aid:
                continue
            w = f"asset {aid}"
            if a.get("subtype") not in ASSET_SUBTYPES:
                log.warning("curated %s: subtype %s not one of %s", w, a.get("subtype"), ASSET_SUBTYPES)
            if a.get("access") not in ASSET_ACCESS:
                log.warning("curated %s: access %s not one of open|on_request|restricted", w, a.get("access"))
            owner = a.get("owner") if isinstance(a.get("owner"), dict) else {"id": a["owner"]} if a.get("owner") else {}
            oid = normalize_curie(str(owner["id"])) if owner.get("id") else None
            if oid and oid not in known_orgs:
                log.warning("curated %s: owner %s is not a curated organization (edge kept only if another "
                            "stage emits that node)", w, oid)
            self.g.node(id=aid, type="asset", subtype=a.get("subtype"), label=a["label"],
                        description=a.get("description"), url=a.get("url"),
                        attrs={"asset_kind": a.get("subtype"), "access": a.get("access"), "owner_id": oid,
                               **({"species": str(a["species"])} if a.get("species") else {}), "source": "curated"})
            if oid:
                self.link("organization_maintains_asset", oid, aid, owner.get("evidence"), w, "maintains")
            for x in _entries(a.get("covers"), w, "covers"):
                self.link("asset_covers_disease", aid, self.disease(x.get("disease"), w), x.get("evidence"), w,
                          "covers")
            for x in _entries(a.get("targets"), w, "targets"):
                self.link("asset_targets_gene", aid, self.gene(x.get("gene"), w), x.get("evidence"), w, "targets")

    def interventions(self, items: Any, known_mechs: set[str]) -> None:
        for i in self.section(items, "interventions"):
            iid = self.node_id(i, "ATLAS:int-", intervention_id(i["label"]), i["label"])
            if not iid:
                continue
            self.g.node(id=iid, type="intervention", subtype=i.get("subtype"), label=i["label"],
                        description=i.get("description"), synonyms=[str(x) for x in i.get("synonyms") or []])
            w = f"intervention {iid}"
            for t in _entries(i.get("treats"), w, "treats"):
                if t.get("approval") not in ("approved", "investigational"):
                    log.warning("curated %s: approval %s not approved|investigational", w, t.get("approval"))
                self.link("intervention_treats_disease", iid, self.disease(t.get("disease"), w), t.get("evidence"),
                          w, "treats", attrs={"approval": t.get("approval")})
            for t in _entries(i.get("targets"), w, "targets"):
                mid = normalize_curie(str(t.get("mechanism") or "")) or ""
                if not mid.startswith(("ATLAS:mech-", "REACT:")):
                    # the edge type is intervention -> mechanism; any other id would break EDGE_ENDPOINTS
                    log.warning("curated %s: target %r is not an ATLAS:mech-* or REACT:* id; skipped", w, mid)
                    self.skipped += 1
                    continue
                if mid.startswith("ATLAS:mech-") and mid not in known_mechs:
                    log.warning("curated %s: mechanism %s is not defined in curated.yaml", w, mid)
                self.link("intervention_targets_mechanism", iid, mid, t.get("evidence"), w, "targets")


def build(cur: dict, sl: dict) -> GraphWriter:
    """Build the curated stage in memory (no I/O); `emit` writes it."""
    c = _Curated(sl)
    cur = cur if isinstance(cur, dict) else {}
    c.organizations(cur.get("organizations"))
    c.mechanisms(cur.get("mechanisms"))
    c.assets(cur.get("assets"), {n for n, x in c.g.nodes.items() if x["type"] == "organization"})
    c.interventions(cur.get("interventions"), {n for n, x in c.g.nodes.items() if x["type"] == "mechanism"})
    statuses = [e["status"] for e in c.g.edges.values()]
    log.info("curated: %d nodes, %d edges (%d literature, %d hypothesis), %d entries skipped", len(c.g.nodes),
             len(statuses), statuses.count("literature"), statuses.count("hypothesis"), c.skipped)
    return c.g


def emit() -> None:
    build(curated_config(), read_json("slice")).close()


# ---- quote verification (manual; needs network) ----------------------------------------------------
_INLINE = r"a|abbr|b|em|font|i|small|span|strong|sub|sup|u"
_PUBMED = re.compile(r"^https?://pubmed\.ncbi\.nlm\.nih\.gov/(\d+)", re.I)
_PMC = re.compile(r"^https?://(?:pmc\.ncbi\.nlm\.nih\.gov|www\.ncbi\.nlm\.nih\.gov/pmc)/articles/PMC(\d+)", re.I)
_DSPACE = re.compile(r"^(https?://[^/]+)/handle/(\d+/\d+)/?$", re.I)
_CTGOV = re.compile(r"^https?://(?:www\.)?clinicaltrials\.gov/(?:study|ct2/show)/(NCT\d{8})", re.I)
_BROWSER = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/126.0 Safari/537.36", "Accept": "text/html,application/xhtml+xml,*/*;q=0.8"}


class NotCheckable(Exception):
    """The source can't be checked automatically (e.g. a PDF); reported separately, not as a failure."""


def page_text(raw: str, inline_join: bool = False) -> str:
    """Visible text of an HTML/XML page: tags stripped, entities decoded, whitespace collapsed. With inline_join,
    inline tags (<i>, <sup>, <span> …) are removed without a space, so 'CLN5<sup>Y392X</sup>' reads 'CLN5Y392X'."""
    raw = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", raw)
    raw = re.sub(r"(?s)<!--.*?-->", " ", raw)
    if inline_join:
        raw = re.sub(rf"(?is)</?(?:{_INLINE})\b[^>]*>", "", raw)
    return _ws(html.unescape(re.sub(r"(?s)<[^>]+>", " ", raw)))


def _strings(x: Any) -> Iterator[str]:
    if isinstance(x, str):
        yield x
    elif isinstance(x, dict):
        for v in x.values():
            yield from _strings(v)
    elif isinstance(x, list):
        for v in x:
            yield from _strings(v)


def _dspace_api(url: str) -> str | None:
    """DSpace 7 handle pages are an Angular app with no text; the item record (incl. the abstract) is in the REST
    API."""
    m = _DSPACE.match(url)
    return f"{m[1]}/server/api/pid/find?id=hdl:{m[2]}" if m else None


def _texts(body: str, is_json: bool) -> list[str]:
    if is_json:
        return [_ws(s) for s in _strings(json.loads(body))]
    return [page_text(body), page_text(body, inline_join=True)]


def source_texts(url: str) -> list[str]:
    """Candidate texts a quote from `url` must be a substring of. PubMed and PMC pages are fetched through NCBI
    E-utilities efetch (the abstract / full text the quote was copied from) and ClinicalTrials.gov study pages
    through its API v2, because those pages are rendered client-side."""
    from ..http import request
    from .pubmed import EUTILS, _params
    if m := _PUBMED.match(url):
        r = request("GET", f"{EUTILS}/efetch.fcgi", params=_params(db="pubmed", id=m[1], rettype="abstract",
                                                                     retmode="text"), retries=3)
        return [_ws(r.text)]
    if m := _PMC.match(url):
        r = request("GET", f"{EUTILS}/efetch.fcgi", params=_params(db="pmc", id=m[1], retmode="xml"), retries=3)
        return [page_text(r.text), page_text(r.text, inline_join=True)]
    if m := _CTGOV.match(url):
        r = request("GET", f"https://clinicaltrials.gov/api/v2/studies/{m[1]}", retries=3)
        return [_ws(s) for s in _strings(r.json())]
    # retries=4: web.archive.org answers 429 / refuses connections for a while after a burst of requests
    api = _dspace_api(url)
    r = request("GET", api or url, retries=4, headers=_BROWSER)
    if "pdf" in r.headers.get("content-type", "") or r.content[:5] == b"%PDF-":
        raise NotCheckable("PDF")
    return _texts(r.text, bool(api))


def unlocker_texts(url: str) -> list[str]:
    """Fetch a bot-blocked page through the Bright Data Web Unlocker (opt-in fallback for --verify --unlocker).
    Zone: BRIGHTDATA_UNLOCKER_ZONE, else BRIGHTDATA_SERP_ZONE, else mcp_unlocker. Bright Data answers HTTP 200 even
    on failure and puts the reason in the x-brd-error / x-brd-err-msg headers with an empty body."""
    from ..config import env
    from ..http import request
    key = env("BRIGHTDATA_API_KEY")
    if not key:
        raise RuntimeError("BRIGHTDATA_API_KEY not set")
    zone = env("BRIGHTDATA_UNLOCKER_ZONE") or env("BRIGHTDATA_SERP_ZONE") or "mcp_unlocker"
    r = request("POST", "https://api.brightdata.com/request", retries=2, headers={"Authorization": f"Bearer {key}"},
                json={"zone": zone, "url": _dspace_api(url) or url, "format": "raw"})
    err = r.headers.get("x-brd-error") or r.headers.get("x-brd-err-msg")
    if err or not r.text.strip():
        raise RuntimeError(f"Bright Data unlocker: {err or 'empty body'} ({r.headers.get('x-brd-err-code', '')})")
    return _texts(r.text, bool(_dspace_api(url)))


def _quotes_by_url(cur: Any) -> dict[str, list[str]]:
    by_url: dict[str, list[str]] = {}

    def walk(x: Any) -> None:
        if isinstance(x, dict):
            if "source_type" in x and x.get("quote") and x.get("url"):
                q = _ws(str(x["quote"]))
                if q not in by_url.setdefault(str(x["url"]), []):
                    by_url[str(x["url"])].append(q)
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
    walk(cur)
    return by_url


def verify_quotes(cur: dict | None = None, fetch: Callable[[str], list[str]] = source_texts,
                  fallback: Callable[[str], list[str]] | None = None) -> dict[str, list]:
    """Fetch every evidence url in curated.yaml and check each quote is on the page. `fallback` (e.g. the Bright
    Data unlocker) is tried for a generic page whose direct fetch failed (bot wall, rate limit).
    Returns {"ok": [(url, quote)], "missing": [...], "failed": [...], "unchecked": [...]} (failed/unchecked rows
    carry the reason as a third item)."""
    by_url = _quotes_by_url(cur if cur is not None else curated_config())
    res: dict[str, list] = {"ok": [], "missing": [], "failed": [], "unchecked": []}
    for url, quotes in by_url.items():
        try:
            texts = fetch(url)
        except NotCheckable as e:
            res["unchecked"] += [(url, q, str(e)) for q in quotes]
            continue
        except Exception as e:  # noqa: BLE001 — report and continue
            texts = None
            if fallback and not (_PUBMED.match(url) or _PMC.match(url) or _CTGOV.match(url)):
                log.info("direct fetch failed (%s); trying fallback for %s", redact(e), url)
                try:
                    texts = fallback(url)
                except Exception as e2:  # noqa: BLE001
                    e = f"{redact(e)}; fallback: {redact(e2)}"
            if texts is None:
                res["failed"] += [(url, q, redact(e)) for q in quotes]
                continue
        for q in quotes:
            res["ok" if any(q in t for t in texts) else "missing"].append((url, q))
    log.info("checked %d quotes on %d pages: %d found, %d not on page, %d fetch failed, %d not checkable",
             sum(map(len, by_url.values())), len(by_url), len(res["ok"]), len(res["missing"]), len(res["failed"]),
             len(res["unchecked"]))
    return res


def main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if "--verify" not in argv:
        print("usage: python -m atlas_pipeline.ingest.curated --verify [--unlocker]")
        return 2
    # --unlocker: retry bot-blocked pages through Bright Data (costs one request per blocked page)
    res = verify_quotes(fallback=unlocker_texts if "--unlocker" in argv else None)
    for url, q in res["missing"]:
        print(f"QUOTE NOT ON PAGE {url}: {q[:100]!r}")
    for url, q, why in res["failed"]:
        print(f"FETCH FAILED {url}: {why}")
    for url, q, why in res["unchecked"]:
        print(f"NOT CHECKABLE ({why}) {url}: {q[:100]!r}")
    total = sum(map(len, res.values()))
    print(f"{len(res['ok'])}/{total} quotes found on their page; {len(res['missing'])} not on page, "
          f"{len(res['failed'])} fetch failed, {len(res['unchecked'])} not checkable")
    return 1 if res["missing"] or res["failed"] else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
