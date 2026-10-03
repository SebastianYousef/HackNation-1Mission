from __future__ import annotations

import pytest

V1 = "/api/v1"


def assert_error(r, status: int, code: str) -> None:
    assert r.status_code == status, r.text
    body = r.json()
    assert set(body) == {"error"}
    assert set(body["error"]) == {"code", "message", "request_id"}
    assert body["error"]["code"] == code
    assert body["error"]["request_id"] == r.headers["x-request-id"]


def test_ops(client):
    assert client.get("/healthz").json()["status"] == "ok"
    r = client.get("/readyz")
    assert r.status_code == 200 and r.json()["ready"] is True


def test_headers_and_request_id_propagation(client):
    r = client.get(f"{V1}/meta", headers={"X-Request-Id": "abc-123"})
    assert r.headers["x-request-id"] == "abc-123"
    assert r.headers["x-served-by"] == "test-1"
    assert r.headers["cache-control"] == "public, max-age=60"
    assert r.headers["etag"].startswith('W/"fx-test-1')
    generated = client.get(f"{V1}/meta", headers={"X-Request-Id": "bad id with spaces"}).headers["x-request-id"]
    assert generated != "bad id with spaces" and len(generated) == 32


def test_cache_and_etag(client):
    r1 = client.get(f"{V1}/clusters")
    assert r1.headers["x-cache"] == "MISS"
    r2 = client.get(f"{V1}/clusters")
    assert r2.headers["x-cache"] == "HIT" and r2.json() == r1.json()
    r3 = client.get(f"{V1}/clusters", headers={"If-None-Match": r1.headers["etag"]})
    assert r3.status_code == 304


def test_meta(client):
    assert client.get(f"{V1}/meta").json()["contract_version"] == "1.0.0"


def test_search(client):
    assert [h["id"] for h in client.get(f"{V1}/search", params={"q": "CLN5"}).json()] == ["MONDO:1", "HGNC:1"]
    assert [h["id"] for h in client.get(f"{V1}/search", params={"q": "cln5", "types": "gene"}).json()] == ["HGNC:1"]
    # unknown query -> substring filter over all hits, deduped
    ids = [h["id"] for h in client.get(f"{V1}/search", params={"q": "disease"}).json()]
    assert sorted(ids) == ["MONDO:1", "MONDO:2"]
    assert client.get(f"{V1}/search", params={"q": "cln", "limit": 1}).json().__len__() == 1
    assert client.get(f"{V1}/search", params={"q": "c"}).json() == []
    assert_error(client.get(f"{V1}/search", params={"q": "cln", "types": "planet"}), 400, "bad_request")


@pytest.mark.parametrize("path", ["/nodes/MONDO:1", "/nodes/MONDO%3A1"])
def test_node(client, path):
    r = client.get(V1 + path)
    assert r.status_code == 200 and r.json()["node"]["id"] == "MONDO:1"


def test_node_404(client):
    assert_error(client.get(f"{V1}/nodes/MONDO:404"), 404, "not_found")
    assert_error(client.get(f"{V1}/nodes/..%2F..%2Fmeta"), 404, "not_found")  # no path traversal
    assert_error(client.get(f"{V1}/does-not-exist"), 404, "not_found")


def test_neighborhood(client):
    nb = client.get(f"{V1}/nodes/MONDO%3A1/neighborhood").json()
    assert nb["center"] == "MONDO:1" and len(nb["edges"]) == 2
    f = client.get(f"{V1}/nodes/MONDO:1/neighborhood", params={"statuses": "curated"}).json()
    assert [e["id"] for e in f["edges"]] == ["E:1"] and {n["id"] for n in f["nodes"]} == {"MONDO:1", "HGNC:1"}
    assert_error(client.get(f"{V1}/nodes/MONDO:1/neighborhood", params={"depth": 9}), 422, "bad_request")
    assert_error(client.get(f"{V1}/nodes/MONDO:1/neighborhood", params={"edge_types": "x"}), 400, "bad_request")
    assert_error(client.get(f"{V1}/nodes/MONDO:9/neighborhood"), 404, "not_found")


def test_edge(client):
    assert client.get(f"{V1}/edges/E%3A1").json()["edge"]["id"] == "E:1"
    assert_error(client.get(f"{V1}/edges/E:404"), 404, "not_found")


def test_similar(client):
    assert len(client.get(f"{V1}/diseases/MONDO:1/similar").json()) == 3
    assert len(client.get(f"{V1}/diseases/MONDO:1/similar", params={"limit": 1}).json()) == 1


def test_views(client):
    assert client.get(f"{V1}/diseases/MONDO:1/action-view").json()["headline"] == "h"
    assert_error(client.get(f"{V1}/diseases/MONDO:2/action-view"), 404, "not_found")
    assert client.get(f"{V1}/mechanisms/GO:1/view").status_code == 200


def test_paths(client):
    assert len(client.get(f"{V1}/paths", params={"from": "MONDO:1"}).json()) == 1
    assert client.get(f"{V1}/paths", params={"from": "MONDO:1", "kind": "asset"}).json() == []
    assert_error(client.get(f"{V1}/paths", params={"from": "MONDO:1", "kind": "nope"}), 422, "bad_request")
    assert_error(client.get(f"{V1}/paths"), 422, "bad_request")


def test_clusters(client):
    assert client.get(f"{V1}/clusters").json()[0]["id"] == "CL:x"
    assert client.get(f"{V1}/clusters/CL:x").json()["cluster"]["id"] == "CL:x"
    assert_error(client.get(f"{V1}/clusters/CL:nope"), 404, "not_found")


def test_explain_and_rate_limit(client):
    body = {"edge_ids": ["E:1"], "audience": "family"}
    r = client.post(f"{V1}/explain", json=body)
    assert r.status_code == 200 and r.json()["steps"][0]["edge_id"] == "E:1"
    assert r.headers["cache-control"] == "no-store"
    assert_error(client.post(f"{V1}/explain", json={"edge_ids": [], "audience": "family"}), 422, "bad_request")
    assert_error(client.post(f"{V1}/explain", json={"edge_ids": ["E:1"], "audience": "kid"}), 422, "bad_request")
    r = client.post(f"{V1}/explain", json=body)  # 4th hit in this minute, limit is 3
    assert_error(r, 429, "rate_limited")
    assert r.headers["retry-after"] == "60"


def test_outreach(client):
    r = client.post(f"{V1}/outreach-draft", json={"disease_id": "MONDO:1", "target_id": "ORG:x", "edge_ids": ["E:1"]})
    assert r.status_code == 200 and r.json()["citations"][0]["n"] == 1


def test_gap_search_job(client):
    r = client.post(f"{V1}/gap-search", json={"disease_id": "MONDO:1"})
    assert r.status_code == 202
    job_id = r.json()["job_id"]
    assert r.headers["location"] == f"/api/v1/jobs/{job_id}"
    j = client.get(f"{V1}/jobs/{job_id}").json()
    assert j["job_id"] == job_id and j["state"] in {"queued", "running", "done", "failed"}


def test_submissions(client):
    r = client.post(f"{V1}/submissions", json={"node_id": None, "kind": "missing_group", "url": None,
                                               "note": "There is a group in Norway", "contact": None})
    assert r.status_code == 201 and len(r.json()["id"]) == 36
    assert_error(client.post(f"{V1}/submissions", json={"kind": "other", "note": ""}), 422, "bad_request")


def test_cors(client):
    ok = client.options(f"{V1}/meta", headers={"Origin": "https://my-app.lovable.app",
                                               "Access-Control-Request-Method": "GET"})
    assert ok.headers.get("access-control-allow-origin") == "https://my-app.lovable.app"
    local = client.get(f"{V1}/meta", headers={"Origin": "http://localhost:5173"})
    assert local.headers.get("access-control-allow-origin") == "http://localhost:5173"
    assert "x-served-by" in local.headers.get("access-control-expose-headers", "").lower()
    bad = client.get(f"{V1}/meta", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in bad.headers


def test_cors_regex():
    import re

    from atlas_api.main import cors_regex
    rx = re.compile(cors_regex("https://*.lovable.app,https://atlas.example.org"))
    assert rx.match("https://a.b.lovable.app") and rx.match("https://atlas.example.org")
    assert not rx.match("https://lovable.app.evil.com") and not rx.match("http://x.lovable.app")


def test_explain_llm_path_is_grounded(fixtures_dir, monkeypatch):
    """No explain fixture + an API key -> LLM path; invented edge ids are dropped and
    status/confidence come from the DB edge, not from the model."""
    from fastapi.testclient import TestClient

    from atlas_api import ai
    from atlas_api.config import Settings
    from atlas_api.main import create_app

    (fixtures_dir / "explain.json").unlink()
    seen = {}

    async def fake_parse(self, system, user, schema):
        seen["edge_ids"] = [e["edge_id"] for e in user["edges"]]
        return ai._Explanation(headline="h", uncertainties=["u"], what_to_check_next="w",
                               steps=[ai._Step(text="real", edge_id="E:1"), ai._Step(text="made up", edge_id="E:999")])

    monkeypatch.setattr(ai.AI, "_parse", fake_parse)
    s = Settings(_env_file=None, data_mode="fixtures", fixtures_dir=fixtures_dir, openai_api_key="sk-test",
                 redis_url=None, database_url=None)
    with TestClient(create_app(s)) as c:
        r = c.post("/api/v1/explain", json={"edge_ids": ["E:1"], "audience": "expert"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert seen["edge_ids"] == ["E:1"]
        assert body["steps"] == [{"text": "real", "edge_id": "E:1", "status": "curated", "confidence": 0.9}]
        assert body["cached"] is False
        assert_error(c.post("/api/v1/explain", json={"edge_ids": ["E:404"], "audience": "expert"}), 404, "not_found")


def test_explain_without_key_is_503(fixtures_dir):
    from fastapi.testclient import TestClient

    from atlas_api.config import Settings
    from atlas_api.main import create_app

    (fixtures_dir / "explain.json").unlink()
    s = Settings(_env_file=None, data_mode="fixtures", fixtures_dir=fixtures_dir, openai_api_key=None,
                 redis_url=None, database_url=None)
    with TestClient(create_app(s)) as c:
        assert_error(c.post("/api/v1/explain", json={"edge_ids": ["E:1"], "audience": "family"}),
                     503, "upstream_unavailable")
