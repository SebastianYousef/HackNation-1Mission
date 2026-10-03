"""Human Phenotype Ontology + annotations.

Sources:
  http://purl.obolibrary.org/obo/hp.obo                      terms, synonyms, is_a hierarchy (for IC)
  http://purl.obolibrary.org/obo/hp/hpoa/phenotype.hpoa      disease -> phenotype annotations
      database_id (OMIM:/ORPHA:/DECIPHER:) -> MONDO via MONDO equivalentTo xrefs
      qualifier NOT -> skipped (negative annotation), hpo_id -> dst, reference -> evidence.source_ref,
      evidence (IEA/PCS/TAS) -> attrs.hpo_evidence, onset -> attrs.onset, frequency -> attrs.frequency
      (+ attrs.frequency_value numeric), aspect P = phenotype, I = inheritance, C = clinical course
      (-> disease attrs: descendants of HP:0003674 Onset -> attrs.onset, other course terms such as
      'Death in infancy' or 'Progressive' -> attrs.clinical_course).
      Frequency 0/N, 0% or HP:0040285 (Excluded) = observed absent -> skipped like NOT.
  http://purl.obolibrary.org/obo/hp/hpoa/genes_to_disease.txt
      ncbi_gene_id -> HGNC via hgnc table, disease_id -> MONDO, association_type -> attrs.association_type
      (MENDELIAN -> label "causes"), source -> evidence.url
  http://purl.obolibrary.org/obo/hp/hpoa/genes_to_phenotype.txt  (not needed: gene->phenotype is
      derivable through diseases; URL kept for completeness)
"""
from __future__ import annotations

import logging
import re

import pandas as pd

from ..config import RAW
from ..http import download as dl, retrieved_at
from ..obo import parse_obo
from ..store import GraphWriter, read_json, read_parquet, write_node_patches, write_parquet
from . import hgnc

log = logging.getLogger(__name__)
BASE = "http://purl.obolibrary.org/obo/hp"
FILES = {"hp.obo": "http://purl.obolibrary.org/obo/hp.obo",
         "phenotype.hpoa": f"{BASE}/hpoa/phenotype.hpoa",
         "genes_to_disease.txt": f"{BASE}/hpoa/genes_to_disease.txt"}
PHENOTYPIC_ABNORMALITY = "HP:0000118"
ONSET = "HP:0003674"

FREQ_TERMS = {  # HPO frequency subontology -> (label, representative value)
    "HP:0040280": ("Obligate (100%)", 1.0), "HP:0040281": ("Very frequent (80-99%)", 0.9),
    "HP:0040282": ("Frequent (30-79%)", 0.55), "HP:0040283": ("Occasional (5-29%)", 0.17),
    "HP:0040284": ("Very rare (1-4%)", 0.025), "HP:0040285": ("Excluded (0%)", 0.0),
}


def download() -> None:
    for name, url in FILES.items():
        dl(url, RAW / "hpo" / name)


def freq_value(f: str | None) -> tuple[str | None, float | None]:
    if not f or not isinstance(f, str):
        return None, None
    if f in FREQ_TERMS:
        return FREQ_TERMS[f]
    m = re.match(r"^(\d+)/(\d+)$", f)
    if m and int(m.group(2)) > 0:
        return f, int(m.group(1)) / int(m.group(2))
    m = re.match(r"^([\d.]+)%$", f)
    if m:
        return f, float(m.group(1)) / 100
    return f, None


def parse_terms() -> pd.DataFrame:
    rows = [{"id": t["id"], "name": t["name"], "def": t["def"], "obsolete": t["obsolete"],
             "parents": [p for p in t["is_a"] if p.startswith("HP:")],
             "synonyms": [s["name"] for s in t["synonyms"] if s["scope"] in ("EXACT", "RELATED")],
             "xrefs": [x["id"] for x in t["xrefs"]]}
            for t in parse_obo(RAW / "hpo" / "hp.obo") if t["id"] and t["id"].startswith("HP:")]
    df = pd.DataFrame(rows)
    write_parquet("hpo_terms", df)
    return df


def parse_hpoa() -> pd.DataFrame:
    df = pd.read_csv(RAW / "hpo" / "phenotype.hpoa", sep="\t", comment="#", dtype=str).fillna("")
    write_parquet("hpoa", df)
    return df


def parse_g2d() -> pd.DataFrame:
    df = pd.read_csv(RAW / "hpo" / "genes_to_disease.txt", sep="\t", dtype=str).fillna("")
    write_parquet("genes_to_disease", df)
    return df


def descendants(terms: pd.DataFrame, root: str) -> set[str]:
    """root and every term below it (is_a), from the `parents` column."""
    children: dict[str, list[str]] = {}
    for tid, parents in terms["parents"].items():
        for p in parents:
            children.setdefault(p, []).append(tid)
    out, todo = {root}, [root]
    while todo:
        for c in children.get(todo.pop(), []):
            if c not in out:
                out.add(c)
                todo.append(c)
    return out


def emit() -> None:
    terms = parse_terms().set_index("id")
    onset_terms = descendants(terms, ONSET)
    hpoa = parse_hpoa()
    g2d = parse_g2d()
    sl = read_json("slice")
    x2m: dict[str, str] = sl["xref_to_mondo"]
    diseases = set(sl["diseases"])
    ra = retrieved_at(RAW / "hpo" / "phenotype.hpoa")
    g = GraphWriter("hpo")

    hpoa = hpoa.assign(mondo=hpoa["database_id"].map(x2m))
    sub = hpoa[hpoa["mondo"].isin(diseases)]
    patches: dict[str, dict] = {}
    for r in sub.itertuples(index=False):
        if r.qualifier == "NOT":
            continue
        if r.aspect in ("I", "C"):
            key = "inheritance" if r.aspect == "I" else "onset" if r.hpo_id in onset_terms else "clinical_course"
            label = terms.loc[r.hpo_id]["name"] if r.hpo_id in terms.index else r.hpo_id
            lst = patches.setdefault(r.mondo, {}).setdefault(key, [])
            if label not in lst:
                lst.append(label)
            continue
        if r.aspect != "P" or r.hpo_id not in terms.index:
            continue
        flabel, fval = freq_value(r.frequency)
        if fval == 0.0:  # 0/N, 0% or Excluded: the feature was looked for and absent (same as NOT)
            continue
        t = terms.loc[r.hpo_id]
        g.node(id=r.hpo_id, type="phenotype", label=t["name"], description=t["def"],
               synonyms=sorted(set(t["synonyms"])), url=f"https://hpo.jax.org/browse/term/{r.hpo_id}")
        onset = terms.loc[r.onset]["name"] if r.onset and r.onset in terms.index else None
        attrs = {k: v for k, v in {"frequency": flabel, "frequency_value": fval, "onset": onset,
                                   "hpo_evidence": r.evidence}.items() if v is not None}
        refs = [x for x in r.reference.split(";") if x] or [r.database_id]
        g.edge("disease_has_phenotype", r.mondo, r.hpo_id, status="curated", label="has symptom", attrs=attrs,
               evidence=[dict(source_type="database", source_name="HPO", source_ref=ref,
                              url=f"https://hpo.jax.org/browse/disease/{r.database_id}",
                              quote=f"{r.database_id} {r.disease_name} | {r.hpo_id} {t['name']} | "
                                    f"evidence {r.evidence} | frequency {r.frequency or 'n/a'} | {r.biocuration}",
                              method="curated", retrieved_at=ra) for ref in refs[:3]])
    write_node_patches("hpo", [{"id": k, "attrs": v} for k, v in patches.items()])

    g2d = g2d.assign(mondo=g2d["disease_id"].map(x2m))
    for r in g2d[g2d["mondo"].isin(diseases)].itertuples(index=False):
        rec = hgnc.resolve(r.ncbi_gene_id) or hgnc.resolve(r.gene_symbol)
        if not rec:
            continue
        gid = hgnc.gene_node(g, rec)
        g.edge("gene_associated_with_disease", gid, r.mondo, status="curated",
               label="causes" if r.association_type == "MENDELIAN" else "associated with",
               attrs={"association_type": r.association_type},
               evidence=dict(source_type="database", source_name="HPO", source_ref=r.disease_id,
                             url=r.source if r.source.startswith("http") else
                             "http://purl.obolibrary.org/obo/hp/hpoa/genes_to_disease.txt",
                             quote=f"{r.ncbi_gene_id}\t{r.gene_symbol}\t{r.association_type}\t{r.disease_id}\t{r.source}",
                             method="curated", retrieved_at=ra))
    g.close()
