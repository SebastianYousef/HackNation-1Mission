"""Extract (OpenAI #1): PubMed abstracts -> claims {subject, relation, object, stance, quote}.

Input : data/interim/pubmed_abstracts.jsonl (from `ingest pubmed`)
Output: data/interim/claims.jsonl  (names only; `reconcile` maps them to ids and emits edges with
        status 'literature', or 'hypothesis' when stance == 'speculative', method 'llm:<model>')
Guardrail: a claim whose quote is not an exact (whitespace-normalised, case-insensitive) substring of
        the title+abstract is DROPPED and counted.
Cache : per PMID + prompt version (llm.py). `--batch` uses the OpenAI Batch API instead of live calls
        (submit, then re-run `extract --batch` until it reports done; results land in the same cache).
"""
from __future__ import annotations

import logging
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Literal

from pydantic import BaseModel, Field

from . import llm
from .store import read_jsonl, write_jsonl

log = logging.getLogger(__name__)
PROMPT_VERSION = "extract-v2"
NS = "extract"

Relation = Literal[
    "gene_associated_with_disease",   # subject gene, object disease
    "variant_effect",                 # subject gene (or variant), object molecular effect / process
    "disease_involves_mechanism",     # subject disease, object biological process / pathway
    "gene_in_mechanism",              # subject gene, object biological process / pathway
    "disease_has_phenotype",          # subject disease, object clinical feature
    "intervention_treats_disease",    # subject therapy, object disease
]

SYSTEM = """Extract evidence-backed claims from one biomedical abstract about rare lysosomal diseases
(e.g. neuronal ceroid lipofuscinoses / Batten disease). Only claims the abstract itself states.
Each claim: subject, relation, object, stance, quote.
 relation (subject -> object):
  gene_associated_with_disease  gene -> disease
  variant_effect                gene or variant -> molecular effect ("loss of enzyme activity", "ER retention")
  disease_involves_mechanism    disease -> biological process ("autophagy", "lysosomal protein degradation")
  gene_in_mechanism             gene -> biological process
  disease_has_phenotype         disease -> clinical feature ("seizures", "vision loss")
  intervention_treats_disease   therapy -> disease
 Use names exactly as written (gene symbols, disease names). Keep process names short noun phrases.
 stance: "supports" (asserted), "contradicts" (evidence against), "speculative" (hedged: may, might,
  could, suggest, potential, hypothesize).
 quote: copy ONE sentence or clause VERBATIM (exact characters) from the abstract that states the claim.
Return at most 12 claims; prefer the most specific ones."""


class Claim(BaseModel):
    subject: str
    relation: Relation
    object: str
    stance: Literal["supports", "contradicts", "speculative"]
    quote: str = Field(description="verbatim text copied from the abstract")


class Claims(BaseModel):
    claims: list[Claim]


_WS = re.compile(r"\s+")


def _norm(s: str) -> str:
    return _WS.sub(" ", s).strip().lower()


def _prompt(rec: dict) -> str:
    return f"PMID: {rec['pmid']}\nTITLE: {rec['title']}\nABSTRACT: {rec['abstract']}"


def check_quotes(rec: dict, out: Claims | None) -> tuple[list[dict], int]:
    """Keep claims whose quote is verbatim in title+abstract; return (rows, n_dropped)."""
    if out is None:
        return [], 0
    hay = _norm(rec["title"] + " " + rec["abstract"])
    rows, dropped = [], 0
    for c in out.claims:
        if not c.quote.strip() or _norm(c.quote) not in hay:
            dropped += 1
            continue
        rows.append({**c.model_dump(), "pmid": rec["pmid"], "year": rec.get("year"),
                     "query_diseases": rec.get("query_diseases", []), "model": llm.model_name()})
    return rows, dropped


def run(limit: int | None = None, concurrency: int = 4, batch: bool = False) -> None:
    recs = [r for r in read_jsonl("pubmed_abstracts.jsonl") if r.get("abstract")]
    if limit:
        recs = recs[:limit]
    if not recs:
        log.info("extract: no abstracts (run `ingest pubmed` first)")
    if batch:
        st = llm.batch_step(NS, SYSTEM, [(r["pmid"], _prompt(r)) for r in recs], Claims, PROMPT_VERSION)
        log.info("extract --batch: %s", st)
        if st.startswith("pending") or st == "submitted":
            log.info("re-run `extract --batch` later; claims.jsonl is built from whatever is cached now")
    elif not llm.available():
        log.warning("extract: OPENAI_API_KEY not set — only cached extractions are used")

    def one(rec: dict):
        if batch or not llm.available():
            return llm.cached(NS, Claims, rec["pmid"], PROMPT_VERSION)
        return llm.structured(NS, SYSTEM, _prompt(rec), Claims, rec["pmid"], PROMPT_VERSION)

    rows: list[dict] = []
    n_raw = n_drop = n_done = 0
    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        for rec, out in zip(recs, pool.map(one, recs)):
            if out is not None:
                n_done += 1
                n_raw += len(out.claims)
            kept, dropped = check_quotes(rec, out)
            rows.extend(kept)
            n_drop += dropped
    write_jsonl("claims.jsonl", rows)
    llm.record_usage("extract", abstracts=len(recs), abstracts_extracted=n_done, claims_returned=n_raw,
                     claims_kept=len(rows), claims_dropped_quote_check=n_drop, batch_api=batch,
                     prompt_version=PROMPT_VERSION)
    log.info("extract: %d abstracts, %d extracted, %d claims kept, %d dropped by quote check",
             len(recs), n_done, len(rows), n_drop)
