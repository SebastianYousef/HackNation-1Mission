"""Hand-curated facts from config/curated.yaml: patient organisations, mechanisms, assets, interventions.
The YAML schema is documented at the top of that file.

Status rule: an edge is 'literature' when one of its supporting evidence entries carries a verbatim quote
(publication, regulator or organisation page); without a quote it is loaded as 'hypothesis' and logged.
Never 'curated' — that status belongs to curated databases. Entries without evidence are skipped.

Disease refs may be MONDO ids or xrefs (OMIM:/ORPHA:), mapped through MONDO equivalentTo; gene refs may be
HGNC ids or approved symbols, mapped through the HGNC table.

`python -m atlas_pipeline.ingest.curated --verify` re-fetches every evidence url and checks each quote is a
substring of the page text (tags stripped, whitespace collapsed). Network-dependent, so not part of `emit`.
"""
from __future__ import annotations

import html
import logging
import re
import sys
from typing import Any

from ..config import curated_config
from ..ids import intervention_id, mech_id, normalize_curie, org_id, slug
from ..models import SOURCE_TYPES, STANCES
from ..store import GraphWriter, read_json
from . import hgnc

log = logging.getLogger(__name__)

MECH_SUBTYPES = {"biological_process", "pathway"}
ASSET_SUBTYPES = {"registry", "natural_history_study", "animal_model", "cell_model", "biomarker", "biobank"}
ASSET_ACCESS = {"open", "on_request", "restricted", None}


def download() -> None:
    return None


def _ws(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def _evidence(raw: list | None, where: str) -> list[dict]:
    """YAML evidence entries -> GraphWriter evidence dicts. Malformed entries are dropped with a warning."""
    out: list[dict] = []
    for ev in raw or []:
        st, name = ev.get("source_type"), ev.get("source_name")
        if st not in SOURCE_TYPES or st == "computed" or not name or not (ev.get("url") or ev.get("source_ref")) \
                or (ev.get("stance") or "supports") not in STANCES:
            log.warning("curated %s: bad evidence entry dropped (source_type/source_name/url/stance): %s", where, ev)
            continue
        row = dict(source_type=st, source_name=str(name), source_ref=ev.get("source_ref") or ev.get("url"),
                   url=ev.get("url"), quote=_ws(str(ev["quote"])) if ev.get("quote") else None,
                   method=ev.get("method") or "manual", stance=ev.get("stance") or "supports",
                   published_at=None if ev.get("published_at") is None else str(ev["published_at"]))
        if ev.get("retrieved_at"):
            row["retrieved_at"] = str(ev["retrieved_at"])
        out.append(row)
    return out


class _Curated:
    def __init__(self, sl: dict):
        self.sl = sl
        self.diseases = set(sl["diseases"])
        self.genes = set(sl.get("genes") or [])
        self.g = GraphWriter("curated")
        self.skipped = 0

    # ---- reference resolution -----------------------------------------------------------------
    def disease(self, ref: Any, where: str) -> str | None:
        ref = normalize_curie(str(ref or ""))
        d = ref if ref and ref.startswith("MONDO:") else self.sl["xref_to_mondo"].get(ref)
        if d not in self.diseases:
            log.warning("curated %s: disease %s not in slice", where, ref)
            return None
        return d

    def gene(self, ref: Any, where: str) -> str | None:
        ref = str(ref or "").strip()
        rec = hgnc.resolve(normalize_curie(ref) if ref.upper().startswith("HGNC") else ref)
        if rec is None:
            log.warning("curated %s: gene %s not found in HGNC", where, ref)
            return None
        if rec["hgnc_id"] not in self.genes:
            log.info("curated %s: gene %s is outside the slice; adding its HGNC node", where, rec["symbol"])
            hgnc.gene_node(self.g, rec)
        return rec["hgnc_id"]

    @staticmethod
    def node_id(entry: dict, prefix: str, default: str, where: str) -> str | None:
        nid = entry.get("id") or default
        if not nid.startswith(prefix) or nid == prefix:
            log.warning("curated %s: id %s must start with %s; entry skipped", where, nid, prefix)
            return None
        return nid

    # ---- edges ---------------------------------------------------------------------------------
    def link(self, type_: str, src: str | None, dst: str | None, raw_ev: list | None, where: str, label: str,
             attrs: dict | None = None) -> str | None:
        if not src or not dst:
            self.skipped += 1
            return None
        ev = _evidence(raw_ev, where)
        if not ev:
            log.warning("curated %s: %s %s -> %s has no usable evidence; skipped", where, type_, src, dst)
            self.skipped += 1
            return None
        quoted = any(v["quote"] and v["stance"] == "supports" for v in ev)
        if not quoted:
            log.warning("curated %s: %s %s -> %s has no verbatim quote; loaded as hypothesis", where, type_, src, dst)
        return self.g.edge(type_, src, dst, status="literature" if quoted else "hypothesis", label=label,
                           attrs=attrs, evidence=ev)

    # ---- sections ------------------------------------------------------------------------------
    def organizations(self, items: list[dict]) -> None:
        for o in items:
            oid = self.node_id(o, "ORG:", org_id(o["label"]), o["label"])
            if not oid:
                continue
            self.g.node(id=oid, type="organization", subtype=o.get("subtype"), label=o["label"],
                        description=o.get("description"), url=o.get("website"),
                        attrs={"website": o.get("website"), "country": o.get("country"),
                               "has_registry": o.get("has_registry"), "contact_url": o.get("contact_url"),
                               "source": "curated"})
            for s in o.get("serves") or []:
                w = f"org {oid}"
                self.link("organization_serves_disease", oid, self.disease(s.get("disease"), w), s.get("evidence"),
                          w, "supports families with")

    def mechanisms(self, items: list[dict]) -> None:
        for m in items:
            mid = self.node_id(m, "ATLAS:mech-", mech_id(m["label"]), m["label"])
            if not mid:
                continue
            if m.get("subtype") not in MECH_SUBTYPES:
                log.warning("curated mechanism %s: subtype %s not one of %s", mid, m.get("subtype"), MECH_SUBTYPES)
            self.g.node(id=mid, type="mechanism", subtype=m.get("subtype"), label=m["label"],
                        description=m.get("description"), attrs={"source": "curated"})
            w = f"mechanism {mid}"
            for x in m.get("genes") or []:
                self.link("gene_in_mechanism", self.gene(x.get("gene"), w), mid, x.get("evidence"), w, "acts in")
            for x in m.get("diseases") or []:
                self.link("disease_involves_mechanism", self.disease(x.get("disease"), w), mid, x.get("evidence"),
                          w, "involves")

    def assets(self, items: list[dict], known_orgs: set[str]) -> None:
        for a in items:
            aid = self.node_id(a, "ASSET:", f"ASSET:{slug(a['label'])}", a["label"])
            if not aid:
                continue
            w = f"asset {aid}"
            if a.get("subtype") not in ASSET_SUBTYPES:
                log.warning("curated %s: subtype %s not one of %s", w, a.get("subtype"), ASSET_SUBTYPES)
            if a.get("access") not in ASSET_ACCESS:
                log.warning("curated %s: access %s not one of open|on_request|restricted", w, a.get("access"))
            owner = a.get("owner") or {}
            oid = normalize_curie(owner.get("id")) if owner.get("id") else None
            if oid and oid not in known_orgs:
                log.warning("curated %s: owner %s is not a curated organization (edge kept only if another "
                            "stage emits that node)", w, oid)
            self.g.node(id=aid, type="asset", subtype=a.get("subtype"), label=a["label"],
                        description=a.get("description"), url=a.get("url"),
                        attrs={"asset_kind": a.get("subtype"), "access": a.get("access"), "owner_id": oid,
                               "source": "curated"})
            if oid:
                self.link("organization_maintains_asset", oid, aid, owner.get("evidence"), w, "maintains")
            for x in a.get("covers") or []:
                self.link("asset_covers_disease", aid, self.disease(x.get("disease"), w), x.get("evidence"), w,
                          "covers")
            for x in a.get("targets") or []:
                self.link("asset_targets_gene", aid, self.gene(x.get("gene"), w), x.get("evidence"), w, "targets")

    def interventions(self, items: list[dict], known_mechs: set[str]) -> None:
        for i in items:
            iid = self.node_id(i, "ATLAS:int-", intervention_id(i["label"]), i["label"])
            if not iid:
                continue
            self.g.node(id=iid, type="intervention", subtype=i.get("subtype"), label=i["label"],
                        description=i.get("description"), synonyms=list(i.get("synonyms") or []))
            w = f"intervention {iid}"
            for t in i.get("treats") or []:
                if t.get("approval") not in ("approved", "investigational"):
                    log.warning("curated %s: approval %s not approved|investigational", w, t.get("approval"))
                self.link("intervention_treats_disease", iid, self.disease(t.get("disease"), w), t.get("evidence"),
                          w, "treats", attrs={"approval": t.get("approval")})
            for t in i.get("targets") or []:
                mid = normalize_curie(str(t.get("mechanism") or ""))
                if mid and mid.startswith("ATLAS:mech-") and mid not in known_mechs:
                    log.warning("curated %s: mechanism %s is not defined in curated.yaml", w, mid)
                self.link("intervention_targets_mechanism", iid, mid, t.get("evidence"), w, "targets")


def build(cur: dict, sl: dict) -> GraphWriter:
    """Build the curated stage in memory (no I/O); `emit` writes it."""
    c = _Curated(sl)
    orgs, mechs = cur.get("organizations") or [], cur.get("mechanisms") or []
    c.organizations(orgs)
    c.mechanisms(mechs)
    c.assets(cur.get("assets") or [], {n for n, x in c.g.nodes.items() if x["type"] == "organization"})
    c.interventions(cur.get("interventions") or [], {n for n, x in c.g.nodes.items() if x["type"] == "mechanism"})
    statuses = [e["status"] for e in c.g.edges.values()]
    log.info("curated: %d nodes, %d edges (%d literature, %d hypothesis), %d entries skipped", len(c.g.nodes),
             len(statuses), statuses.count("literature"), statuses.count("hypothesis"), c.skipped)
    return c.g


def emit() -> None:
    build(curated_config(), read_json("slice")).close()


# ---- quote verification (manual; needs network) ----------------------------------------------------
def page_text(raw: str) -> str:
    raw = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", raw)
    raw = re.sub(r"(?s)<!--.*?-->", " ", raw)
    return _ws(html.unescape(re.sub(r"(?s)<[^>]+>", " ", raw)))


def verify_quotes(cur: dict | None = None) -> list[str]:
    """Fetch every evidence url in curated.yaml and check each quote is on the page. Returns problems."""
    from ..http import request
    cur = cur if cur is not None else curated_config()
    by_url: dict[str, list[str]] = {}

    def walk(x: Any) -> None:
        if isinstance(x, dict):
            if "source_type" in x and x.get("quote") and x.get("url"):
                by_url.setdefault(x["url"], []).append(_ws(str(x["quote"])))
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
    walk(cur)
    problems: list[str] = []
    for url, quotes in by_url.items():
        try:
            text = page_text(request("GET", url, retries=2).text)
        except Exception as e:  # noqa: BLE001 — report and continue
            problems.append(f"FETCH FAILED {url}: {e}")
            continue
        problems += [f"QUOTE NOT ON PAGE {url}: {q[:80]!r}" for q in quotes if q not in text]
    log.info("verified %d quotes on %d pages: %d problems", sum(map(len, by_url.values())), len(by_url),
             len(problems))
    return problems


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if "--verify" in sys.argv:
        bad = verify_quotes()
        print("\n".join(bad) or "OK: every quote found on its page")
        sys.exit(1 if bad else 0)
    print("usage: python -m atlas_pipeline.ingest.curated --verify")
