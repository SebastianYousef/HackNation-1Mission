"""Tests run in fixtures mode against a tiny self-made fixtures dir (stable even
while contract/fixtures evolves). test_contract_fixtures.py covers the real ones."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from atlas_api.config import Settings
from atlas_api.main import create_app

D1 = {"id": "MONDO:1", "type": "disease", "subtype": None, "label": "CLN5 disease", "summary": "s"}
D2 = {"id": "MONDO:2", "type": "disease", "subtype": None, "label": "CLN6 disease", "summary": None}
G1 = {"id": "HGNC:1", "type": "gene", "subtype": None, "label": "CLN5", "summary": None}


def edge(eid: str, src: str, dst: str, etype: str, status: str = "curated", conf: float = 0.9) -> dict:
    return {"id": eid, "src": src, "dst": dst, "type": etype, "label": None, "status": status,
            "confidence": conf, "score": None, "support_count": 1, "contradict_count": 0,
            "sources": ["HPO"], "attrs": {}}


E1 = edge("E:1", "HGNC:1", "MONDO:1", "gene_associated_with_disease")
E2 = edge("E:2", "MONDO:1", "MONDO:2", "disease_similar_to", "inferred", 0.6)
EVID = {"id": "V:1", "stance": "supports", "source_type": "database", "source_name": "HPO", "source_ref": None,
        "url": None, "quote": None, "method": "curated", "published_at": None, "retrieved_at": "2026-10-03T00:00:00Z"}
CLUSTER = {"id": "CL:x", "label": "X", "summary": None, "method": "leiden", "size": 2, "attrs": {}}

FILES: dict[str, object] = {
    "meta.json": {"contract_version": "1.1.0", "dataset": {"version": "test-1"},
                  "counts": {"nodes": {"disease": 2, "gene": 1}, "edges": 2, "evidence": 1, "clusters": 1},
                  "sources": ["HPO"]},
    "search.json": {"cln5": [{**D1, "matched_name": "CLN5 disease", "match_kind": "label", "score": 1.0},
                             {**G1, "matched_name": "CLN5", "match_kind": "label", "score": 0.9}],
                    "cln6": [{**D2, "matched_name": "CLN6 disease", "match_kind": "label", "score": 1.0}]},
    "nodes/MONDO_1.json": {"node": {**D1, "description": None, "synonyms": [], "xrefs": {}, "attrs": {}, "url": None},
                           "degree": {"gene": 1, "disease": 1}, "clusters": [], "has_action_view": True,
                           "has_mechanism_view": False},
    "neighborhood/MONDO_1.json": {"center": "MONDO:1", "nodes": [D1, D2, G1], "edges": [E1, E2], "truncated": False},
    "edges/E_1.json": {"edge": E1, "source": G1, "target": D1, "supporting": [EVID], "contradicting": [], "context": []},
    "similar/MONDO_1.json": [{"disease": D2, "edge_id": "E:2", "score": 0.8, "confidence": 0.6, "status": "inferred",
                              "components": {}, "caution": None,
                              "shared": {"phenotypes": [], "mechanisms": [], "genes": []}, "same_cluster": True}] * 3,
    "paths/MONDO_1.json": [{"id": "P:1", "kind": "related_disease", "title": "t", "score": 0.5, "from": "MONDO:1",
                            "to": "MONDO:2", "node_ids": ["MONDO:1", "MONDO:2"], "edge_ids": ["E:2"],
                            "nodes": [D1, D2], "edges": [E2], "weakest_status": "inferred", "min_confidence": 0.6,
                            "attrs": {}}],
    "clusters.json": [CLUSTER],
    "clusters/CL_x.json": {"cluster": CLUSTER, "members": [{**D1, "membership": 1.0}],
                           "top_phenotypes": [], "top_mechanisms": []},
    "action-view/MONDO_1.json": {"disease": {"id": "MONDO:1"}, "headline": "h"},
    "mechanism-view/GO_1.json": {"mechanism": {"id": "GO:1"}, "headline": "h"},
    "explain.json": {"headline": "h", "steps": [{"text": "t", "edge_id": "E:1", "status": "curated",
                                                 "confidence": 0.9}],
                     "uncertainties": [], "what_to_check_next": "x", "model": None, "cached": True},
    "outreach-draft.json": {"subject": "s", "body": "b [1]", "model": None,
                            "citations": [{"n": 1, "edge_id": "E:1", "source_ref": None, "url": None}]},
    "gap-search-job.json": {"job_id": "job-x", "state": "done", "error": None,
                            "result": {"leads": [], "disclaimer": "unverified"}},
}


@pytest.fixture
def fixtures_dir(tmp_path: Path) -> Path:
    for rel, data in FILES.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data))
    return tmp_path


@pytest.fixture
def client(fixtures_dir: Path):
    settings = Settings(_env_file=None, data_mode="fixtures", fixtures_dir=fixtures_dir, instance_id="test-1",
                        redis_url=None, database_url=None, ai_rate_limit_per_minute=3,
                        cors_origins="https://*.example.org")
    with TestClient(create_app(settings)) as c:
        yield c
