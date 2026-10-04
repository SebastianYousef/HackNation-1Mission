"""Entity reconciliation: free-text names -> stable ids.

Tiers (first hit wins; method + confidence recorded in data/interim/reconcile_mapping.parquet):
  1. xref      — the string is itself a CURIE we know (MONDO/HP/HGNC) or an xref
                 (OMIM:/ORPHA:/DOID:…) with a 1:1 MONDO equivalentTo mapping            conf 1.0
  2. label     — normalised exact match on a node label                                   conf 0.95
     synonym   — normalised exact match on a synonym / gene alias                          conf 0.85
     abbrev    — exact match on an abbreviation / gene symbol alias (only if unambiguous)   conf 0.7
  3. embedding — only for names tiers 1-2 left unresolved (EmbeddingIndex). Node "label; synonyms" and the
                 name are embedded (llm.embed, OPENAI_EMBED_MODEL, cached; load copies the vectors into
                 nodes.embedding); top-5 cosine among nodes of the SAME type:
                   embedding     top-1 >= ATLAS_EMBED_MIN_SCORE (0.85) and >= ATLAS_EMBED_MARGIN (0.05)
                                 ahead of the next id                                  conf 0.6 * score
                   embedding+llm otherwise, candidates >= ATLAS_EMBED_JUDGE_FLOOR (0.70) go to
                                 llm.pick_candidate, which may only pick one of them or "none"  conf 0.5
                 A type whose nodes are not all embedded (no key, budget, insufficient_quota) is skipped,
                 so a missing vector can never make a wrong node win. Disease umbrella terms never match.
                 Edges with such an endpoint keep the claim's status, get attrs.name_match (method, score)
                 and attrs.confidence_cap 0.6 (paths never rank them above that).
Unresolved mechanism names become new curated-vocabulary nodes ATLAS:mech-<slug> (method new_node).

The `reconcile` stage also turns extracted claims (claims.jsonl) into literature/hypothesis edges
(stance 'speculative' -> status 'hypothesis'). A 'contradicts' claim never creates an edge: it is attached
as a contradicting evidence row to the same edge only if an earlier stage or a supporting claim made it.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from typing import Callable, Iterable, NamedTuple

import pandas as pd

from . import llm
from .config import env
from .ids import edge_id, mech_id, intervention_id, norm_name, person_id
from .store import GraphWriter, read_json, read_jsonl, write_json, write_parquet

log = logging.getLogger(__name__)
KIND_CONF = {"label": 0.95, "synonym": 0.85, "abbrev": 0.7}
EMBED_TYPES = {"disease", "gene", "phenotype", "mechanism", "intervention"}
EMBED_CONF_CAP = 0.6
JUDGE_CONF = 0.5


class Match(NamedTuple):
    """A tier-3 hit; indexes like the (id, method, confidence) tuples of tier 1-2."""
    id: str
    method: str
    confidence: float
    score: float | None = None


class NameIndex:
    def __init__(self) -> None:
        self.names: dict[str, list[tuple[str, str, str]]] = defaultdict(list)  # norm -> [(id, type, kind)]
        self.ids: dict[str, str] = {}  # id -> type
        self.x2m: dict[str, str] = {}
        self.nodes: dict[str, dict] = {}  # id -> first node row seen (stage order, as graph merge)
        self.embedder: "EmbeddingIndex | None" = None

    def add(self, node: dict) -> None:
        nid, t = node["id"], node["type"]
        self.ids[nid] = t
        self.nodes.setdefault(nid, node)
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

    def resolve(self, name: str, types: set[str] | None = None, context: str | None = None,
                fuzzy: bool = True) -> tuple[str, str, float] | Match | None:
        hit = self.exact(name, types)
        if hit is None and fuzzy and self.embedder is not None:
            hit = self.embedder.match(name, types, context)
        return hit


def node_text(node: dict) -> str:
    """What is embedded for a node: its label plus up to 5 synonyms (names, not definitions)."""
    syn = [s for s in dict.fromkeys(node.get("synonyms") or []) if s and s != node["label"]][:5]
    return "; ".join([node["label"], *syn])


def _umbrella() -> set[str]:
    from .ingest.clinicaltrials import umbrella   # lazy: clinicaltrials imports this module
    return umbrella()


class EmbeddingIndex:
    """Tier 3: top-5 cosine over the embedded nodes of one type (see module docstring).
    `embed` maps texts to unit vectors or None (llm.embed; tests pass a fake)."""

    def __init__(self, idx: NameIndex, embed: Callable[[list[str]], list] | None = None,
                 judge: Callable[[str, str | None, list[dict]], str | None] | None = None) -> None:
        self.idx = idx
        self.embed = embed or llm.embed
        self.judge = judge or llm.pick_candidate
        self.min_score = float(env("ATLAS_EMBED_MIN_SCORE", "0.85"))
        self.margin = float(env("ATLAS_EMBED_MARGIN", "0.05"))
        self.floor = float(env("ATLAS_EMBED_JUDGE_FLOOR", "0.70"))
        self.mats: dict[str, tuple[list[str], object] | None] = {}  # type -> (ids, unit-row matrix) | None
        self.qvecs: dict[str, object] = {}
        self.umbrella: set[str] | None = None
        self.embedded: dict[str, str] = {}  # id -> embedded text (load copies the vectors to nodes.embedding)

    def warm(self, names_by_type: dict[str, set[str]]) -> None:
        """Embed every needed node type and query name in as few batched calls as possible."""
        import numpy as np
        types = [t for t in names_by_type if t in EMBED_TYPES and names_by_type[t] and t not in self.mats]
        ids = {t: [i for i, ty in self.idx.ids.items() if ty == t] for t in types}
        queries = sorted({n.strip() for t in names_by_type if t in EMBED_TYPES for n in names_by_type[t]} - {""})
        node_texts = [node_text(self.idx.nodes[i]) for t in types for i in ids[t]]
        vecs = self.embed(node_texts + queries) if node_texts or queries else []
        k = 0
        for t in types:
            vs = vecs[k:k + len(ids[t])]
            k += len(ids[t])
            if not vs or any(v is None for v in vs):
                # a partial index could let a wrong node win because the right one has no vector
                log.info("embedding tier off for %s: %d of %d nodes embedded", t,
                         sum(v is not None for v in vs), len(vs))
                self.mats[t] = None
            else:
                self.mats[t] = (ids[t], np.vstack(vs))
                self.embedded.update((i, node_text(self.idx.nodes[i])) for i in ids[t])
        for q, v in zip(queries, vecs[k:]):
            if v is not None:
                self.qvecs[q] = v

    def candidates(self, name: str, type_: str, k: int = 5) -> list[tuple[str, float]]:
        import numpy as np
        q = name.strip()
        if type_ not in self.mats or q not in self.qvecs:
            self.warm({type_: {q}})
        m, v = self.mats.get(type_), self.qvecs.get(q)
        if m is None or v is None:
            return []
        ids, mat = m
        sims = mat @ v
        top = np.argsort(-sims)[:k]
        return [(ids[i], float(sims[i])) for i in top]

    def match(self, name: str, types: set[str] | None, context: str | None = None) -> Match | None:
        if not name or not name.strip() or not types or len(types) != 1:
            return None   # same node type required: never compare across types
        t = next(iter(types))
        if t not in EMBED_TYPES:
            return None
        if t == "disease":
            if self.umbrella is None:
                self.umbrella = _umbrella()
            if norm_name(name) in self.umbrella:
                return None   # "Batten disease" names a family, never one subtype
        cands = self.candidates(name, t)
        if not cands:
            return None
        (best, s1), s2 = cands[0], (cands[1][1] if len(cands) > 1 else -1.0)
        if s1 >= self.min_score and s1 - s2 >= self.margin:
            return Match(best, "embedding", round(min(EMBED_CONF_CAP, EMBED_CONF_CAP * s1), 3), round(s1, 4))
        pool = [(i, s) for i, s in cands if s >= self.floor]
        if not pool:
            return None
        nodes = self.idx.nodes
        choice = self.judge(name, context, [{"id": i, "label": nodes[i]["label"],
                                             "synonyms": list(nodes[i].get("synonyms") or [])[:5]} for i, _ in pool])
        score = dict(pool).get(choice) if choice else None
        if score is None:   # "none", no key, or an id outside the candidates
            return None
        return Match(choice, "embedding+llm", JUDGE_CONF, round(score, 4))


def _mapping_row(name: str, type_: str, hit, pmid: str | None) -> dict:
    return {"name": name, "type": type_, "id": hit[0] if hit else None, "method": hit[1] if hit else "unresolved",
            "confidence": hit[2] if hit else 0.0, "score": hit.score if isinstance(hit, Match) else None,
            "pmid": pmid}


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


# claim field -> node type it resolves to (tier 3 pre-pass)
NAME_FIELDS = {"disease": "disease", "gene": "gene", "mechanism": "mechanism", "phenotype": "phenotype",
               "intervention": "intervention"}


def leftover_names(idx: NameIndex, claims: Iterable[dict]) -> dict[str, set[str]]:
    """Names tiers 1-2 cannot resolve, by type: what tier 3 must embed (so it is embedded in one batch)."""
    out: dict[str, set[str]] = defaultdict(set)
    for c in claims:
        for f, t in NAME_FIELDS.items():
            name = (c.get(f) or "").strip()
            if name and idx.exact(name, {t}) is None:
                out[t].add(name)
    return out


def run(embed: Callable[[list[str]], list] | None = None,
        judge: Callable[[str, str | None, list[dict]], str | None] | None = None) -> None:
    """`embed` / `judge` replace llm.embed / llm.pick_candidate (tests)."""
    idx = NameIndex.from_stage_files()
    claims = list(read_jsonl("claims.jsonl"))
    normed = [n for n in map(normalise_claim, claims) if n is not None]
    left = leftover_names(idx, normed)
    if left and env("ATLAS_EMBED", "on") != "off":
        idx.embedder = EmbeddingIndex(idx, embed=embed, judge=judge)
        idx.embedder.warm(left)
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

        fuzzy: dict[str, dict] = {}  # id -> how a tier-3 match reached it (recorded on the edge)

        def res(name, types, t, allow_fuzzy=True):
            if not name:
                return None
            hit = idx.resolve(name, types, context=c["quote"], fuzzy=allow_fuzzy)
            mapping.append(_mapping_row(name, t, hit, c["pmid"]))
            if isinstance(hit, Match):
                fuzzy[hit.id] = {"name": name.strip(), "id": hit.id, "method": hit.method, "score": hit.score}
            return hit[0] if hit else None

        def edge(type_, src, dst, **kw):
            # a contradicting claim never creates an edge; it is attached later to an existing one
            if contradicts:
                contra.append((type_, src, dst, ev))
                return
            matched = [fuzzy[x] for x in (src, dst) if x in fuzzy]
            if matched:
                # the claim's status is kept (never upgraded); the fuzzy name match caps path strength
                kw["attrs"] = {**(kw.get("attrs") or {}), "name_match": matched, "confidence_cap": EMBED_CONF_CAP}
            g.edge(type_, src, dst, status=status, evidence=ev, **kw)

        kind = c["kind"]
        dname = c.get("disease")
        disease = res(dname, {"disease"}, "disease")
        # the abstract's query disease stands in only when a disease slot was left blank, never for a
        # disease name that failed to resolve (that claim is about some other disease)
        if disease is None and "disease" in c and not (dname or "").strip() \
                and len(c.get("query_diseases") or []) == 1:
            disease = c["query_diseases"][0]
        gname = (c.get("gene") or "").strip()
        splittable = kind == "variant_effect" and len(gname.split()) > 1
        # "CLN5 p.Arg112His": the exact gene symbol is tried before any embedding match of the whole string
        gene = res(c.get("gene"), {"gene"}, "gene", allow_fuzzy=not splittable)
        variant = None
        if gene is None and splittable:
            # "CLN5 p.Arg112His" -> gene CLN5, variant kept as an attribute
            gene = res(gname.split()[0], {"gene"}, "gene", allow_fuzzy=False)
            variant = gname if gene else None
            if gene is None:
                gene = res(gname, {"gene"}, "gene")
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
    df = pd.DataFrame(mapping, columns=["name", "type", "id", "method", "confidence", "score", "pmid"])
    write_parquet("reconcile_mapping", df)
    write_json("embedded_nodes", idx.embedder.embedded if idx.embedder else {})
    log.info("reconcile: %d claims (%d unknown relation), %d contradictions attached of %d, %d names (%s)",
             len(claims), n_skipped, n_contra, len(contra), len(df),
             df["method"].value_counts().to_dict() if len(df) else {})
