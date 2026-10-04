"""Build the disease slice from config/slice.yaml.

slice = descendants(roots) ∪ descendants(focus_roots) ∪ extra_diseases
        ∪ gene-hop: diseases (via HPO genes_to_disease, mapped to MONDO) of
          seed_genes ∪ genes of focus diseases        (capped at expansion.max_extra_diseases)
roots / focus_roots / seed_genes are the top-level lists plus those of every `families` entry, so several disease
families (lysosomal, mitochondrial, epileptic encephalopathies, ...) live in one slice. Every focus disease gets
its own action view and paths, whatever its family; similarity and clusters run across all families.
Writes data/interim/slice.json:
  diseases, focus, root_descendants, expanded, genes (HGNC ids, incl. MONDO material-basis genes of slice
  diseases), xref_to_mondo (OMIM/ORPHA -> MONDO), families [{key, label, roots, focus}], family_of {disease: key}
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
    mondo_all = read_parquet("mondo_terms")
    mondo = mondo_all[~mondo_all["obsolete"]]
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
    x2m = xref_map(mondo_all)   # needs the obsolete terms for the replaced_by tier

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
    if "material_basis_genes" in mondo.columns:   # MONDO's own curated disease -> gene links (e.g. Dravet -> SCN1A)
        mb = mondo[mondo["id"].isin(diseases)]
        genes |= {h for hs in mb["material_basis_genes"] for h in hs if hgnc.resolve(h)}
    fams, family_of = families(cfg, children, diseases)
    out = {
        "version": cfg.version, "name": cfg.name,
        "diseases": sorted(diseases), "focus": sorted(focus), "root_descendants": sorted(root_desc),
        "expanded": sorted(expanded), "genes": sorted(genes), "seed_genes": sorted(seed),
        "xref_to_mondo": x2m, "families": fams, "family_of": family_of,
    }
    write_json("slice", out)
    log.info("slice: %d diseases (%d focus, %d gene-hop), %d genes; families: %s", len(diseases), len(focus),
             len(expanded), len(genes), ", ".join(f"{f['key']} {f['size']}/{len(f['focus'])} focus" for f in fams))
    return out


def families(cfg, children: dict[str, list[str]], diseases: set[str]) -> tuple[list[dict], dict[str, str]]:
    """Per family: its slice members (descendants of its roots and focus roots) and focus diseases; family_of maps
    each member to one family key: the family whose focus covers it, else the first family (config order) whose
    roots do. Gene-hop diseases outside every family root are left out (views fall back to a neutral label)."""
    out, family_of, focus_of = [], {}, {}
    for f in cfg.families:
        members = (descendants(children, f.roots) | descendants(children, f.focus_roots)) & diseases
        focus = descendants(children, f.focus_roots) & diseases
        for d in sorted(focus):
            focus_of.setdefault(d, f.key)
        for d in sorted(members):
            family_of.setdefault(d, f.key)
        out.append({"key": f.key, "label": f.label, "roots": f.roots, "focus": sorted(focus), "size": len(members)})
    family_of.update(focus_of)
    return out, dict(sorted(family_of.items()))
