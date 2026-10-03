"""Extract (OpenAI #1): PubMed abstracts -> claims {subject, relation, object, stance, quote}.

Input : data/interim/pubmed_abstracts.jsonl (from `ingest pubmed`)
Output: data/interim/claims.jsonl  (names only; `reconcile` maps them to ids and emits edges with
        status 'literature', or 'hypothesis' when stance == 'speculative', method 'llm:<model>')
Guardrail: a claim is DROPPED (and counted) unless its quote is an exact (whitespace-normalised,
        case-insensitive) substring of the title or of the abstract, has >= 4 words / 20 chars and names
        the claim's subject or object; the stored quote is the source's own text for that span.
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


MIN_QUOTE_WORDS, MIN_QUOTE_CHARS = 4, 20


def _find(quote: str, text: str) -> str | None:
    """The span of `text` that equals `quote` up to case and whitespace, copied from the source."""
    words = quote.split()
    if not words:
        return None
    m = re.search(r"\s+".join(map(re.escape, words)), text, re.IGNORECASE)
    return m.group(0) if m else None


def check_quotes(rec: dict, out: Claims | None) -> tuple[list[dict], int]:
    """Keep claims whose quote is verbatim (case/whitespace-insensitive) inside the title or inside the
    abstract, is at least MIN_QUOTE_WORDS words / MIN_QUOTE_CHARS chars, and names the subject or the
    object. The stored quote is the matching span of the source text, not the LLM's copy.
    Returns (rows, n_dropped)."""
    if out is None:
        return [], 0
    rows, dropped = [], 0
    for c in out.claims:
        q = _norm(c.quote)
        span = None
        if len(q) >= MIN_QUOTE_CHARS and len(q.split()) >= MIN_QUOTE_WORDS \
                and (_norm(c.subject) in q or _norm(c.object) in q):
            span = _find(c.quote, rec.get("abstract") or "") or _find(c.quote, rec.get("title") or "")
        if span is None:
            dropped += 1
            continue
        rows.append({**c.model_dump(), "quote": span, "pmid": rec["pmid"], "year": rec.get("year"),
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
