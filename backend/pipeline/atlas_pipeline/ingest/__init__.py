"""One module per source. Each exposes `download()` (cached raw files / API pulls) and
`emit()` (writes data/interim/<source>.{nodes,edges,evidence}.jsonl for the slice).
Order matters: mondo -> hgnc -> slice -> everything else (see ingest.run)."""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)

STATIC = ["mondo", "hgnc", "hpo", "orphanet", "reactome", "clinvar", "curated", "medlineplus"]
API = ["pubmed", "clinicaltrials", "nih_reporter", "brightdata_orgs"]
ALL = STATIC + API


def _mod(name: str):
    import importlib
    return importlib.import_module(f"atlas_pipeline.ingest.{name}")


def run(sources: list[str] | None = None, skip: list[str] | None = None) -> None:
    from .. import slice as slice_mod
    sources = sources or ALL
    sources = [s for s in sources if s not in (skip or [])]
    # raw downloads first (cached)
    for s in sources:
        if s in STATIC:
            log.info("== download %s", s)
            _mod(s).download()
    # parse the ontologies + build the slice (needs mondo, hgnc, hpo raw files)
    for need in ("mondo", "hgnc", "hpo"):
        _mod(need).download()
    _mod("mondo").parse()
    _mod("hgnc").parse()
    slice_mod.build()
    for s in sources:
        log.info("== emit %s", s)
        _mod(s).emit()
