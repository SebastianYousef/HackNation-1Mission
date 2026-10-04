"""Paths, env vars and the slice definition (config/slice.yaml)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

PIPELINE_DIR = Path(__file__).resolve().parent.parent          # backend/pipeline
REPO_DIR = PIPELINE_DIR.parent.parent
CONTRACT_TS = REPO_DIR / "contract" / "atlas.ts"
DATA_DIR = Path(os.environ.get("ATLAS_DATA_DIR", PIPELINE_DIR / "data"))
RAW = DATA_DIR / "raw"
INTERIM = DATA_DIR / "interim"
SNAPSHOT = DATA_DIR / "snapshot"
CONFIG_DIR = Path(os.environ.get("ATLAS_CONFIG_DIR", PIPELINE_DIR / "config"))


def _load_dotenv() -> None:
    """Minimal .env support (backend/pipeline/.env) without an extra dependency."""
    p = PIPELINE_DIR / ".env"
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_dotenv()


def env(name: str, default: str | None = None) -> str | None:
    v = os.environ.get(name)
    return v if v not in (None, "") else default


@dataclass
class Family:
    """One disease family of the slice (config/slice.yaml `families`): its MONDO roots, its focus roots (action
    views, paths and API ingest), seed genes, the label used in templates ('a rare disease in the <label> group')
    and the umbrella terms sponsors use for the whole family (never matched to one subtype)."""
    key: str
    label: str
    roots: list[str] = field(default_factory=list)
    focus_roots: list[str] = field(default_factory=list)
    seed_genes: list[str] = field(default_factory=list)
    umbrella_terms: list[str] = field(default_factory=list)
    query_by_gene: bool = False   # web searches name a numbered subtype by its causal gene (brightdata_orgs)


@dataclass
class SliceConfig:
    version: str
    name: str
    roots: list[str]
    focus_roots: list[str]
    extra_diseases: list[str]
    seed_genes: list[str]
    expansion: dict[str, Any] = field(default_factory=dict)
    literature: dict[str, Any] = field(default_factory=dict)
    analytics: dict[str, Any] = field(default_factory=dict)
    views: dict[str, Any] = field(default_factory=dict)
    families: list[Family] = field(default_factory=list)

    @property
    def umbrella_terms(self) -> set[str]:
        return {t for f in self.families for t in f.umbrella_terms}


def _union(*lists: list[str]) -> list[str]:
    return list(dict.fromkeys(x for xs in lists for x in xs or []))


@lru_cache
def slice_config(path: str | None = None) -> SliceConfig:
    p = Path(path or env("ATLAS_SLICE", str(CONFIG_DIR / "slice.yaml")))
    d = yaml.safe_load(p.read_text())
    fams = [Family(key=str(f["key"]), label=str(f.get("label") or f["key"]), roots=list(f.get("roots") or []),
                   focus_roots=list(f.get("focus_roots") or []), seed_genes=list(f.get("seed_genes") or []),
                   umbrella_terms=[str(t).lower() for t in f.get("umbrella_terms") or []],
                   query_by_gene=bool(f.get("query_by_gene", False)))
            for f in d.get("families") or []]
    # top-level roots / focus_roots / seed_genes (the original single-family format) plus every family's
    return SliceConfig(
        version=str(d.get("version", "dev")),
        name=d.get("name", "slice"),
        roots=_union(d.get("roots"), *(f.roots for f in fams)),
        focus_roots=_union(d.get("focus_roots"), *(f.focus_roots for f in fams)),
        extra_diseases=d.get("extra_diseases", []) or [],
        seed_genes=_union(d.get("seed_genes"), *(f.seed_genes for f in fams)),
        families=fams,
        expansion=d.get("expansion", {}) or {},
        literature=d.get("literature", {}) or {},
        analytics=d.get("analytics", {}) or {},
        views=d.get("views", {}) or {},
    )


@lru_cache
def curated_config() -> dict[str, Any]:
    p = CONFIG_DIR / "curated.yaml"
    return yaml.safe_load(p.read_text()) if p.exists() else {}


def ensure_dirs() -> None:
    for d in (RAW, INTERIM, SNAPSHOT):
        d.mkdir(parents=True, exist_ok=True)
