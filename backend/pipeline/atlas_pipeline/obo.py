"""Small, fast OBO 1.4 parser (enough for mondo.obo and hp.obo)."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Iterator

_SYN = re.compile(r'^"((?:[^"\\]|\\.)*)"\s+(EXACT|RELATED|NARROW|BROAD)\s*([A-Z_]*)?\s*(\[[^\]]*\])?')
_DEF = re.compile(r'^"((?:[^"\\]|\\.)*)"\s*(\[[^\]]*\])?')
_SRC = re.compile(r'source="([^"]+)"')


def _strip_trailing(v: str) -> str:
    # drop "{...}" qualifiers and "! comment"
    if " {" in v:
        v = v[: v.index(" {")]
    if " ! " in v:
        v = v[: v.index(" ! ")]
    return v.strip()


def parse_obo(path: Path) -> Iterator[dict]:
    term: dict | None = None
    with open(path, encoding="utf-8") as f:
        for raw in f:
            line = raw.rstrip("\n")
            if line.startswith("["):
                if term is not None:
                    yield term
                term = {"id": None, "name": None, "def": None, "def_refs": [], "synonyms": [], "xrefs": [],
                        "is_a": [], "obsolete": False, "subsets": [], "replaced_by": None} \
                    if line == "[Term]" else None
                continue
            if term is None or ":" not in line:
                continue
            tag, val = line.split(":", 1)
            val = val.strip()
            if tag == "id":
                term["id"] = val
            elif tag == "name":
                term["name"] = val
            elif tag == "def":
                m = _DEF.match(val)
                if m:
                    term["def"] = m.group(1).replace('\\"', '"')
                    term["def_refs"] = [r.strip() for r in (m.group(2) or "[]")[1:-1].split(",") if r.strip()]
            elif tag == "synonym":
                m = _SYN.match(val)
                if m:
                    term["synonyms"].append({"name": m.group(1).replace('\\"', '"'), "scope": m.group(2),
                                             "type": m.group(3) or ""})
            elif tag == "xref":
                srcs = _SRC.findall(val)
                term["xrefs"].append({"id": _strip_trailing(val).split(" ")[0], "equivalent": "MONDO:equivalentTo" in srcs})
            elif tag == "is_a":
                term["is_a"].append(_strip_trailing(val))
            elif tag == "is_obsolete":
                term["obsolete"] = val == "true"
            elif tag == "subset":
                term["subsets"].append(_strip_trailing(val))
            elif tag == "replaced_by":
                term["replaced_by"] = val
    if term is not None:
        yield term


def descendants(children: dict[str, list[str]], roots: list[str]) -> set[str]:
    out, stack = set(), list(roots)
    while stack:
        t = stack.pop()
        if t in out:
            continue
        out.add(t)
        stack.extend(children.get(t, []))
    return out


def ancestors_map(parents: dict[str, list[str]]) -> dict[str, frozenset[str]]:
    """term -> all ancestors including itself (memoised DFS)."""
    memo: dict[str, frozenset[str]] = {}

    def anc(t: str) -> frozenset[str]:
        if t in memo:
            return memo[t]
        memo[t] = frozenset([t])  # cycle guard
        s = {t}
        for p in parents.get(t, []):
            s |= anc(p)
        memo[t] = frozenset(s)
        return memo[t]

    import sys
    sys.setrecursionlimit(max(10000, sys.getrecursionlimit()))
    for t in list(parents):
        anc(t)
    return memo
