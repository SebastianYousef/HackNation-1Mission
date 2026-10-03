"""Build the disease slice from config/slice.yaml.

slice = descendants(roots) ∪ descendants(focus_roots) ∪ extra_diseases
        ∪ gene-hop: diseases (via HPO genes_to_disease, mapped to MONDO) of
          seed_genes ∪ genes of focus diseases        (capped at expansion.max_extra_diseases)
Writes data/interim/slice.json:
  diseases, focus, root_descendants, expanded, genes (HGNC ids), xref_to_mondo (OMIM/ORPHA -> MONDO)
"""
from __future__ import annotations

import logging

import pandas as pd

from .config import RAW, slice_config
from .ingest import hgnc
from .ingest.mondo import xref_map
from .obo import descendants
from .store import read_parquet, write_json

log = logging.getLogger(__name__)


def build() -> dict:
    cfg = slice_config()
    mondo = read_parquet("mondo_terms")
    mondo = mondo[~mondo["obsolete"]]
    children: dict[str, list[str]] = {}
    for mid, parents in zip(mondo["id"], mondo["parents"]):
        for p in parents:
            children.setdefault(p, []).append(mid)
    known = set(mondo["id"])
    for r in cfg.roots + cfg.focus_roots:
        if r not in known:
            raise SystemExit(f"slice root {r} not found in MONDO")
    root_desc = descendants(children, cfg.roots)
    focus = descendants(children, cfg.focus_roots)
    diseases = root_desc | focus | {d for d in cfg.extra_diseases if d in known}
    x2m = xref_map(mondo)

    g2d = pd.read_csv(RAW / "hpo" / "genes_to_disease.txt", sep="\t", dtype=str).fillna("")
    g2d["mondo"] = g2d["disease_id"].map(x2m)
    g2d = g2d[g2d["mondo"].notna()]
    g2d["hgnc"] = [((hgnc.resolve(n) or hgnc.resolve(s) or {}).get("hgnc_id")) for n, s in zip(g2d["ncbi_gene_id"], g2d["gene_symbol"])]
    g2d = g2d[g2d["hgnc"].notna()]

    seed = {(hgnc.resolve(s) or {}).get("hgnc_id") for s in cfg.seed_genes} - {None}
    missing = [s for s in cfg.seed_genes if not hgnc.resolve(s)]
    if missing:
        log.warning("seed genes not in HGNC: %s", missing)
    hop_genes = seed | set(g2d.loc[g2d["mondo"].isin(focus), "hgnc"])
    expanded: list[str] = []
    if cfg.expansion.get("gene_hop", True):
        cand = g2d.loc[g2d["hgnc"].isin(hop_genes) & ~g2d["mondo"].isin(diseases), "mondo"]
        expanded = list(dict.fromkeys(cand))[: int(cfg.expansion.get("max_extra_diseases", 200))]
        diseases |= set(expanded)
    genes = seed | set(g2d.loc[g2d["mondo"].isin(diseases), "hgnc"])
    out = {
        "version": cfg.version, "name": cfg.name,
        "diseases": sorted(diseases), "focus": sorted(focus), "root_descendants": sorted(root_desc),
        "expanded": sorted(expanded), "genes": sorted(genes), "seed_genes": sorted(seed),
        "xref_to_mondo": x2m,
    }
    write_json("slice", out)
    log.info("slice: %d diseases (%d focus, %d gene-hop), %d genes", len(diseases), len(focus), len(expanded), len(genes))
    return out
