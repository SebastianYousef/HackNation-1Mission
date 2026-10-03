"""Entity reconciliation: free-text names -> stable ids.

Tiers (first hit wins; method + confidence recorded in data/interim/reconcile_mapping.parquet):
  1. xref      — the string is itself a CURIE we know (MONDO/HP/HGNC) or an xref
                 (OMIM:/ORPHA:/DOID:…) with a 1:1 MONDO equivalentTo mapping            conf 1.0
  2. label     — normalised exact match on a node label                                   conf 0.95
     synonym   — normalised exact match on a synonym / gene alias                          conf 0.85
     abbrev    — exact match on an abbreviation / gene symbol alias (only if unambiguous)   conf 0.7
  3. embedding — embeddings nearest neighbour + LLM judge (STUB: `embedding_match` returns None;
                 TODO: embed names with text-embedding-3-small, store in nodes.embedding, top-5 cosine,
                 ask llm.structured to pick one or "none")                                   conf <= 0.6
Unresolved mechanism names become new curated-vocabulary nodes ATLAS:mech-<slug> (method new_node).

The `reconcile` stage also turns extracted claims (claims.jsonl) into literature/hypothesis edges.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from typing import Iterable

import pandas as pd

from .config import INTERIM
from .ids import mech_id, intervention_id, norm_name, person_id
from .store import GraphWriter, read_json, read_jsonl, write_parquet

log = logging.getLogger(__name__)
KIND_CONF = {"label": 0.95, "synonym": 0.85, "abbrev": 0.7}


class NameIndex:
    def __init__(self) -> None:
        self.names: dict[str, list[tuple[str, str, str]]] = defaultdict(list)  # norm -> [(id, type, kind)]
        self.ids: dict[str, str] = {}  # id -> type
        self.x2m: dict[str, str] = {}

    def add(self, node: dict) -> None:
        nid, t = node["id"], node["type"]
        self.ids[nid] = t
        self.names[norm_name(node["label"])].append((nid, t, "label"))
        for s in node.get("synonyms") or []:
            self.names[norm_name(s)].append((nid, t, "synonym"))
        for a in (node.get("attrs") or {}).get("abbreviations") or []:
            self.names[norm_name(a)].append((nid, t, "abbrev"))

    @classmethod
    def from_nodes(cls, nodes: Iterable[dict], types: set[str] | None = None) -> "NameIndex":
        idx = cls()
        for n in nodes:
            if types is None or n["type"] in types:
                idx.add(n)
        sl = read_json("slice") or {}
        idx.x2m = sl.get("xref_to_mondo", {})
        return idx

    @classmethod
    def from_stage_files(cls, types: set[str] | None = None,
                         stages: tuple[str, ...] = ("mondo", "hgnc", "hpo", "orphanet", "reactome", "curated")) -> "NameIndex":
        def gen():
            for st in stages:
                yield from read_jsonl(f"{st}.nodes.jsonl")
        return cls.from_nodes(gen(), types)

    def exact(self, name: str, types: set[str] | None = None) -> tuple[str, str, float] | None:
        """(id, method, confidence) or None. Ambiguous names (several ids) return None."""
        if not name:
            return None
        s = name.strip()
        if s in self.ids and (types is None or self.ids[s] in types):
            return s, "xref", 1.0
        if s in self.x2m and (types is None or "disease" in types):
            return self.x2m[s], "xref", 1.0
        hits = [h for h in self.names.get(norm_name(s), []) if types is None or h[1] in types]
        if not hits:
            return None
        for kind in ("label", "synonym", "abbrev"):
            ids = {h[0] for h in hits if h[2] == kind}
            if len(ids) == 1:
                return next(iter(ids)), kind, KIND_CONF[kind]
            if len(ids) > 1:
                return None
        return None

    def resolve(self, name: str, types: set[str] | None = None) -> tuple[str, str, float] | None:
        return self.exact(name, types) or embedding_match(name, types)


def embedding_match(name: str, types: set[str] | None) -> tuple[str, str, float] | None:
    """Tier 3 (STUB). See module docstring for the intended implementation."""
    return None


def _mapping_row(name: str, type_: str, hit, pmid: str | None) -> dict:
    return {"name": name, "type": type_, "id": hit[0] if hit else None, "method": hit[1] if hit else "unresolved",
            "confidence": hit[2] if hit else 0.0, "pmid": pmid}


def run() -> None:
    idx = NameIndex.from_stage_files()
    claims = list(read_jsonl("claims.jsonl"))
    g = GraphWriter("reconcile")
    mapping: list[dict] = []
    for c in claims:
        pid = f"PMID:{c['pmid']}"
        url = f"https://pubmed.ncbi.nlm.nih.gov/{c['pmid']}/"
        status = "hypothesis" if c.get("speculative") else "literature"
        ev = dict(source_type="publication", source_name="PubMed", source_ref=pid, url=url, quote=c["quote"],
                  method=f"llm:{c.get('model')}", stance=c.get("stance", "supports"), published_at=c.get("year"))

        def res(name, types, t):
            if not name:
                return None
            hit = idx.resolve(name, types)
            mapping.append(_mapping_row(name, t, hit, c["pmid"]))
            return hit[0] if hit else None

        disease = res(c.get("disease"), {"disease"}, "disease")
        if disease is None and len(c.get("query_diseases") or []) == 1:
            disease = c["query_diseases"][0]
        gene = res(c.get("gene"), {"gene"}, "gene")
        kind = c["kind"]
        mech = None
        if c.get("mechanism"):
            mech = res(c["mechanism"], {"mechanism"}, "mechanism")
            if mech is None:
                mech = mech_id(c["mechanism"])
                g.node(id=mech, type="mechanism", label=c["mechanism"].strip()[:120],
                       subtype="variant_effect" if kind == "variant_effect" else "biological_process",
                       attrs={"source": "llm_extraction"})
                mapping[-1].update(id=mech, method="new_node", confidence=0.5)
        if kind == "gene_disease" and gene and disease:
            g.edge("gene_associated_with_disease", gene, disease, status=status, label="associated with", evidence=ev)
        elif kind in ("mechanism", "variant_effect") and mech:
            if disease and kind == "mechanism":
                g.edge("disease_involves_mechanism", disease, mech, status=status, label="involves", evidence=ev)
            if gene:
                g.edge("gene_in_mechanism", gene, mech, status=status, label="acts in",
                       attrs={"variant": c.get("variant")} if c.get("variant") else None, evidence=ev)
        elif kind == "phenotype" and disease:
            ph = res(c.get("phenotype"), {"phenotype"}, "phenotype")
            if ph:
                g.edge("disease_has_phenotype", disease, ph, status=status, label="has symptom", evidence=ev)
        elif kind == "investigator" and disease and c.get("investigator"):
            parts = c["investigator"].replace(",", " ").split()
            if len(parts) >= 2:
                p = person_id(parts[-1], parts[0])
                g.node(id=p, type="person", subtype="researcher", label=" ".join(parts), attrs={"roles": ["researcher"]})
                g.edge("person_studies", p, disease, status=status, label="studies", evidence=ev)
        elif kind == "intervention" and disease and c.get("intervention"):
            hit = res(c["intervention"], {"intervention"}, "intervention")
            iid = hit or intervention_id(c["intervention"])
            if not hit:
                g.node(id=iid, type="intervention", subtype="other", label=c["intervention"].strip()[:120])
            g.edge("intervention_treats_disease", iid, disease, status=status, label="is being tested for",
                   attrs={"approval": "investigational"}, evidence=ev)
    g.close()
    df = pd.DataFrame(mapping, columns=["name", "type", "id", "method", "confidence", "pmid"])
    write_parquet("reconcile_mapping", df)
    log.info("reconcile: %d claims, %d names (%s)", len(claims), len(df),
             df["method"].value_counts().to_dict() if len(df) else {})
