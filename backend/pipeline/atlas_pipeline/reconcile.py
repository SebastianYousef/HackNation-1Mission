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

The `reconcile` stage also turns extracted claims (claims.jsonl) into literature/hypothesis edges
(stance 'speculative' -> status 'hypothesis'). A 'contradicts' claim never creates an edge: it is attached
as a contradicting evidence row to the same edge only if an earlier stage or a supporting claim made it.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from typing import Iterable

import pandas as pd

from .ids import edge_id, mech_id, intervention_id, norm_name, person_id
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


# extract.Relation -> (reconcile kind, field for the subject, field for the object)
RELATIONS = {
    "gene_associated_with_disease": ("gene_disease", "gene", "disease"),
    "variant_effect": ("variant_effect", "gene", "mechanism"),
    "disease_involves_mechanism": ("mechanism", "disease", "mechanism"),
    "gene_in_mechanism": ("mechanism", "gene", "mechanism"),
    "disease_has_phenotype": ("phenotype", "disease", "phenotype"),
    "intervention_treats_disease": ("intervention", "intervention", "disease"),
}


def normalise_claim(c: dict) -> dict | None:
    """extract's {subject, relation, object, stance} row -> {kind, gene/disease/..., speculative, stance}.
    Rows that already carry `kind` pass through. stance 'speculative' becomes speculative=True with
    evidence stance 'supports' (the edge is then status 'hypothesis'). Unknown relations -> None."""
    if "kind" not in c:
        m = RELATIONS.get(c.get("relation"))
        if m is None:
            return None
        kind, s_key, o_key = m
        c = {**c, "kind": kind, s_key: c.get("subject"), o_key: c.get("object")}
    if c.get("stance") == "speculative":
        c = {**c, "speculative": True, "stance": "supports"}
    return c


def _name_tokens(label: str) -> frozenset[str]:
    return frozenset(norm_name(label).split())


def person_id_report(nodes: Iterable[dict]) -> dict[str, list]:
    """The collision report ids.person_id promises (log only; ids are unchanged). person ids are
    PERSON:<last>-<first token>, so
      initial_only : ids whose first token is a bare initial (or missing): 'J Smith' from PubMed lumps every
                     J. Smith into PERSON:smith-j
      conflicting  : ids whose stages name different people: neither name's tokens contain the other's
                     ('John A Smith' vs 'John B Smith'; 'Erika F Augustine' vs 'Erika Augustine' is fine)
    `nodes` are person rows from the stage files (one label per id per stage)."""
    labels: dict[str, set[str]] = defaultdict(set)
    for n in nodes:
        if n.get("type") == "person" and n["id"].startswith("PERSON:"):
            labels[n["id"]].add(n["label"])
    initial_only = sorted(i for i in labels if len(i.rsplit("-", 1)[-1]) == 1)
    conflicting = []
    for pid in sorted(labels):
        toks = sorted({_name_tokens(x) for x in labels[pid]}, key=sorted)
        if any(not (a <= b or b <= a) for i, a in enumerate(toks) for b in toks[i + 1:]):
            conflicting.append((pid, sorted(labels[pid])))
    return {"ids": sorted(labels), "initial_only": initial_only, "conflicting": conflicting}


def log_person_id_report(nodes: Iterable[dict]) -> dict[str, list]:
    r = person_id_report(nodes)
    log.info("person ids: %d, %d initial-only (may merge different people, e.g. %s)", len(r["ids"]),
             len(r["initial_only"]), ", ".join(r["initial_only"][:5]) or "-")
    if r["conflicting"]:
        log.warning("person ids: %d carry names of different people (wrong merge?): %s", len(r["conflicting"]),
                    "; ".join(f"{pid} = {' / '.join(ls)}" for pid, ls in r["conflicting"][:10]))
    return r


def _prior_edge_ids() -> set[str]:
    """Edge ids written by the stages that run before reconcile (contradicting claims attach only to these)."""
    from .graph import STAGE_ORDER
    before = STAGE_ORDER[:STAGE_ORDER.index("reconcile")]
    return {e["id"] for st in before for e in read_jsonl(f"{st}.edges.jsonl")}


def run() -> None:
    idx = NameIndex.from_stage_files()
    claims = list(read_jsonl("claims.jsonl"))
    g = GraphWriter("reconcile")
    mapping: list[dict] = []
    contra: list[tuple[str, str, str, dict]] = []  # deferred (type, src, dst, evidence)
    n_skipped = 0
    for raw in claims:
        c = normalise_claim(raw)
        if c is None:
            n_skipped += 1
            continue
        pid = f"PMID:{c['pmid']}"
        url = f"https://pubmed.ncbi.nlm.nih.gov/{c['pmid']}/"
        status = "hypothesis" if c.get("speculative") else "literature"
        stance = c.get("stance") or "supports"
        contradicts = stance == "contradicts"
        yr = c.get("year")
        ev = dict(source_type="publication", source_name="PubMed", source_ref=pid, url=url, quote=c["quote"],
                  method=f"llm:{c.get('model')}", stance=stance, published_at=str(yr) if yr else None)

        def res(name, types, t):
            if not name:
                return None
            hit = idx.resolve(name, types)
            mapping.append(_mapping_row(name, t, hit, c["pmid"]))
            return hit[0] if hit else None

        def edge(type_, src, dst, **kw):
            # a contradicting claim never creates an edge; it is attached later to an existing one
            if contradicts:
                contra.append((type_, src, dst, ev))
            else:
                g.edge(type_, src, dst, status=status, evidence=ev, **kw)

        kind = c["kind"]
        dname = c.get("disease")
        disease = res(dname, {"disease"}, "disease")
        # the abstract's query disease stands in only when a disease slot was left blank, never for a
        # disease name that failed to resolve (that claim is about some other disease)
        if disease is None and "disease" in c and not (dname or "").strip() \
                and len(c.get("query_diseases") or []) == 1:
            disease = c["query_diseases"][0]
        gene = res(c.get("gene"), {"gene"}, "gene")
        variant = None
        if gene is None and kind == "variant_effect" and c.get("gene"):
            # "CLN5 p.Arg112His" -> gene CLN5, variant kept as an attribute
            head = c["gene"].split()[0]
            if head != c["gene"].strip():
                gene = res(head, {"gene"}, "gene")
                variant = c["gene"].strip() if gene else None
        mech = None
        if c.get("mechanism") and (gene or (disease and kind == "mechanism")):
            mech = res(c["mechanism"], {"mechanism"}, "mechanism")
            if mech is None:
                # a contradiction still gets the deterministic id (so it can attach to a node an earlier
                # claim created), but only a supporting claim creates the node
                mech = mech_id(c["mechanism"])
                if not contradicts:
                    g.node(id=mech, type="mechanism", label=c["mechanism"].strip()[:120],
                           subtype="variant_effect" if kind == "variant_effect" else "biological_process",
                           attrs={"source": "llm_extraction"})
                    mapping[-1].update(id=mech, method="new_node", confidence=0.5)
        variant = variant or c.get("variant")
        if kind == "gene_disease" and gene and disease:
            edge("gene_associated_with_disease", gene, disease, label="associated with")
        elif kind in ("mechanism", "variant_effect") and mech:
            if disease and kind == "mechanism":
                edge("disease_involves_mechanism", disease, mech, label="involves")
            if gene:
                edge("gene_in_mechanism", gene, mech, label="acts in",
                     attrs={"variant": variant} if variant else None)
        elif kind == "phenotype" and disease:
            ph = res(c.get("phenotype"), {"phenotype"}, "phenotype")
            if ph:
                edge("disease_has_phenotype", disease, ph, label="has symptom")
        elif kind == "investigator" and disease and c.get("investigator") and not contradicts:
            parts = c["investigator"].replace(",", " ").split()
            if len(parts) >= 2:
                p = person_id(parts[-1], parts[0])
                g.node(id=p, type="person", subtype="researcher", label=" ".join(parts), attrs={"roles": ["researcher"]})
                edge("person_studies", p, disease, label="studies")
        elif kind == "intervention" and disease and c.get("intervention"):
            hit = res(c["intervention"], {"intervention"}, "intervention")
            # same id clinicaltrials builds, so a contradiction can attach to a trial edge
            iid = hit or intervention_id(c["intervention"])
            if not hit and not contradicts:
                g.node(id=iid, type="intervention", subtype="other", label=c["intervention"].strip()[:120])
            edge("intervention_treats_disease", iid, disease, label="is being tested for",
                 attrs={"approval": "investigational"})
    n_contra = 0
    if contra:
        existing = _prior_edge_ids() | set(g.edges)
        for type_, src, dst, ev in contra:
            if edge_id(type_, src, dst) in existing:
                # status "hypothesis" is the weakest, so it never upgrades the edge's merged status
                g.edge(type_, src, dst, status="hypothesis", evidence=ev)
                n_contra += 1
    g.close()
    from .graph import STAGE_ORDER
    stages = [st for st in STAGE_ORDER if st != "reconcile"]
    log_person_id_report([*(n for st in stages for n in read_jsonl(f"{st}.nodes.jsonl")), *g.nodes.values()])
    df = pd.DataFrame(mapping, columns=["name", "type", "id", "method", "confidence", "pmid"])
    write_parquet("reconcile_mapping", df)
    log.info("reconcile: %d claims (%d unknown relation), %d contradictions attached of %d, %d names (%s)",
             len(claims), n_skipped, n_contra, len(contra), len(df),
             df["method"].value_counts().to_dict() if len(df) else {})
