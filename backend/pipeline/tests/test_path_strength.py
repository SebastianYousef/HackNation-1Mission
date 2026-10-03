"""One rule for a path's strength: paths.py computes it (with honesty caps), the loader insists on it,
views serve it unchanged and check_contract.py accepts exactly it."""
import importlib.util
from pathlib import Path

from atlas_pipeline.analytics.paths import path_strength
from atlas_pipeline.graph import Graph
from atlas_pipeline.load import validate
from atlas_pipeline.views import path_json

spec = importlib.util.spec_from_file_location(
    "check_contract", Path(__file__).resolve().parents[2] / "scripts" / "check_contract.py")
assert spec and spec.loader
cc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cc)


def graph() -> Graph:
    g = Graph()
    for nid, t in [("MONDO:1", "disease"), ("HP:1", "phenotype"), ("MONDO:2", "disease")]:
        g.nodes[nid] = {"id": nid, "type": t, "subtype": None, "label": nid}
    for eid, src, dst, status, conf in [("E:a", "MONDO:1", "HP:1", "curated", 0.9),
                                        ("E:b", "MONDO:2", "HP:1", "literature", 0.7)]:
        g.edges[eid] = {"id": eid, "src": src, "dst": dst, "type": "disease_has_phenotype",
                        "status": status, "confidence": conf, "attrs": {}}
        g.evidence[eid] = [{"id": f"V:{eid}", "edge_id": eid, "stance": "supports",
                            "source_type": "database", "source_name": "HPO"}]
    return g


def path(g: Graph, attrs: dict) -> dict:
    eids = ["E:a", "E:b"]
    weakest, conf = path_strength(g, eids, attrs)
    return {"id": "P:x", "from_id": "MONDO:1", "to_id": "MONDO:2", "kind": "related_disease", "title": "x",
            "node_ids": ["MONDO:1", "HP:1", "MONDO:2"], "edge_ids": eids, "score": 0.5,
            "weakest_status": weakest, "min_confidence": conf, "attrs": attrs}


def test_uncapped_path_equals_edge_minimum():
    g = graph()
    assert path_strength(g, ["E:a", "E:b"], {}) == ("literature", 0.7)
    pj = path_json(g, path(g, {}))
    assert (pj["weakest_status"], pj["min_confidence"]) == ("literature", 0.7)
    assert cc.check_path(pj, "$") == []


def test_caps_lower_never_raise():
    g = graph()
    assert path_strength(g, ["E:a", "E:b"], {"status_cap": "inferred", "confidence_cap": 0.25}) == ("inferred", 0.25)
    # a cap above the edges' own strength changes nothing
    assert path_strength(g, ["E:a", "E:b"], {"status_cap": "curated", "confidence_cap": 0.95}) == ("literature", 0.7)


def test_views_serve_stored_value_and_checker_accepts_cap():
    g = graph()
    p = path(g, {"status_cap": "hypothesis", "confidence_cap": 0.25, "caution": "one shared symptom"})
    pj = path_json(g, p)
    assert (pj["weakest_status"], pj["min_confidence"]) == ("hypothesis", 0.25)
    assert cc.check_path(pj, "$") == []


def test_checker_rejects_stronger_or_uncapped_weaker():
    g = graph()
    pj = path_json(g, path(g, {}))
    assert any("stronger" in x for x in cc.check_path({**pj, "weakest_status": "curated"}, "$"))
    assert any("above" in x for x in cc.check_path({**pj, "min_confidence": 0.8}, "$"))
    # weaker than the edges without a cap that says so is also a mismatch
    assert cc.check_path({**pj, "weakest_status": "inferred"}, "$")
    assert cc.check_path({**pj, "min_confidence": 0.2}, "$")


def test_loader_rejects_missing_or_stale_strength():
    g = graph()
    good = path(g, {"status_cap": "inferred", "confidence_cap": 0.25})
    assert validate(g, [], [], [good], []) == []
    stale = {**good, "weakest_status": "literature", "min_confidence": 0.7}
    missing = {k: v for k, v in good.items() if k not in ("weakest_status", "min_confidence")}
    for bad in (stale, missing):
        assert any("stale" in x for x in validate(g, [], [], [bad], []))
