"""CURIE normalisation and deterministic ids.

edge id     = 'E:' + sha1(type|src|dst)[:16]          (matches schema.sql comment)
evidence id = 'V:' + sha1(edge_id|source_name|source_ref|method|quote)[:16]
path id     = 'P:' + sha1(kind|node_ids...)[:16]
"""
from __future__ import annotations

import hashlib
import re
import unicodedata

# prefix spelling variants -> canonical prefix
_PREFIX = {
    "mondo": "MONDO", "hp": "HP", "hpo": "HP", "omim": "OMIM", "mim": "OMIM", "omimps": "OMIMPS",
    "orpha": "ORPHA", "orphanet": "ORPHA", "ordo": "ORPHA", "hgnc": "HGNC", "ncbigene": "NCBIGene",
    "entrez": "NCBIGene", "mesh": "MESH", "msh": "MESH", "umls": "UMLS", "doid": "DOID", "gard": "GARD",
    "icd10cm": "ICD10CM", "icd10": "ICD10", "icd9": "ICD9", "ncit": "NCIT", "snomedct": "SNOMEDCT",
    "snomedct_us": "SNOMEDCT", "medgen": "MEDGEN", "efo": "EFO", "go": "GO", "react": "REACT",
    "reactome": "REACT", "chebi": "CHEBI", "pmid": "PMID", "pubmed": "PMID", "clinvar": "CLINVAR",
    "uniprotkb": "UniProtKB", "ensembl": "ENSEMBL", "nci": "NCIT", "icd11": "ICD11", "nando": "NANDO",
}
_OBO_URL = re.compile(r"^https?://purl\.obolibrary\.org/obo/([A-Za-z]+)_(\w+)$")


def normalize_curie(s: str | None) -> str | None:
    """'Orphanet:558' -> 'ORPHA:558', 'MIM:256730' -> 'OMIM:256730', 'HP_0001250' -> 'HP:0001250',
    'MONDO:MONDO:0008769' -> 'MONDO:0008769'."""
    if not s:
        return None
    s = s.strip()
    m = _OBO_URL.match(s)
    if m:
        s = f"{m.group(1)}:{m.group(2)}"
    elif "_" in s and ":" not in s and re.match(r"^[A-Za-z]+_\d+$", s):
        s = s.replace("_", ":", 1)
    if ":" not in s:
        return s
    if re.match(r"^NCT\d{8}$", s):
        return s
    pfx, local = s.split(":", 1)
    pfx = _PREFIX.get(pfx.lower(), pfx)
    # doubled prefix ('MONDO:MONDO:0008769', 'HGNC:HGNC:2073', 'Orphanet:ORPHA:558'): drop the inner one
    while ":" in local and _PREFIX.get(local.split(":", 1)[0].lower(), local.split(":", 1)[0]) == pfx:
        local = local.split(":", 1)[1]
    return f"{pfx}:{local.strip()}"


def prefix(curie: str) -> str:
    return curie.split(":", 1)[0] if ":" in curie else ""


def _h(*parts: object, n: int = 16) -> str:
    return hashlib.sha1("|".join("" if p is None else str(p) for p in parts).encode()).hexdigest()[:n]


def edge_id(type_: str, src: str, dst: str) -> str:
    return "E:" + _h(type_, src, dst)


def evidence_id(edge_id_: str, source_name: str, source_ref: str | None, method: str, quote: str | None) -> str:
    return "V:" + _h(edge_id_, source_name, source_ref, method, quote)


def path_id(kind: str, node_ids: list[str]) -> str:
    return "P:" + _h(kind, *node_ids)


def slug(s: str, maxlen: int = 60) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-zA-Z0-9]+", "-", s.lower()).strip("-")
    return s[:maxlen].strip("-") or "x"


def person_id(last: str, first: str | None) -> str:
    """Name-based person id shared by PubMed and NIH RePORTER so the same PI merges.
    PERSON:<last>-<first token>. Collisions are possible; reconcile reports them."""
    first_tok = (first or "").replace(".", " ").split()
    return f"PERSON:{slug(last)}-{slug(first_tok[0]) if first_tok else 'x'}"


def org_id(name: str) -> str:
    return f"ORG:{slug(name)}"


def asset_id(name: str) -> str:
    """ASSET:<slug(name)>, e.g. asset_id('BDSRA Natural History Registry'); web leads use '<host> registry'."""
    return f"ASSET:{slug(name)}"


def mech_id(name: str) -> str:
    return f"ATLAS:mech-{slug(name)}"


def intervention_id(name: str) -> str:
    return f"ATLAS:int-{slug(name)}"


_WS = re.compile(r"\s+")


def norm_name(s: str) -> str:
    """Normalised lookup key for names (case/space/punctuation-insensitive)."""
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"[\-_,;:/()\[\]'\".]", " ", s)
    return _WS.sub(" ", s).strip()
