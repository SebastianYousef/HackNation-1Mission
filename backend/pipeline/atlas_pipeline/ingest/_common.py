"""Helpers shared by the API-backed ingesters."""
from __future__ import annotations

import re

from ..config import slice_config
from ..models import now_iso
from ..store import read_json, read_jsonl, write_json

GENERIC = {"disease", "syndrome", "disorder", "ncl", "lsd"}


def query_diseases() -> list[dict]:
    """Disease nodes to query APIs for (literature.scope: focus | slice), focus first."""
    sl = read_json("slice")
    scope = slice_config().literature.get("scope", "focus")
    ids = sl["focus"] if scope == "focus" else sl["diseases"]
    nodes = {n["id"]: n for n in read_jsonl("mondo.nodes.jsonl")}
    return [nodes[i] for i in ids if i in nodes]


# label words too unspecific to anchor an abbreviation query (see context_terms)
_CONTEXT_STOP = GENERIC | {"neuronal", "infantile", "juvenile", "adult", "congenital", "late", "early", "type",
                           "variant", "subtype", "progressive", "deficiency", "protracted", "northern",
                           "autosomal", "recessive", "dominant", "related", "associated", "form"}


def is_abbrev(name: str) -> bool:
    """Acronym-like names (INCL, CLN3, vLINCL): no space, >= 2 capitals. Search engines match them
    case-insensitively ('INCL' hits 'incl.', 'IncL/M'; 'CLN3' hits yeast 'Cln3'), so they need care."""
    s = name.strip()
    return " " not in s and sum(c.isupper() for c in s) >= 2


def search_names(node: dict, k: int = 4, abbreviations: bool = True) -> list[str]:
    """Label + up to k-1 distinctive synonyms/abbreviations usable as free-text queries.
    abbreviations=False skips letter-only acronyms (INCL, ANCL) that collide with ordinary words, for
    sources whose query cannot scope them (RePORTER). Acronyms with a digit (CLN5) are kept."""
    names = [node["label"]]
    cands = list(node.get("synonyms") or []) + list((node.get("attrs") or {}).get("abbreviations") or [])
    for s in sorted(cands, key=len):
        s2 = s.strip()
        if len(s2) < 4 or s2.lower() in GENERIC or s2.lower() in (n.lower() for n in names):
            continue
        if re.search(r"[\[\]\"]", s2) or (not abbreviations and is_abbrev(s2)
                                            and not any(c.isdigit() for c in s2)):
            continue
        names.append(s2)
        if len(names) >= k:
            break
    return names


def context_terms(node: dict) -> list[str]:
    """Distinctive words of the label ('ceroid', 'lipofuscinosis'); an abbreviation only counts in a
    query when one of these co-occurs."""
    words = re.findall(r"[a-z]+", node["label"].lower())
    return list(dict.fromkeys(w for w in words if len(w) >= 5 and w not in _CONTEXT_STOP))


def _phrase(name: str) -> str:
    """Regex for a name with spaces/hyphens/punctuation between its words interchangeable."""
    return r"[\W_]+".join(re.escape(w) for w in re.findall(r"\w+", name))


def mentions(text: str | None, names: list[str]) -> str | None:
    """First name that occurs in text as a whole phrase, else None. Word separators are interchangeable
    ('ceroid-lipofuscinosis'). Acronym-like names match case-sensitively, others case-insensitively. A
    name may not continue a hyphenated or 'late' prefix ('late-infantile NCL' is not 'infantile NCL')."""
    text = text or ""
    for n in names:
        body = _phrase(n)
        if not body:
            continue
        pat = r"(?<![\w-])(?<!late )" + body + r"(?!\w)"
        if re.search(pat, text, 0 if is_abbrev(n) else re.I):
            return n
    return None


class Coverage:
    """Per-source, per-disease record of what we searched (feeds ActionView.coverage)."""

    def __init__(self, source: str):
        self.source = source
        self.rows: dict[str, dict] = {}

    def add(self, disease_id: str, query: str, count: int | None) -> None:
        self.rows[disease_id] = {"query": query, "result_count": count, "checked_at": now_iso()}

    def save(self) -> None:
        write_json(f"coverage_{self.source}", self.rows)
