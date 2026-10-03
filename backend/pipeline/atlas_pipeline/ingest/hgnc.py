"""HGNC gene nomenclature.

Source : https://storage.googleapis.com/public-download-files/hgnc/tsv/tsv/hgnc_complete_set.txt
Fields : hgnc_id -> nodes.id (HGNC:nnnn), symbol -> label, name -> description,
         alias_symbol + prev_symbol + alias_name -> synonyms, entrez_id -> xrefs.NCBIGene,
         ensembl_gene_id -> xrefs.ENSEMBL, uniprot_ids -> xrefs.UniProtKB, omim_id -> xrefs.OMIM,
         locus_type -> attrs.locus_type.  Emits gene nodes for every gene in the slice.
"""
from __future__ import annotations

import logging
from functools import lru_cache

import pandas as pd

from ..config import RAW
from ..http import download as dl
from ..store import GraphWriter, read_json, read_parquet, write_parquet

log = logging.getLogger(__name__)
URL = "https://storage.googleapis.com/public-download-files/hgnc/tsv/tsv/hgnc_complete_set.txt"
PATH = RAW / "hgnc" / "hgnc_complete_set.txt"


def download() -> None:
    dl(URL, PATH)


def _split(v) -> list[str]:
    if not isinstance(v, str) or not v:
        return []
    return [x.strip().strip('"') for x in v.split("|") if x.strip()]


def parse() -> pd.DataFrame:
    df = pd.read_csv(PATH, sep="\t", dtype=str, low_memory=False)
    df = df[df["status"] == "Approved"]
    out = pd.DataFrame({
        "hgnc_id": df["hgnc_id"], "symbol": df["symbol"], "name": df["name"],
        "locus_type": df["locus_type"], "entrez_id": df["entrez_id"].fillna(""),
        "ensembl": df["ensembl_gene_id"].fillna(""), "uniprot": df["uniprot_ids"].fillna(""),
        "omim": df["omim_id"].fillna(""),
        "synonyms": [_split(a) + _split(p) + _split(n) for a, p, n in
                     zip(df["alias_symbol"], df["prev_symbol"], df["alias_name"])],
    })
    write_parquet("hgnc_genes", out)
    lookup.cache_clear()
    log.info("hgnc: %d genes", len(out))
    return out


@lru_cache
def lookup() -> dict[str, dict]:
    """symbol / NCBIGene:<id> / HGNC:<id> -> gene record."""
    df = read_parquet("hgnc_genes")
    m: dict[str, dict] = {}
    for r in df.to_dict("records"):
        m[r["hgnc_id"]] = r
        m[r["symbol"].upper()] = r
        if r["entrez_id"]:
            m[f"NCBIGene:{r['entrez_id']}"] = r
    return m


def resolve(key: str) -> dict | None:
    lk = lookup()
    return lk.get(key) or lk.get(key.upper())


def gene_node(g: GraphWriter, rec: dict) -> str:
    xrefs = {}
    for k, col in (("NCBIGene", "entrez_id"), ("ENSEMBL", "ensembl"), ("UniProtKB", "uniprot"), ("OMIM", "omim")):
        vals = [v for v in str(rec[col]).split("|") if v]
        if vals:
            xrefs[k] = vals
    return g.node(id=rec["hgnc_id"], type="gene", label=rec["symbol"], description=rec["name"],
                  synonyms=sorted(set(list(rec["synonyms"]) + [rec["name"]])), xrefs=xrefs,
                  attrs={"locus_type": rec["locus_type"], "symbol": rec["symbol"]},
                  url=f"https://www.genenames.org/data/gene-symbol-report/#!/hgnc_id/{rec['hgnc_id']}")


def emit() -> None:
    sl = read_json("slice")
    g = GraphWriter("hgnc")
    for hid in sl["genes"]:
        rec = resolve(hid)
        if rec:
            gene_node(g, rec)
    g.close()
