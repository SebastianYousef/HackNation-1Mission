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


@lru_cache
def slice_config(path: str | None = None) -> SliceConfig:
    p = Path(path or env("ATLAS_SLICE", str(CONFIG_DIR / "slice.yaml")))
    d = yaml.safe_load(p.read_text())
    return SliceConfig(
        version=str(d.get("version", "dev")),
        name=d.get("name", "slice"),
        roots=d.get("roots", []),
        focus_roots=d.get("focus_roots", []),
        extra_diseases=d.get("extra_diseases", []) or [],
        seed_genes=d.get("seed_genes", []) or [],
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
