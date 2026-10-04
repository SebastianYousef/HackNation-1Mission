"""Edge confidence rules (documented; the UI shows status + confidence on every edge).

curated    : 0.9 flat (asserted by a curated database: MONDO, HPO, Orphanet, ClinVar, registries).
             Each contradiction subtracts 0.15.
literature : 0.5 + 0.1 per *independent* supporting source_ref (distinct PMID/NCT/URL) beyond the
             first, capped at 0.85; minus 0.15 per contradicting evidence row. Computed rows
             (source_type 'computed', e.g. mechanism propagation) are not independent sources.
inferred   : the calibrated analytics score (see `calibrate`), never above 0.8.
hypothesis : min(0.4, literature rule) — speculative claims are capped at 0.4.
Result is clamped to [0.05, 1].
"""
from __future__ import annotations

from typing import Iterable, Mapping

CURATED = 0.9
LIT_BASE, LIT_STEP, LIT_CAP = 0.5, 0.1, 0.85
CONTRA_PENALTY = 0.15
HYPOTHESIS_CAP = 0.4
INFERRED_CAP = 0.8

STATUS_RANK = {"curated": 0, "literature": 1, "inferred": 2, "hypothesis": 3}  # lower = stronger


def calibrate(score: float) -> float:
    """Map an analytics similarity score (0..1) to a confidence. Linear, conservative:
    0.15 -> 0.33, 0.5 -> 0.55, 1.0 -> 0.8. Re-fit once we have labelled pairs."""
    return max(0.05, min(INFERRED_CAP, 0.25 + 0.55 * score))


def edge_confidence(status: str, evidence: Iterable[Mapping], score: float | None = None) -> float:
    ev = list(evidence)
    sup = {(e.get("source_ref") or e.get("url") or e.get("id")) for e in ev
           if e.get("stance") == "supports" and e.get("source_type") != "computed"}
    n_contra = sum(1 for e in ev if e.get("stance") == "contradicts")
    if status == "curated":
        c = CURATED
    elif status == "inferred":
        c = calibrate(score if score is not None else 0.3)
    else:
        c = min(LIT_CAP, LIT_BASE + LIT_STEP * max(0, len(sup) - 1))
        if status == "hypothesis":
            c = min(HYPOTHESIS_CAP, c)
    c -= CONTRA_PENALTY * n_contra
    return round(max(0.05, min(1.0, c)), 4)


def strongest_status(statuses: Iterable[str]) -> str:
    return min(statuses, key=lambda s: STATUS_RANK.get(s, 9))
