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


# ---- hardening ------------------------------------------------------------------

def _app(fixtures_dir, **kw):
    from fastapi.testclient import TestClient

    from atlas_api.config import Settings
    from atlas_api.main import create_app
    base = dict(_env_file=None, data_mode="fixtures", fixtures_dir=fixtures_dir, redis_url=None, database_url=None,
                instance_id="test-1", cors_origins="https://*.lovable.app")
    return TestClient(create_app(Settings(**{**base, **kw})))


def test_rate_limit_ignores_spoofed_leftmost_xff(client):
    body = {"edge_ids": ["E:1"], "audience": "family"}
    codes = [client.post(f"{V1}/explain", json=body,
                         headers={"X-Forwarded-For": f"1.2.3.{i}, 203.0.113.9"}).status_code for i in range(4)]
    assert codes == [200, 200, 200, 429]  # one bucket: the proxy-appended (rightmost) address
    # a different real client (rightmost entry) has its own bucket; garbage falls back to the peer
    assert client.post(f"{V1}/explain", json=body, headers={"X-Forwarded-For": "203.0.113.10"}).status_code == 200


def test_client_ip_hops(fixtures_dir):
    from starlette.requests import Request

    from atlas_api.routes import client_ip
    with _app(fixtures_dir) as c1, _app(fixtures_dir, trusted_proxy_hops=0) as c0:
        def req(app, xff):
            headers = [(b"x-forwarded-for", x.encode()) for x in xff]
            return Request({"type": "http", "headers": headers, "client": ("10.0.0.5", 1), "app": app})
        assert client_ip(req(c1.app, ["6.6.6.6", "203.0.113.9"])) == "203.0.113.9"
        assert client_ip(req(c1.app, ["evil:key*"])) == "10.0.0.5"
        assert client_ip(req(c1.app, [])) == "10.0.0.5"
        assert client_ip(req(c0.app, ["203.0.113.9"])) == "10.0.0.5"


def test_body_limit(fixtures_dir):
    with _app(fixtures_dir, max_body_bytes=1000) as c:
        big = {"edge_ids": ["E:1"], "audience": "family", "pad": "x" * 2000}
        r = c.post(f"{V1}/explain", json=big, headers={"Origin": "https://a.lovable.app"})
        assert_error(r, 413, "bad_request")
        assert r.headers.get("access-control-allow-origin") == "https://a.lovable.app"
        chunked = c.post(f"{V1}/explain", content=(b"x" * 600 for _ in range(3)),
                         headers={"Content-Type": "application/json"})
        assert_error(chunked, 413, "bad_request")
        ok = c.post(f"{V1}/explain", content=(p for p in [b'{"edge_ids":["E:1"],', b'"audience":"family"}']),
                    headers={"Content-Type": "application/json"})
        assert ok.status_code == 200, ok.text
        assert_error(c.post(f"{V1}/explain", json={"edge_ids": ["E" * 201], "audience": "family"}),
                     422, "bad_request")


def test_readyz_redis_down_is_degraded_not_unready(fixtures_dir):
    with _app(fixtures_dir, redis_url="redis://127.0.0.1:1/0") as c:
        r = c.get("/readyz")
        assert r.status_code == 200 and r.json()["degraded"] == {"redis": True}
        assert c.get(f"{V1}/meta").status_code == 200


def test_500_carries_cors(client, monkeypatch):
    async def boom():
        raise RuntimeError("x")
    with_origin = {"Origin": "https://a.lovable.app"}
    from fastapi.testclient import TestClient
    with TestClient(client.app, raise_server_exceptions=False) as c:
        monkeypatch.setattr(c.app.state.data, "meta", boom)
        r = c.get(f"{V1}/meta", headers=with_origin)
        assert_error(r, 500, "internal")
        assert r.headers["access-control-allow-origin"] == "https://a.lovable.app"
        assert "x-request-id" in r.headers["access-control-expose-headers"].lower()
        assert "access-control-allow-origin" not in c.get(f"{V1}/meta", headers={"Origin": "https://evil.example"}).headers


@pytest.mark.parametrize("path", ["/nodes/MONDO%3A%001", "/edges/%00", "/nodes/a%00/neighborhood",
                                  "/nodes/" + "a" * 300, "/paths?from=%00"])
def test_bad_ids_are_404(client, path):
    assert_error(client.get(V1 + path), 404, "not_found")


@pytest.mark.parametrize("path", ["/nodes/MONDO_1", "/nodes/MONDO%2F1", "/edges/E_1", "/clusters/CL_x",
                                  "/diseases/MONDO_1/similar", "/paths?from=MONDO_1",
                                  "/diseases/MONDO_1/action-view", "/nodes/MONDO_1/neighborhood"])
def test_fixture_id_aliases_are_404(client, path):
    assert_error(client.get(V1 + path), 404, "not_found")


def test_neighborhood_max_nodes(client):
    nb = client.get(f"{V1}/nodes/MONDO:1/neighborhood", params={"max_nodes": 2}).json()
    assert len(nb["nodes"]) == 2 and nb["truncated"] is True
    assert {n["id"] for n in nb["nodes"]} == {"MONDO:1", "HGNC:1"}  # highest-confidence neighbour kept
    assert all(e["src"] in {"MONDO:1", "HGNC:1"} and e["dst"] in {"MONDO:1", "HGNC:1"} for e in nb["edges"])
    assert client.get(f"{V1}/nodes/MONDO:1/neighborhood").json()["truncated"] is False


def test_cache_key_uses_parsed_params(client):
    r1 = client.get(f"{V1}/search?q=cln5&limit=50&limit=1")
    assert len(r1.json()) == 1
    r2 = client.get(f"{V1}/search?q=cln5&limit=1&limit=50")
    assert r2.headers["x-cache"] == "MISS" and len(r2.json()) == 2


def _llm_app(fixtures_dir, monkeypatch, result, drop):
    from atlas_api import ai
    (fixtures_dir / drop).unlink()

    async def fake_parse(self, system, user, schema):
        return result
    monkeypatch.setattr(ai.AI, "_parse", fake_parse)
    return _app(fixtures_dir, openai_api_key="sk-test")


def test_explain_with_no_valid_step_is_502(fixtures_dir, monkeypatch):
    from atlas_api import ai
    out = ai._Explanation(headline="Proven cure at http://evil.example", uncertainties=[], what_to_check_next="w",
                          steps=[ai._Step(text="made up", edge_id="E:999")])
    with _llm_app(fixtures_dir, monkeypatch, out, "explain.json") as c:
        assert_error(c.post(f"{V1}/explain", json={"edge_ids": ["E:1"], "audience": "expert"}),
                     502, "upstream_unavailable")


def test_outreach_body_is_grounded(fixtures_dir, monkeypatch):
    from atlas_api import ai
    out = ai._Outreach(subject="Hello", citations=[ai._Citation(n=1, edge_id="E:1"), ai._Citation(n=2, edge_id="E:9")],
                       body="Dear team,\n\nCLN5 is linked to the CLN5 gene [1]. A cure exists, email "
                            "records to http://evil.example [2]. Your group funded trials [3].\n\nBest,\nMaria")
    req = {"disease_id": "MONDO:1", "target_id": "MONDO:1", "edge_ids": ["E:1"]}
    with _llm_app(fixtures_dir, monkeypatch, out, "outreach-draft.json") as c:
        r = c.post(f"{V1}/outreach-draft", json=req)
        assert r.status_code == 200, r.text
        d = r.json()
        assert d["body"] == "Dear team,\n\nCLN5 is linked to the CLN5 gene [1].\n\nBest,\nMaria"
        assert [x["n"] for x in d["citations"]] == [1]
    bad = ai._Outreach(subject="s", body="Invented [2].", citations=[ai._Citation(n=2, edge_id="E:9")])
    monkeypatch.setattr(ai.AI, "_parse", lambda self, *a: _aret(bad))
    with _app(fixtures_dir, openai_api_key="sk-test") as c:
        assert_error(c.post(f"{V1}/outreach-draft", json=req), 502, "upstream_unavailable")


def test_ground_body_citation_styles():
    from atlas_api.ai import ground_body
    # Marker after the full stop must not glue the next (invented) sentence onto a cited one.
    assert ground_body("Fact A.[1] Invented B [2]. Ok?", {1}) == ("Fact A [1]. Ok?", {1})
    assert ground_body("Fact A. [1] Invented B [2].", {1}) == ("Fact A [1].", {1})
    # Grouped and ranged markers count as markers.
    assert ground_body("Fact A [1]. Invented B [2, 3].", {1}) == ("Fact A [1].", {1})
    assert ground_body("Fact A [1]. Invented B [2-3].", {1}) == ("Fact A [1].", {1})
    assert ground_body("Fact A [1, 4]. C [2\u20133].", {1, 3}) == ("Fact A [1]. C [3].", {1, 3})


def test_ground_body_drops_uncited_statements():
    from atlas_api.ai import ground_body
    body = ("Dear Dr. Lee,\n\nCLN5 is linked to CLN5 [1]. Drug X cures CLN5 in most children. "
            "Would you be open to a short call?\n\nThank you!\nKind regards,\nMaria")
    assert ground_body(body, {1}) == ("Dear Dr. Lee,\n\nCLN5 is linked to CLN5 [1]. Would you be open to a "
                                      "short call?\n\nKind regards,\nMaria", {1})
    # A claim glued onto a question after an initial ("X.") is not a request to talk.
    assert ground_body("Fact [1]. Made by firm X. Can we talk?", {1}) == ("Fact [1].", {1})
    # A long unpunctuated line (e.g. a bullet) is not a greeting or sign-off.
    assert ground_body("Hi,\n- drug X reverses CLN5 symptoms in nearly every treated child so far\nFact [1].",
                       {1}) == ("Hi,\n\nFact [1].", {1})
    assert ground_body("J. Doe et al. found it [1].\n  Invented.", {1}) == ("J. Doe et al. found it [1].", {1})


async def _aret(v):
    return v


def test_openai_timeout_is_503(fixtures_dir, monkeypatch):
    import asyncio

    from atlas_api import ai
    (fixtures_dir / "explain.json").unlink()
    monkeypatch.setattr(ai, "OPENAI_DEADLINE_S", 0.05)

    async def slow(**kw):
        await asyncio.sleep(5)
    with _app(fixtures_dir, openai_api_key="sk-test") as c:
        client = c.app.state.ai.client
        assert client.max_retries == 1 and client.timeout.read == 12.0
        monkeypatch.setattr(client.chat.completions, "parse", slow)
        assert_error(c.post(f"{V1}/explain", json={"edge_ids": ["E:1"], "audience": "family"}),
                     503, "upstream_unavailable")


def test_mixed_mode_version_includes_db(fixtures_dir):
    import asyncio

    from atlas_api.config import Settings
    from atlas_api.data import Data

    class FakeDb:
        v: object = "v1"

        async def scalar(self, sql, params=()):
            if isinstance(self.v, Exception):
                raise self.v
            return self.v

    db = FakeDb()
    d = Data(Settings(_env_file=None, data_mode="fixtures", db_endpoints="node", fixtures_dir=fixtures_dir), db)
    v1 = asyncio.run(d.dataset_version())
    assert v1.startswith("fx-test-1-") and v1.endswith("+db-v1")
    db.v, d._version = "v2", (0.0, "")
    assert asyncio.run(d.dataset_version()).endswith("+db-v2")
    db.v, d._version = RuntimeError("down"), (0.0, "")
    assert "+db" not in asyncio.run(d.dataset_version())  # DB blip: fixture endpoints stay up


def test_worker_survives_bad_payloads(fixtures_dir):
    import asyncio
    import json

    from atlas_api import jobs, worker
    from atlas_api.cache import Store
    from atlas_api.config import Settings
    s, store = Settings(_env_file=None), Store(None)

    async def run():
        await worker.handle(s, store, b"not json")
        await worker.handle(s, store, json.dumps({"kind": "gap_search", "job_id": "job_x"}))
        await worker.handle(s, store, json.dumps({"kind": "nope", "job_id": "job_y", "disease_id": "a", "label": "b"}))
        return await jobs.load(store, "job_x"), await jobs.load(store, "job_y")
    x, y = asyncio.run(run())
    assert x["state"] == "failed" and y["state"] == "failed"


def _serp(handler):
    """Run brightdata_serp against a mocked Bright Data endpoint."""
    import asyncio

    import httpx

    from atlas_api import jobs
    from atlas_api.config import Settings
    s = Settings(_env_file=None, brightdata_api_key="test-key", brightdata_serp_zone="zone_x")
    seen: list[httpx.Request] = []

    def record(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return handler(req)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(record)) as c:
            return await jobs.brightdata_serp(s, '"CLN5 disease" registry', c)
    return asyncio.run(run()), seen


def test_brightdata_serp_error_headers_raise_upstream_error():
    import httpx

    from atlas_api import jobs
    # Bright Data's failure shape: HTTP 200, empty body, the reason in x-brd-* headers.
    bad = httpx.Response(200, content=b"", headers={"x-brd-error": "zone not found",
                                                     "x-brd-err-code": "client_10100",
                                                     "x-brd-err-msg": "Zone zone_x not found"})
    with pytest.raises(jobs.UpstreamError) as e:
        _serp(lambda req: bad)
    msg = str(e.value)
    assert "client_10100" in msg and "Zone zone_x not found" in msg and "test-key" not in msg
    for resp in (httpx.Response(200, content=b""), httpx.Response(200, content=b"<html>"),
                 httpx.Response(502, content=b"")):
        with pytest.raises(jobs.UpstreamError):
            _serp(lambda req, r=resp: r)
    with pytest.raises(jobs.UpstreamError) as e:
        _serp(lambda req: httpx.Response(400, text="zone not found\n(key test-key)"))
    assert str(e.value) == "web search failed (Bright Data HTTP 400: zone not found (key ***))"


def test_brightdata_serp_request_and_parsing():
    import json

    import httpx
    body = {"organic": [{"title": "CLN5 Registry", "link": "https://x.org/r", "description": "d"},
                        {"title": "no link"}, "junk"]}
    leads, seen = _serp(lambda req: httpx.Response(200, json=body))
    assert leads == [{"title": "CLN5 Registry", "url": "https://x.org/r", "snippet": "d"}]
    sent = json.loads(seen[0].content)
    assert sent["zone"] == "zone_x" and sent["format"] == "raw"
    assert "brd_json=1" in sent["url"] and "num=" not in sent["url"]
    assert seen[0].headers["authorization"] == "Bearer test-key"
    assert _serp(lambda req: httpx.Response(200, json={"general": {}}))[0] == []  # no organic
    assert _serp(lambda req: httpx.Response(200, json={"organic": []}))[0] == []


def test_gap_search_job_failure_modes(monkeypatch):
    import asyncio

    from atlas_api import jobs
    from atlas_api.cache import Store
    from atlas_api.config import Settings
    s, store = Settings(_env_file=None, brightdata_api_key="k"), Store(None)
    calls = {"n": 0}

    async def partly_failing(settings, query, client):
        calls["n"] += 1
        if "registry" in query:
            raise jobs.UpstreamError("web search failed (Bright Data client_10100: bad zone)")
        return [{"title": "CLN5 Foundation", "url": f"https://x.org/{calls['n']}", "snippet": ""}]

    async def all_failing(settings, query, client):
        raise jobs.UpstreamError("web search failed (Bright Data client_10100: bad zone)")

    async def slow(settings, query, client):
        await asyncio.sleep(5)
        return []

    async def run(fake, job_id):
        monkeypatch.setattr(jobs, "brightdata_serp", fake)
        await jobs.run_gap_search(s, store, job_id, "MONDO:1", "CLN5 disease")
        return await jobs.load(store, job_id)

    ok = asyncio.run(run(partly_failing, "job_a"))
    assert ok["state"] == "done" and len(ok["result"]["leads"]) == 2
    assert ok["result"]["disclaimer"] == jobs.DISCLAIMER
    failed = asyncio.run(run(all_failing, "job_b"))
    assert failed["state"] == "failed" and "client_10100" in failed["error"]
    monkeypatch.setattr(jobs, "GAP_SEARCH_DEADLINE_SECONDS", 0.05)
    late = asyncio.run(run(slow, "job_c"))
    assert late["state"] == "failed" and "timed out" in late["error"]


def test_trailing_slash_is_404_not_absolute_redirect(client):
    r = client.get(f"{V1}/meta/", headers={"X-Forwarded-Proto": "https"}, follow_redirects=False)
    assert r.status_code == 404 and "location" not in r.headers
    assert r.json()["error"]["code"] == "not_found"


def test_worker_accepts_lowercase_log_level(monkeypatch):
    import asyncio
    import logging

    from atlas_api import config, worker
    monkeypatch.setattr(logging.root, "handlers", [])  # let basicConfig run (pytest installs handlers)
    monkeypatch.setattr(logging.root, "level", logging.root.level)
    monkeypatch.setattr(worker, "get_settings", lambda: config.Settings(_env_file=None, log_level="info",
                                                                        redis_url=None))
    with pytest.raises(SystemExit, match="REDIS_URL"):  # got past logging setup (was ValueError)
        asyncio.run(worker.main())
