"""MONDO disease ontology.

Source : http://purl.obolibrary.org/obo/mondo.obo  (OBO; ~53 MB)
Fields : id -> nodes.id (MONDO:nnnnnnn), name -> label, def -> description,
         synonym EXACT/RELATED -> synonyms (ABBREVIATION-typed -> attrs.abbreviations),
         xref (OMIM, Orphanet->ORPHA, DOID, UMLS, MESH, GARD, ICD10CM, NCIT, MEDGEN …) -> xrefs,
         xref {source="MONDO:equivalentTo"} -> 1:1 OMIM/ORPHA -> MONDO mapping (reconcile tier 1),
         is_a -> disease_subtype_of (child -> parent, status curated, source MONDO),
         subset disease_grouping -> attrs.grouping=true, subset rare -> attrs.rare=true.
"""
from __future__ import annotations

import logging

import pandas as pd

from ..config import RAW
from ..http import download as dl, retrieved_at
from ..ids import normalize_curie, prefix
from ..obo import parse_obo
from ..store import GraphWriter, read_json, read_parquet, write_parquet

log = logging.getLogger(__name__)
URL = "http://purl.obolibrary.org/obo/mondo.obo"
PATH = RAW / "mondo" / "mondo.obo"


def download() -> None:
    dl(URL, PATH)


def parse() -> pd.DataFrame:
    rows = []
    for t in parse_obo(PATH):
        if not t["id"] or not t["id"].startswith("MONDO:"):
            continue
        rows.append({
            "id": t["id"], "name": t["name"] or t["id"], "def": t["def"], "obsolete": t["obsolete"],
            "parents": [p for p in t["is_a"] if p.startswith("MONDO:")],
            "synonyms": [s["name"] for s in t["synonyms"] if s["scope"] in ("EXACT", "RELATED") and s["type"] != "ABBREVIATION"],
            "abbreviations": [s["name"] for s in t["synonyms"] if s["type"] == "ABBREVIATION"],
            "xrefs": [normalize_curie(x["id"]) for x in t["xrefs"]],
            "equiv_xrefs": [normalize_curie(x["id"]) for x in t["xrefs"] if x["equivalent"]],
            "subsets": t["subsets"],
        })
    df = pd.DataFrame(rows)
    write_parquet("mondo_terms", df)
    log.info("mondo: %d terms", len(df))
    return df


def xref_map(df: pd.DataFrame) -> dict[str, str]:
    """OMIM:/ORPHA:/DOID:… -> MONDO id, from MONDO:equivalentTo xrefs (1:1 only)."""
    m: dict[str, set[str]] = {}
    for mid, xs, obs in zip(df["id"], df["equiv_xrefs"], df["obsolete"]):
        if obs:
            continue
        for x in xs:
            m.setdefault(x, set()).add(mid)
    return {k: next(iter(v)) for k, v in m.items() if len(v) == 1}


def emit() -> None:
    df = read_parquet("mondo_terms").set_index("id")
    sl = read_json("slice")
    diseases = set(sl["diseases"])
    ra = retrieved_at(PATH)
    g = GraphWriter("mondo")
    for mid in diseases:
        r = df.loc[mid]
        xrefs: dict[str, list[str]] = {}
        for x in r["xrefs"]:
            if x and ":" in x and prefix(x) not in ("MONDO",):
                xrefs.setdefault(prefix(x), []).append(x.split(":", 1)[1])
        subsets = list(r["subsets"])
        attrs = {"grouping": "disease_grouping" in subsets, "rare": "rare" in subsets,
                 "abbreviations": list(r["abbreviations"]), "in_focus": mid in sl["focus"],
                 "slice_reason": "focus" if mid in sl["focus"] else ("root" if mid in sl["root_descendants"] else "gene_hop")}
        g.node(id=mid, type="disease", label=r["name"], description=r["def"], synonyms=sorted(set(r["synonyms"])),
               xrefs={k: sorted(set(v)) for k, v in xrefs.items()}, attrs=attrs,
               url=f"https://monarchinitiative.org/{mid}")
    for mid in diseases:
        for p in df.loc[mid]["parents"]:
            if p in diseases:
                g.edge("disease_subtype_of", mid, p, status="curated", label="is a type of",
                       evidence=dict(source_type="database", source_name="MONDO", source_ref=mid,
                                     url=f"http://purl.obolibrary.org/obo/{mid.replace(':', '_')}",
                                     quote=f"{mid} is_a {p} ! {df.loc[p]['name']}", method="curated",
                                     retrieved_at=ra))
    g.close()
