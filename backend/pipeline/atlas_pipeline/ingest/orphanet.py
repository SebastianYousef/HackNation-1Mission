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
Not needed / stubbed:
  https://www.orphadata.com/data/xml/en_product4.xml   HPO phenotypes per disorder — already contained in
      phenotype.hpoa (ORPHA:* rows), so we do not parse it twice.
  https://www.orphadata.com/data/xml/en_product9_prev.xml  prevalence -> disease attrs.prevalence  (TODO)
"""
from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET

from ..config import RAW
from ..http import download as dl, retrieved_at
from ..store import GraphWriter, read_json
from . import hgnc

log = logging.getLogger(__name__)
URLS = {"en_product6.xml": "https://www.orphadata.com/data/xml/en_product6.xml"}
PREVALENCE_URL = "https://www.orphadata.com/data/xml/en_product9_prev.xml"  # TODO parse -> attrs.prevalence


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
    g.close()
