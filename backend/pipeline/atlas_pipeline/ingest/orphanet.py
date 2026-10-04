"""Orphanet / Orphadata (CC-BY-4.0).

Implemented:
  https://www.orphadata.com/data/xml/en_product6.xml   genes associated with rare diseases
    Disorder/OrphaCode -> ORPHA:<code> -> MONDO (equivalentTo xref)
    DisorderGeneAssociation/Gene/Symbol (+ ExternalReference HGNC) -> gene node
    DisorderGeneAssociationType/Name -> attrs.orphanet_association ("Disease-causing germline mutation(s) in")
    DisorderGeneAssociationStatus/Name -> attrs.orphanet_status ("Assessed")
    SourceOfValidation "12345[PMID]_..." -> evidence rows with source_ref PMID:12345
    Status: "Assessed" -> curated; "Not yet assessed" -> literature if Orphanet cites PMIDs, else hypothesis
            (evidence method algorithm:orphanet_not_yet_assessed instead of curated).
    "Candidate gene tested in" / "Biomarker tested in" rows assert no association and are skipped.
  https://www.orphadata.com/data/xml/en_product9_prev.xml  epidemiology per disorder
    Disorder/OrphaCode -> ORPHA:<code> -> MONDO (same xref map) -> node patch attrs.prevalence: a list of
    {type, class, qualification, mean, geographic, validation, source, pmids} (absent fields omitted), one per
    Prevalence record; validated records first, then worldwide ones. `mean` is Orphanet's ValMoy as given
    (0 / empty = not stated, so omitted); read it together with `qualification` (a value, or a case/family count).
    This is a node attribute, not an edge, so it carries no edge status or evidence rows.
Not needed / stubbed:
  https://www.orphadata.com/data/xml/en_product4.xml   HPO phenotypes per disorder — already contained in
      phenotype.hpoa (ORPHA:* rows), so we do not parse it twice.
"""
from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET

from ..config import RAW
from ..http import download as dl, retrieved_at
from ..store import GraphWriter, read_json, write_node_patches
from . import hgnc

log = logging.getLogger(__name__)
PREVALENCE_URL = "https://www.orphadata.com/data/xml/en_product9_prev.xml"
URLS = {"en_product6.xml": "https://www.orphadata.com/data/xml/en_product6.xml",
        "en_product9_prev.xml": PREVALENCE_URL}


def download() -> None:
    for name, url in URLS.items():
        dl(url, RAW / "orphanet" / name)


def emit() -> None:
    sl = read_json("slice")
    x2m, diseases = sl["xref_to_mondo"], set(sl["diseases"])
    path = RAW / "orphanet" / "en_product6.xml"
    ra = retrieved_at(path)
    g = GraphWriter("orphanet")
    for _, el in ET.iterparse(path, events=("end",)):
        if el.tag != "Disorder":
            continue
        code = el.findtext("OrphaCode")
        mondo = x2m.get(f"ORPHA:{code}")
        if mondo in diseases:
            dname = el.findtext("Name")
            for a in el.iter("DisorderGeneAssociation"):
                sym = a.findtext("Gene/Symbol")
                hg = None
                for ref in a.iter("ExternalReference"):
                    if ref.findtext("Source") == "HGNC":
                        hg = f"HGNC:{ref.findtext('Reference')}"
                rec = (hg and hgnc.resolve(hg)) or (sym and hgnc.resolve(sym))
                if not rec:
                    continue
                gid = hgnc.gene_node(g, rec)
                atype = a.findtext("DisorderGeneAssociationType/Name") or ""
                status = a.findtext("DisorderGeneAssociationStatus/Name") or ""
                if atype.lower().startswith(("candidate gene tested in", "biomarker tested in")):
                    continue
                pmids = re.findall(r"(\d+)\[PMID\]", a.findtext("SourceOfValidation") or "")
                quote = f"{dname} (ORPHA:{code}) — {atype} {sym} [{status}]"
                evs = [dict(source_type="database", source_name="Orphanet", source_ref=f"ORPHA:{code}",
                            url=f"https://www.orpha.net/en/disease/detail/{code}", quote=quote, retrieved_at=ra)]
                evs += [dict(source_type="database", source_name="Orphanet", source_ref=f"PMID:{p}",
                             url=f"https://pubmed.ncbi.nlm.nih.gov/{p}/", quote=quote, retrieved_at=ra) for p in pmids[:3]]
                causal = "causing" in atype.lower()
                edge_status = ("curated" if status.lower() != "not yet assessed"
                               else "literature" if pmids else "hypothesis")
                if edge_status != "curated":  # the quote is Orphanet's line, not a reviewed assertion
                    evs = [dict(e, method="algorithm:orphanet_not_yet_assessed") for e in evs]
                g.edge("gene_associated_with_disease", gid, mondo, status=edge_status,
                       label="causes" if causal else "associated with",
                       attrs={"orphanet_association": atype, "orphanet_status": status}, evidence=evs)
        el.clear()
    prev_path = RAW / "orphanet" / "en_product9_prev.xml"
    prev = parse_prevalence(prev_path, x2m, diseases) if prev_path.exists() else {}
    write_node_patches("orphanet", ({"id": m, "attrs": {"prevalence": recs}} for m, recs in sorted(prev.items())))
    log.info("[orphanet] prevalence for %d diseases", len(prev))
    g.close()


def _prevalence_record(p: ET.Element) -> dict:
    mean = None
    try:
        mean = float(p.findtext("ValMoy") or "") or None  # 0.0 means "no mean value given"
    except ValueError:
        pass
    source = (p.findtext("Source") or "").strip()
    rec = {"type": p.findtext("PrevalenceType/Name"), "class": p.findtext("PrevalenceClass/Name"),
           "qualification": p.findtext("PrevalenceQualification/Name"), "mean": mean,
           "geographic": p.findtext("PrevalenceGeographic/Name"),
           "validation": p.findtext("PrevalenceValidationStatus/Name"),
           "source": source[:300] or None,
           "pmids": [f"PMID:{x}" for x in dict.fromkeys(re.findall(r"(\d+)\[PMID\]", source))]}
    return {k: v for k, v in rec.items() if v not in (None, "", [])}


def _prevalence_rank(r: dict) -> tuple[bool, bool]:
    return (r.get("validation", "").lower() != "validated", r.get("geographic", "").lower() != "worldwide")


def parse_prevalence(path, x2m: dict[str, str], diseases: set[str]) -> dict[str, list[dict]]:
    """en_product9_prev.xml -> {mondo_id: [prevalence record, ...]} for slice diseases (see module doc)."""
    out: dict[str, list[dict]] = {}
    for _, el in ET.iterparse(path, events=("end",)):
        if el.tag != "Disorder":
            continue
        mondo = x2m.get(f"ORPHA:{el.findtext('OrphaCode')}")
        if mondo in diseases:
            recs = out.setdefault(mondo, [])
            for p in el.iter("Prevalence"):
                r = _prevalence_record(p)
                if r and r not in recs:  # several ORPHA codes can map to one MONDO id
                    recs.append(r)
        el.clear()
    for recs in out.values():
        recs.sort(key=_prevalence_rank)  # stable: Orphanet's order within each rank
    return {m: recs for m, recs in out.items() if recs}
