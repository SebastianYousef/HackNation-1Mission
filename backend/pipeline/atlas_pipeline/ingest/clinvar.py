"""ClinVar variants for slice genes (PARTIAL: opt-in because the file is ~450 MB).

Source : https://ftp.ncbi.nlm.nih.gov/pub/clinvar/tab_delimited/variant_summary.txt.gz
Fields : VariationID -> CLINVAR:<id> (variant node), Name -> label, GeneSymbol -> variant_of_gene,
         ClinicalSignificance (Pathogenic / Likely pathogenic only) -> attrs.clinical_significance,
         ReviewStatus -> attrs.review_status, PhenotypeIDS (MONDO:/OMIM:/Orphanet:) -> variant_associated_with_disease,
         Assembly == GRCh38 rows only. Max `ATLAS_CLINVAR_PER_GENE` (default 30) variants per gene,
         best review status first.
Enable with ATLAS_CLINVAR=1 (otherwise emit() writes an empty stage and logs a note).
"""
from __future__ import annotations

import logging
import os
import re

import pandas as pd

from ..config import RAW
from ..http import download as dl, retrieved_at
from ..ids import normalize_curie
from ..store import GraphWriter, read_json
from . import hgnc

log = logging.getLogger(__name__)
URL = "https://ftp.ncbi.nlm.nih.gov/pub/clinvar/tab_delimited/variant_summary.txt.gz"
PATH = RAW / "clinvar" / "variant_summary.txt.gz"
REVIEW_RANK = {"practice guideline": 0, "reviewed by expert panel": 1,
               "criteria provided, multiple submitters, no conflicts": 2, "criteria provided, single submitter": 3}


def enabled() -> bool:
    return os.environ.get("ATLAS_CLINVAR") == "1"


def download() -> None:
    if enabled():
        dl(URL, PATH)


def emit() -> None:
    g = GraphWriter("clinvar")
    if not enabled() or not PATH.exists():
        log.info("clinvar skipped (set ATLAS_CLINVAR=1 to download %s)", URL)
        g.close()
        return
    sl = read_json("slice")
    x2m, diseases = sl["xref_to_mondo"], set(sl["diseases"])
    symbols = {hgnc.resolve(h)["symbol"]: hgnc.resolve(h) for h in sl["genes"] if hgnc.resolve(h)}
    per_gene = int(os.environ.get("ATLAS_CLINVAR_PER_GENE", "30"))
    cols = ["VariationID", "Name", "GeneSymbol", "ClinicalSignificance", "ReviewStatus", "PhenotypeIDS",
            "PhenotypeList", "Assembly", "Type"]
    chunks = []
    for ch in pd.read_csv(PATH, sep="\t", usecols=cols, dtype=str, chunksize=200_000):
        ch = ch[(ch["Assembly"] == "GRCh38") & ch["GeneSymbol"].isin(symbols)
                & ch["ClinicalSignificance"].str.contains("athogenic", na=False)
                & ~ch["ClinicalSignificance"].str.contains("onflicting", na=False)]
        chunks.append(ch)
    df = pd.concat(chunks).fillna("")
    df["rank"] = df["ReviewStatus"].map(REVIEW_RANK).fillna(9)
    df = df.sort_values("rank").groupby("GeneSymbol").head(per_gene)
    ra = retrieved_at(PATH)
    for r in df.itertuples(index=False):
        rec = symbols[r.GeneSymbol]
        gid = hgnc.gene_node(g, rec)
        vid = f"CLINVAR:{r.VariationID}"
        url = f"https://www.ncbi.nlm.nih.gov/clinvar/variation/{r.VariationID}/"
        g.node(id=vid, type="variant", label=r.Name[:200], url=url,
               attrs={"clinical_significance": r.ClinicalSignificance, "review_status": r.ReviewStatus,
                      "variant_type": r.Type})
        ev = dict(source_type="database", source_name="ClinVar", source_ref=vid, url=url,
                  quote=f"{r.Name} | {r.ClinicalSignificance} | {r.ReviewStatus} | {r.PhenotypeList}"[:500],
                  retrieved_at=ra)
        g.edge("variant_of_gene", vid, gid, status="curated", label="is a variant of", evidence=ev)
        for x in re.split(r"[,|;]", r.PhenotypeIDS):
            x = normalize_curie(x.strip())
            m = x if x and x.startswith("MONDO:") else x2m.get(x or "")
            if m in diseases:
                g.edge("variant_associated_with_disease", vid, m, status="curated", label="causes",
                       attrs={"clinical_significance": r.ClinicalSignificance}, evidence=ev)
    g.close()
