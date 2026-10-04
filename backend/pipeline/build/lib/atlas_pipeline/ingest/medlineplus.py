"""Family-friendly summaries: MedlinePlus Genetics (NLM) first, Orphanet definitions second.

Sources (both free and open, no login):
  https://medlineplus.gov/download/ghr-summaries.xml   MedlinePlus Genetics bulk XML (public domain, credit the
      National Library of Medicine). ~1,300 <health-condition-summary> and ~1,500 <gene-summary> records.
      Condition: name, ghr-page (url), text-list/text[text-role=description]/html (paragraphs),
                 related-gene-list/related-gene/gene-symbol, synonym-list, db-key-list (OMIM, GTR, MeSH, ...).
      Gene:      gene-symbol, ghr-page, text[text-role=function]/html.
      www.medlineplus.gov serves the same file (used when medlineplus.gov does not resolve).
  https://www.orphadata.com/data/xml/en_product1.xml   Orphanet nomenclature (CC-BY-4.0):
      Disorder/OrphaCode + SummaryInformation/TextSection[TextSectionType=Definition]/Contents.

Mapping (never fuzzy):
  MedlinePlus condition -> MONDO: its name equals a MONDO label or EXACT synonym of exactly one slice disease
      ("name"), else its OMIM keys map (MONDO equivalentTo, 1:1) to exactly one slice disease ("omim"). A condition
      whose OMIM keys map to several slice diseases is a group: each of those diseases is "listed" under it.
  Orphanet definition -> MONDO through the ORPHA equivalentTo map.

plain_summary per slice disease (node patch; the first one that applies):
  1. MedlinePlus exact match (name/omim)               the first 1-2 sentences of its description
  2. Orphanet definition                                the first 1-2 sentences
  3. the MONDO definition, unless it is a generated one ("Any X in which the cause of the disease is ...")
     -> nothing written; views fall back to the MONDO description
  4. "A form of <condition>. " + the MedlinePlus text of a group condition that lists the disease, or of the
     nearest broader disease (MONDO is_a, <= 3 levels) with an exact MedlinePlus match. The lead-in is a fixed
     template, never LLM text.
  Every patch carries attrs.plain_summary_source {source_type, source_name, source_ref, url, quote (the verbatim
  sentences used), match, status, license, retrieved_at}: 'curated' for 1, 2 and a listed group condition (curated
  databases saying it about this disease), 'inferred' for a broader disease's text (our hierarchy step).
plain_summary per slice gene: the first 1-2 sentences of the MedlinePlus gene's function text.

Edges: MedlinePlus related genes of an exact-matched condition -> gene_associated_with_disease, status curated
(MedlinePlus Genetics is an NLM-reviewed database), evidence source_type database, source_name "MedlinePlus
Genetics", url = the condition page, quote = the description sentence naming the gene (verbatim), else the
structured record "<condition> | related gene: <symbol>". Genes outside the slice get their HGNC node.
"""
from __future__ import annotations

import html
import logging
import re
import xml.etree.ElementTree as ET
from pathlib import Path

import httpx

from ..config import RAW
from ..http import download as dl, retrieved_at
from ..ids import norm_name
from ..store import GraphWriter, read_json, read_parquet, write_node_patches
from . import hgnc

log = logging.getLogger(__name__)
MP_URLS = ["https://medlineplus.gov/download/ghr-summaries.xml",
           "https://www.medlineplus.gov/download/ghr-summaries.xml"]
MP_PATH = RAW / "medlineplus" / "ghr-summaries.xml"
ORPHA_URL = "https://www.orphadata.com/data/xml/en_product1.xml"
ORPHA_PATH = RAW / "orphanet" / "en_product1.xml"
MP_NAME = "MedlinePlus Genetics"
MP_LICENSE = "Public domain; courtesy of MedlinePlus from the National Library of Medicine"
ORPHA_LICENSE = "CC-BY-4.0, Orphanet"
MAX_CHARS = 450
PARENT_DEPTH = 3
# MONDO's generated definitions read like a rule, not like a description a family could use
GENERATED_DEF = re.compile(r"^any\b|in which the cause of the disease is", re.I)
_SENT = re.compile(r"(?<=[.!?])\s+(?=[A-Z(\"'])")


def download() -> None:
    for url in MP_URLS:
        try:
            dl(url, MP_PATH)
            break
        except (httpx.HTTPError, OSError) as e:  # medlineplus.gov sometimes fails DNS; www. serves the same file
            log.warning("medlineplus download failed from %s: %s", url, type(e).__name__)
    try:
        dl(ORPHA_URL, ORPHA_PATH)
    except (httpx.HTTPError, OSError) as e:
        log.warning("orphanet product1 download failed: %s", type(e).__name__)


# ---------------------------------------------------------------- parsing
def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child(el: ET.Element, name: str) -> ET.Element | None:
    return next((c for c in el if _local(c.tag) == name), None)


def _text(el: ET.Element | None) -> str:
    return " ".join("".join(el.itertext()).split()) if el is not None else ""


def _paragraphs(rec: ET.Element, role: str) -> list[str]:
    """Paragraph texts of the <text> whose <text-role> is role (whitespace collapsed)."""
    for t in rec.iter():
        if _local(t.tag) == "text" and _text(_child(t, "text-role")) == role:
            html = _child(t, "html")
            if html is None:
                return []
            paras = [_text(p) for p in html.iter() if _local(p.tag) == "p"]
            return [p for p in paras if p] or ([_text(html)] if _text(html) else [])
    return []


def parse_medlineplus(path: Path | str = MP_PATH) -> tuple[list[dict], list[dict]]:
    """([condition], [gene]) records from the MedlinePlus Genetics bulk XML."""
    root = ET.parse(path).getroot()
    conds, genes = [], []
    for rec in root:
        kind = _local(rec.tag)
        if kind == "health-condition-summary":
            keys: dict[str, list[str]] = {}
            for k in rec.iter():
                if _local(k.tag) == "db-key":
                    keys.setdefault(_text(_child(k, "db")), []).append(_text(_child(k, "key")))
            conds.append({"name": _text(_child(rec, "name")), "url": _text(_child(rec, "ghr-page")),
                          "paragraphs": _paragraphs(rec, "description"),
                          "genes": [_text(_child(g, "gene-symbol")) for g in rec.iter()
                                    if _local(g.tag) == "related-gene" and _text(_child(g, "gene-symbol"))],
                          "synonyms": [_text(s) for s in rec.iter() if _local(s.tag) == "synonym"],
                          "omim": [f"OMIM:{k}" for k in keys.get("OMIM", []) if k.isdigit()]})
        elif kind == "gene-summary":
            genes.append({"symbol": _text(_child(rec, "gene-symbol")), "url": _text(_child(rec, "ghr-page")),
                          "paragraphs": _paragraphs(rec, "function")})
    return conds, genes


def parse_orphanet_definitions(path: Path | str = ORPHA_PATH) -> dict[str, dict]:
    """ORPHA:<code> -> {name, definition, url} from en_product1.xml (Definition text sections only)."""
    out: dict[str, dict] = {}
    for _, el in ET.iterparse(path, events=("end",)):
        if el.tag != "Disorder":
            continue
        code = (el.findtext("OrphaCode") or "").strip()
        definition = ""
        for ts in el.iter("TextSection"):
            if (ts.findtext("TextSectionType/Name") or "").strip().lower() == "definition":
                # Orphadata double-escapes some entities ('&amp;nbsp;'): unescape once more, then collapse spaces
                definition = " ".join(html.unescape(ts.findtext("Contents") or "").split())
                break
        if code and definition:
            out[f"ORPHA:{code}"] = {"name": " ".join(html.unescape(el.findtext("Name") or "").split()),
                                    "definition": definition,
                                    "url": f"https://www.orpha.net/en/disease/detail/{code}"}
        el.clear()
    return out


# ---------------------------------------------------------------- text helpers
def sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENT.split(" ".join((text or "").split())) if s.strip()]


def lead_sentences(text: str, n: int = 2, max_chars: int = MAX_CHARS) -> str:
    """The first 1..n sentences of text, verbatim, within max_chars (always at least the first sentence)."""
    ss = sentences(text)
    out = ss[:1]
    for s in ss[1:n]:
        if len(" ".join(out + [s])) > max_chars:
            break
        out.append(s)
    return " ".join(out)


def gene_sentence(paragraphs: list[str], symbol: str) -> str | None:
    """The first description sentence about the gene ('... the CLN5 gene ...'), else the first naming the symbol as
    a whole word (it may be the disease name, 'CLN5 disease'), verbatim; None if no sentence names it."""
    ss = [s for p in paragraphs for s in sentences(p)]
    for rx in (re.compile(r"(?<![\w-])" + re.escape(symbol) + r" genes?\b"),
               re.compile(r"(?<![\w-])" + re.escape(symbol) + r"(?![\w-])")):
        hit = next((s for s in ss if rx.search(s)), None)
        if hit:
            return hit
    return None


# ---------------------------------------------------------------- mapping
def name_index(mondo) -> dict[str, set[str]]:
    """norm_name(label or EXACT synonym) -> live MONDO ids."""
    idx: dict[str, set[str]] = {}
    live = mondo[~mondo["obsolete"]]
    exact = live["exact_synonyms"] if "exact_synonyms" in live.columns else [[] for _ in range(len(live))]
    for mid, name, syns in zip(live["id"], live["name"], exact):
        for n in [name, *list(syns)]:
            if n:
                idx.setdefault(norm_name(n), set()).add(mid)
    return idx


def map_conditions(conds: list[dict], names: dict[str, set[str]], x2m: dict[str, str],
                   diseases: set[str]) -> tuple[dict[str, tuple[dict, str]], dict[str, dict]]:
    """(exact {disease: (condition, 'name'|'omim')}, listed {disease: group condition}). Name matches win over
    OMIM matches; ties go to the condition whose name sorts first (deterministic)."""
    exact: dict[str, tuple[dict, str]] = {}
    listed: dict[str, dict] = {}
    for c in sorted(conds, key=lambda c: c["name"].lower()):
        if not c["paragraphs"]:
            continue
        named = names.get(norm_name(c["name"]), set()) & diseases
        by_omim = {x2m[k] for k in c["omim"] if k in x2m} & diseases
        if len(named) == 1:
            d = next(iter(named))
            if d not in exact or exact[d][1] != "name":
                exact[d] = (c, "name")
        if len(by_omim) == 1 and not named:
            d = next(iter(by_omim))
            exact.setdefault(d, (c, "omim"))
        elif len(by_omim) > 1:
            for d in sorted(by_omim - named):
                listed.setdefault(d, c)
    return exact, listed


def _source(name: str, ref: str, url: str, quote: str, match: str, status: str, lic: str, ra: str | None) -> dict:
    return {"source_type": "database", "source_name": name, "source_ref": ref, "url": url, "quote": quote,
            "match": match, "status": status, "license": lic, "retrieved_at": ra}


def _slug(url: str) -> str:
    return url.rstrip("/").rsplit("/", 1)[-1]


def build(sl: dict, mondo, conds: list[dict], genes: list[dict], orpha: dict[str, dict],
          g: GraphWriter, mp_ra: str | None = None, orpha_ra: str | None = None) -> list[dict]:
    """Writes MedlinePlus gene edges into g and returns the node patches (see module docstring)."""
    diseases = set(sl["diseases"])
    x2m: dict[str, str] = sl["xref_to_mondo"]
    m = mondo.set_index("id")
    exact, listed = map_conditions(conds, name_index(mondo), x2m, diseases)
    orpha_by_d: dict[str, tuple[str, dict]] = {}
    for code in sorted(orpha):
        d = x2m.get(code)
        if d in diseases:
            orpha_by_d.setdefault(d, (code, orpha[code]))

    def parents(d: str) -> list[str]:
        return [p for p in (m.loc[d]["parents"] if d in m.index else []) if p in m.index]

    def nearest_exact(d: str) -> str | None:
        level, seen = [d], {d}
        for _ in range(PARENT_DEPTH):
            nxt = sorted({p for x in level for p in parents(x)} - seen)
            hit = next((p for p in nxt if p in exact), None)
            if hit:
                return hit
            seen |= set(nxt)
            level = nxt
        return None

    patches: list[dict] = []
    counts = {"medlineplus": 0, "orphanet": 0, "listed": 0, "parent": 0, "mondo_definition": 0, "none": 0}
    for d in sorted(diseases):
        if d not in m.index:
            continue
        if d in exact:
            c, how = exact[d]
            q = lead_sentences(c["paragraphs"][0])
            src = _source(MP_NAME, f"MedlinePlus:{_slug(c['url'])}", c["url"], q, how, "curated", MP_LICENSE, mp_ra)
            patches.append({"id": d, "plain_summary": q, "attrs": {"plain_summary_source": src}})
            counts["medlineplus"] += 1
            continue
        if d in orpha_by_d:
            code, o = orpha_by_d[d]
            q = lead_sentences(o["definition"])
            src = _source("Orphanet", code, o["url"], q, "orpha", "curated", ORPHA_LICENSE, orpha_ra)
            patches.append({"id": d, "plain_summary": q, "attrs": {"plain_summary_source": src}})
            counts["orphanet"] += 1
            continue
        mdef = m.loc[d]["def"]
        if isinstance(mdef, str) and mdef.strip() and not GENERATED_DEF.search(mdef):
            counts["mondo_definition"] += 1   # views use the MONDO description as it is
            continue
        if d in listed:
            c, status, match, lead = listed[d], "curated", "listed", listed[d]["name"]
        else:
            p = nearest_exact(d)
            if p is None:
                counts["none"] += 1
                continue
            c, status, match, lead = exact[p][0], "inferred", "parent", str(m.loc[p]["name"])
        q = lead_sentences(c["paragraphs"][0])
        text = f"A form of {lead}. {q}"
        src = _source(MP_NAME, f"MedlinePlus:{_slug(c['url'])}", c["url"], q, match, status, MP_LICENSE, mp_ra)
        patches.append({"id": d, "plain_summary": text, "attrs": {"plain_summary_source": src}})
        counts[match] += 1

    n_edges = 0
    edge_genes: set[str] = set()
    for d, (c, how) in sorted(exact.items()):
        for sym in dict.fromkeys(c["genes"]):
            rec = hgnc.resolve(sym)
            if rec is None:
                continue
            gid = hgnc.gene_node(g, rec)
            edge_genes.add(gid)
            quote = gene_sentence(c["paragraphs"], sym) or f"{c['name']} | related gene: {sym}"
            ev = dict(source_type="database", source_name=MP_NAME, source_ref=f"MedlinePlus:{_slug(c['url'])}",
                      url=c["url"], quote=quote, method="curated")
            g.edge("gene_associated_with_disease", gid, d, status="curated", label="associated with",
                   evidence=ev | ({"retrieved_at": mp_ra} if mp_ra else {}))
            n_edges += 1

    slice_genes = set(sl.get("genes") or []) | edge_genes
    n_gene_summaries = 0
    for gr in genes:
        rec = hgnc.resolve(gr["symbol"]) if gr["symbol"] else None
        if rec is None or rec["hgnc_id"] not in slice_genes or not gr["paragraphs"]:
            continue
        q = lead_sentences(gr["paragraphs"][0])
        src = _source(MP_NAME, f"MedlinePlus:gene/{_slug(gr['url'])}", gr["url"], q, "symbol", "curated",
                      MP_LICENSE, mp_ra)
        patches.append({"id": rec["hgnc_id"], "plain_summary": q, "attrs": {"plain_summary_source": src}})
        n_gene_summaries += 1
    log.info("medlineplus: disease summaries %s; %d gene summaries; %d gene links", counts, n_gene_summaries, n_edges)
    return patches


def emit() -> None:
    g = GraphWriter("medlineplus")
    sl = read_json("slice")
    conds, genes = parse_medlineplus(MP_PATH) if MP_PATH.exists() else ([], [])
    orpha = parse_orphanet_definitions(ORPHA_PATH) if ORPHA_PATH.exists() else {}
    if not conds and not orpha:
        log.warning("medlineplus: no MedlinePlus or Orphanet summaries downloaded; stage writes nothing")
    patches = build(sl, read_parquet("mondo_terms"), conds, genes, orpha, g,
                    retrieved_at(MP_PATH), retrieved_at(ORPHA_PATH))
    write_node_patches("medlineplus", patches)
    g.close()
