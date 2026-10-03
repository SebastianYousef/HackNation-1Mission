"""Reactome pathways (gene -> pathway).

Source : https://reactome.org/download/current/NCBI2Reactome_All_Levels.txt
         cols: NCBI gene id, pathway stable id, url, pathway name, evidence code, species
         https://reactome.org/download/current/ReactomePathways.txt (id, name, species) — names
Mapping: NCBIGene -> HGNC (hgnc table); pathway -> mechanism node REACT:<stId> subtype 'pathway',
         attrs.n_genes (human genes in the pathway, all levels). Only Homo sapiens rows.
Emits  : gene_in_mechanism (curated, source Reactome) for slice genes, for pathways with
         n_genes <= 4 * analytics.max_pathway_genes (top-level pathways like "Metabolism" are dropped).
         disease_involves_mechanism is derived later (analytics.mechanisms).
"""
from __future__ import annotations

import logging

import pandas as pd

from ..config import RAW, slice_config
from ..http import download as dl, retrieved_at
from ..store import GraphWriter, read_json, write_parquet
from . import hgnc

log = logging.getLogger(__name__)
URLS = {"NCBI2Reactome_All_Levels.txt": "https://reactome.org/download/current/NCBI2Reactome_All_Levels.txt",
        "ReactomePathways.txt": "https://reactome.org/download/current/ReactomePathways.txt"}


def download() -> None:
    for name, url in URLS.items():
        dl(url, RAW / "reactome" / name)


def emit() -> None:
    path = RAW / "reactome" / "NCBI2Reactome_All_Levels.txt"
    df = pd.read_csv(path, sep="\t", header=None, dtype=str,
                     names=["gene", "pathway", "url", "name", "evidence", "species"])
    df = df[df["species"] == "Homo sapiens"].drop_duplicates(["gene", "pathway"])
    size = df.groupby("pathway")["gene"].nunique()
    sl = read_json("slice")
    entrez = {}
    for hid in sl["genes"]:
        rec = hgnc.resolve(hid)
        if rec and rec["entrez_id"]:
            entrez[rec["entrez_id"]] = rec
    maxg = 4 * int(slice_config().analytics.get("max_pathway_genes", 200))
    sub = df[df["gene"].isin(entrez) & df["pathway"].map(size).le(maxg)]
    write_parquet("reactome_pathway_sizes", size.rename("n_genes").reset_index())
    ra = retrieved_at(path)
    g = GraphWriter("reactome")
    for r in sub.itertuples(index=False):
        rec = entrez[r.gene]
        gid = hgnc.gene_node(g, rec)
        mid = f"REACT:{r.pathway}"
        g.node(id=mid, type="mechanism", subtype="pathway", label=r.name.strip(), url=r.url,
               xrefs={"REACT": [r.pathway]}, attrs={"n_genes": int(size[r.pathway]), "source": "Reactome"})
        g.edge("gene_in_mechanism", gid, mid, status="curated", label="acts in",
               evidence=dict(source_type="database", source_name="Reactome", source_ref=r.pathway, url=r.url,
                             quote=f"{r.gene}\t{r.pathway}\t{r.name.strip()}\t{r.evidence}\tHomo sapiens",
                             method="curated", retrieved_at=ra))
    g.close()
