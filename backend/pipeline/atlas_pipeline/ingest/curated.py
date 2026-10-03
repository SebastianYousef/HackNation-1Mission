"""Hand-curated facts from config/curated.yaml (patient organisations, approved therapies).

Disease references may be MONDO ids or xrefs (OMIM:/ORPHA:) — mapped through the MONDO
equivalentTo table. Edges are status 'literature' (an org website / regulator page is the source),
quote null (we do not hold verbatim text), method 'curated:manual'.
"""
from __future__ import annotations

import logging

from ..config import curated_config
from ..store import GraphWriter, read_json

log = logging.getLogger(__name__)


def download() -> None:
    return None


def _disease(ref: str, sl: dict) -> str | None:
    if ref.startswith("MONDO:"):
        return ref if ref in set(sl["diseases"]) else None
    m = sl["xref_to_mondo"].get(ref)
    return m if m in set(sl["diseases"]) else None


def emit() -> None:
    cur = curated_config()
    sl = read_json("slice")
    g = GraphWriter("curated")
    for o in cur.get("organizations", []) or []:
        g.node(id=o["id"], type="organization", subtype=o.get("subtype"), label=o["label"], url=o.get("website"),
               attrs={"website": o.get("website"), "country": o.get("country"), "has_registry": o.get("has_registry"),
                      "contact_url": o.get("contact_url"), "source": "curated"})
        for ref in o.get("serves", []):
            d = _disease(ref, sl)
            if not d:
                log.warning("curated org %s: disease %s not in slice", o["id"], ref)
                continue
            g.edge("organization_serves_disease", o["id"], d, status="literature", label="supports families with",
                   evidence=dict(source_type="patient_org_site", source_name=o["label"], source_ref=o.get("website"),
                                 url=o.get("website"), quote=None, method="curated:manual"))
    for i in cur.get("interventions", []) or []:
        g.node(id=i["id"], type="intervention", subtype=i.get("subtype"), label=i["label"])
        for t in i.get("treats", []):
            d = _disease(t["disease"], sl)
            if not d:
                log.warning("curated intervention %s: disease %s not in slice", i["id"], t["disease"])
                continue
            g.edge("intervention_treats_disease", i["id"], d, status="curated", label="treats",
                   attrs={"approval": t.get("approval")},
                   evidence=dict(source_type="web", source_name="FDA", source_ref=t.get("url"), url=t.get("url"),
                                 quote=None, method="curated:manual"))
    g.close()
