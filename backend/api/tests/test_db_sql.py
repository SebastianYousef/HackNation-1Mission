"""Migrations + api_* SQL smoke test against a real Postgres.

Skipped unless TEST_DATABASE_URL is set, e.g.
    createdb atlas_test
    TEST_DATABASE_URL=postgresql://postgres@localhost:55432/atlas_test make api-test

Everything runs in ONE transaction that is rolled back at the end, so the target
database is left as it was (it may even hold a real dataset; the seed rows use
TEST: ids). Inside it, every backend/db/migrations/*.sql file is applied in order,
twice (migrations must be idempotent), a tiny graph is seeded, and every api_*
function is called the way atlas_api/data.py calls it. The server needs the
pg_trgm and vector extensions available.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL not set")

MIGRATIONS = sorted((Path(__file__).resolve().parents[2] / "db" / "migrations").glob("*.sql"))

D1, D2, G1, P1, M1 = "TEST:D1", "TEST:D2", "TEST:G1", "TEST:P1", "TEST:M1"
E_GENE, E_P1, E_P2, E_SIM = "E:test-gene", "E:test-d1p1", "E:test-d2p1", "E:test-sim"

SEED = f"""
insert into nodes (id, type, label, plain_summary, synonyms, attrs) values
  ('{D1}', 'disease',   'Testoid lipofuscinosis 1', 'A test disease.', '{{TL1}}', '{{}}'),
  ('{D2}', 'disease',   'Testoid lipofuscinosis 2', null, '{{}}', '{{}}'),
  ('{G1}', 'gene',      'TSTG1', null, '{{}}', '{{}}'),
  ('{P1}', 'phenotype', 'Testoid seizure', null, '{{}}', '{{"ic": 3.2}}'),
  ('{M1}', 'mechanism', 'Testoid lysosomal mechanism', null, '{{}}', '{{}}');
insert into node_names (node_id, name, kind)
  select id, label, 'label' from nodes where id like 'TEST:%';
insert into node_names (node_id, name, kind) values ('{D1}', 'TL1', 'abbreviation');
insert into edges (id, src, dst, type, status, confidence, score, attrs, support_count, sources) values
  ('{E_GENE}', '{G1}', '{D1}', 'gene_associated_with_disease', 'curated', 0.9, null, '{{}}', 1, '{{HPO}}'),
  ('{E_P1}',   '{D1}', '{P1}', 'disease_has_phenotype',        'curated', 0.9, null, '{{}}', 1, '{{HPO}}'),
  ('{E_P2}',   '{D2}', '{P1}', 'disease_has_phenotype',        'curated', 0.8, null, '{{}}', 1, '{{HPO}}'),
  ('{E_SIM}',  '{D1}', '{D2}', 'disease_similar_to',           'inferred', 0.6, 0.8,
   '{{"components": {{"phenotype": 0.8}}, "caution": "test caution"}}', 1, '{{Atlas analytics}}');
insert into evidence (id, edge_id, stance, source_type, source_name, method, published_at, retrieved_at) values
  ('V:test-1', '{E_GENE}', 'supports', 'database', 'HPO', 'curated', '2020-05-01', '2026-10-03 22:06:01.5+02'),
  ('V:test-2', '{E_GENE}', 'context',  'database', 'HPO', 'curated', null,         '2026-10-03 00:00:00+00'),
  ('V:test-3', '{E_P1}',   'supports', 'database', 'HPO', 'curated', null,         '2026-10-03 00:00:00+00'),
  ('V:test-4', '{E_P2}',   'supports', 'database', 'HPO', 'curated', null,         '2026-10-03 00:00:00+00'),
  ('V:test-5', '{E_SIM}',  'supports', 'computed', 'Atlas analytics', 'algorithm:test', null, '2026-10-03 00:00:00+00');
insert into clusters (id, label, method, size, attrs) values
  ('CL:test', 'Test cluster', 'test', 2, '{{"top_phenotypes": ["{P1}"], "top_mechanisms": ["{M1}"]}}');
insert into cluster_members (cluster_id, node_id, membership) values ('CL:test', '{D1}', 1.0), ('CL:test', '{D2}', 0.5);
insert into paths (id, from_id, to_id, kind, title, node_ids, edge_ids, score, weakest_status, min_confidence) values
  ('P:test', '{D1}', '{D2}', 'related_disease', 'test path', '{{{D1},{P1},{D2}}}', '{{{E_P1},{E_P2}}}', 0.5, 'curated', 0.8),
  ('P:test-dangling', '{D1}', '{D2}', 'related_disease', 'ids gone', '{{{D1},TEST:gone,{D2}}}', '{{E:gone,E:gone2}}', 0.1, null, null);
insert into views (kind, key, payload) values
  ('action', '{D1}', '{{"disease": {{"id": "{D1}"}}}}'),
  ('mechanism', '{M1}', '{{"mechanism": {{"id": "{M1}"}}}}');
"""

NODE_BRIEF_KEYS = {"id", "type", "subtype", "label", "summary"}
EDGE_KEYS = {"id", "src", "dst", "type", "label", "status", "confidence", "score", "support_count",
             "contradict_count", "sources", "attrs"}
EVIDENCE_KEYS = {"id", "stance", "source_type", "source_name", "source_ref", "url", "quote", "method",
                 "published_at", "retrieved_at"}


@pytest.fixture(scope="module")
def cur():
    import psycopg

    with psycopg.connect(URL, prepare_threshold=None) as conn:  # not autocommit: one transaction
        try:
            with conn.cursor() as c:
                for _ in range(2):
                    for f in MIGRATIONS:
                        c.execute(f.read_text())
                # retrieved_at must not follow the session zone (migration 0006)
                c.execute("set local timezone = 'America/Los_Angeles'")
                c.execute(SEED)
                yield c
        finally:
            conn.rollback()


def call(cur, sql: str, *params):
    cur.execute(sql, params)
    return cur.fetchone()[0]


def test_migrations_found():
    assert len(MIGRATIONS) >= 6 and MIGRATIONS[0].name.endswith("_schema.sql")


def test_every_api_function_is_smoked(cur):
    cur.execute("select distinct proname from pg_proc where proname like 'api\\_%' "
                "and pronamespace = (select oid from pg_namespace where nspname = current_schema())")
    assert {r[0] for r in cur.fetchall()} == set(SMOKED)


def test_helpers_are_stable(cur):
    cur.execute("select proname, provolatile from pg_proc where proname in "
                "('_node_brief','_node_full','_edge_json','_evidence_json','_shared') "
                "and pronamespace = (select oid from pg_namespace where nspname = current_schema())")
    assert dict(cur.fetchall()) == dict.fromkeys(
        ["_node_brief", "_node_full", "_edge_json", "_evidence_json", "_shared"], "s")


def smoke_meta(cur):
    m = call(cur, "select api_meta()")
    assert set(m) == {"contract_version", "dataset", "counts", "sources"}
    assert m["counts"]["nodes"]["disease"] >= 2 and m["counts"]["edges"] >= 4
    assert "HPO" in m["sources"]


def smoke_search(cur):
    sql = "select api_search(%s::text, %s::text[], %s::int)"
    hits = call(cur, sql, "Testoid lipofuscinosis 1", None, 20)
    assert hits[0]["id"] == D1 and hits[0]["score"] == 1.0 and hits[0]["match_kind"] == "label"
    assert set(hits[0]) == NODE_BRIEF_KEYS | {"matched_name", "match_kind", "score"}
    assert {h["id"] for h in call(cur, sql, "testoid", ["disease"], 50)} >= {D1, D2}
    assert all(h["type"] == "gene" for h in call(cur, sql, "testoid", ["gene"], 50))
    assert call(cur, sql, "t", None, 20) == []
    assert call(cur, sql, "%%", None, 20) == []  # LIKE metacharacters are escaped


def smoke_node(cur):
    n = call(cur, "select api_node(%s::text)", D1)
    assert set(n) == {"node", "degree", "clusters", "has_action_view", "has_mechanism_view"}
    assert n["node"]["synonyms"] == ["TL1"] and n["degree"] == {"gene": 1, "phenotype": 1, "disease": 1}
    assert n["clusters"][0]["id"] == "CL:test" and n["has_action_view"] is True and n["has_mechanism_view"] is False
    assert call(cur, "select api_node(%s::text)", "TEST:none") is None


def smoke_neighborhood(cur):
    sql = "select api_neighborhood(%s::text, %s::int, %s::text[], %s::text[], %s::real, %s::int)"
    nb = call(cur, sql, D1, 1, None, None, 0, 60)
    assert nb["center"] == D1 and nb["truncated"] is False
    assert {x["id"] for x in nb["nodes"]} == {D1, D2, G1, P1}
    assert {e["id"] for e in nb["edges"]} == {E_GENE, E_P1, E_P2, E_SIM}
    assert set(nb["edges"][0]) == EDGE_KEYS
    cur_only = call(cur, sql, D1, 2, None, ["curated"], 0, 60)
    assert E_SIM not in {e["id"] for e in cur_only["edges"]}
    assert call(cur, sql, D1, 1, None, None, 0, 2)["truncated"] is True
    assert call(cur, sql, "TEST:none", 1, None, None, 0, 60) is None


def smoke_edge(cur):
    e = call(cur, "select api_edge(%s::text)", E_GENE)
    assert e["edge"]["id"] == E_GENE and e["source"]["id"] == G1 and e["target"]["id"] == D1
    assert [v["id"] for v in e["supporting"]] == ["V:test-1"] and e["contradicting"] == []
    v = e["supporting"][0]
    assert set(v) == EVIDENCE_KEYS and v["published_at"] == "2020-05-01"
    # UTC, whatever the session TimeZone (set to America/Los_Angeles above)
    assert v["retrieved_at"] == "2026-10-03T20:06:01.5+00:00"
    assert e["context"][0]["retrieved_at"] == "2026-10-03T00:00:00+00:00"
    assert call(cur, "select api_edge(%s::text)", "E:none") is None


def smoke_similar(cur):
    s = call(cur, "select api_similar_diseases(%s::text, %s::int)", D1, 10)
    assert len(s) == 1 and s[0]["disease"]["id"] == D2 and s[0]["edge_id"] == E_SIM
    assert s[0]["caution"] == "test caution" and s[0]["same_cluster"] is True
    assert [p["id"] for p in s[0]["shared"]["phenotypes"]] == [P1] and s[0]["shared"]["genes"] == []
    assert call(cur, "select api_similar_diseases(%s::text, %s::int)", G1, 10) is None  # not a disease


def smoke_paths(cur):
    sql = "select api_paths(%s::text, %s::text, %s::text, %s::int)"
    ps = call(cur, sql, D1, None, None, 10)
    assert [p["id"] for p in ps] == ["P:test", "P:test-dangling"]
    assert [n["id"] for n in ps[0]["nodes"]] == [D1, P1, D2] and [e["id"] for e in ps[0]["edges"]] == [E_P1, E_P2]
    assert ps[0]["weakest_status"] == "curated" and ps[0]["min_confidence"] == pytest.approx(0.8)
    # ids that no longer resolve: arrays are never null; the pre-0004 fallback has nothing to go on
    assert [n["id"] for n in ps[1]["nodes"]] == [D1, D2] and ps[1]["edges"] == []
    assert ps[1]["weakest_status"] is None and ps[1]["min_confidence"] is None
    assert call(cur, sql, D1, D1, None, 10) == []
    assert call(cur, sql, "TEST:none", None, None, 10) is None


def smoke_clusters(cur):
    cs = call(cur, "select api_clusters()")
    assert "CL:test" in {c["id"] for c in cs}


def smoke_cluster(cur):
    c = call(cur, "select api_cluster(%s::text)", "CL:test")
    assert c["cluster"]["size"] == 2 and [m["id"] for m in c["members"]] == [D1, D2]
    assert [p["id"] for p in c["top_phenotypes"]] == [P1] and [m["id"] for m in c["top_mechanisms"]] == [M1]
    assert call(cur, "select api_cluster(%s::text)", "CL:none") is None


def smoke_action_view(cur):
    assert call(cur, "select api_action_view(%s::text)", D1) == {"disease": {"id": D1}}
    assert call(cur, "select api_action_view(%s::text)", D2) is None


def smoke_mechanism_view(cur):
    assert call(cur, "select api_mechanism_view(%s::text)", M1) == {"mechanism": {"id": M1}}
    assert call(cur, "select api_mechanism_view(%s::text)", D1) is None


SMOKED = {
    "api_meta": smoke_meta,
    "api_search": smoke_search,
    "api_node": smoke_node,
    "api_neighborhood": smoke_neighborhood,
    "api_edge": smoke_edge,
    "api_similar_diseases": smoke_similar,
    "api_paths": smoke_paths,
    "api_clusters": smoke_clusters,
    "api_cluster": smoke_cluster,
    "api_action_view": smoke_action_view,
    "api_mechanism_view": smoke_mechanism_view,
}


@pytest.mark.parametrize("name", sorted(SMOKED))
def test_api_function(cur, name):
    with cur.connection.transaction(force_rollback=True):  # savepoint: a failure doesn't abort the rest
        SMOKED[name](cur)
