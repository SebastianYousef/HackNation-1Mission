"""Phenotype information content from HPO annotation frequency (ancestor-propagated)."""
from __future__ import annotations

import math
from collections import Counter
from functools import lru_cache

from ..obo import ancestors_map
from ..store import read_parquet

ROOT = "HP:0000118"  # Phenotypic abnormality


@lru_cache
def hpo_ancestors() -> dict[str, frozenset[str]]:
    terms = read_parquet("hpo_terms")
    parents = {t: list(p) for t, p, o in zip(terms["id"], terms["parents"], terms["obsolete"]) if not o}
    anc = ancestors_map(parents)
    under = {t for t, a in anc.items() if ROOT in a}
    # keep only phenotypic-abnormality ancestors, drop the root itself (uninformative)
    return {t: frozenset(x for x in a if x in under and x != ROOT) for t, a in anc.items() if t in under}


def compute() -> dict[str, float]:
    hpoa = read_parquet("hpoa")
    hpoa = hpoa[(hpoa["aspect"] == "P") & (hpoa["qualifier"] != "NOT")]
    anc = hpo_ancestors()
    per_dis: dict[str, set[str]] = {}
    for d, t in zip(hpoa["database_id"], hpoa["hpo_id"]):
        if t in anc:
            per_dis.setdefault(d, set()).update(anc[t])
    n = len(per_dis)
    cnt = Counter(t for s in per_dis.values() for t in s)
    ic = {t: -math.log(c / n) for t, c in cnt.items()}
    mx = math.log(n)
    for t in anc:  # never annotated -> maximal IC
        ic.setdefault(t, mx)
    return ic
