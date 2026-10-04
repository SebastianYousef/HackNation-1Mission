"""The researcher-studies tests again, against Postgres (table researcher_studies).

Skipped unless TEST_DATABASE_URL is set (see test_db_sql.py). Applies every migration, then
empties researcher_studies before each test: point it at a scratch database only.
"""
from __future__ import annotations

import os
from pathlib import Path

import psycopg
import pytest
from fastapi.testclient import TestClient

import test_researcher_studies as t
from atlas_api.config import Settings
from atlas_api.main import create_app

URL = os.environ.get("TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(not URL, reason="TEST_DATABASE_URL not set")
MIGRATIONS = sorted((Path(__file__).resolve().parents[2] / "db" / "migrations").glob("*.sql"))


@pytest.fixture
def client(fixtures_dir: Path):
    with psycopg.connect(URL, autocommit=True) as conn:
        for m in MIGRATIONS:
            conn.execute(m.read_text())
        conn.execute("truncate researcher_studies")
    # nodes come from the fixtures, researcher studies from Postgres
    settings = Settings(_env_file=None, data_mode="fixtures", db_endpoints="researcher_studies", database_url=URL,
                        fixtures_dir=fixtures_dir, instance_id="test-db", redis_url=None, ai_rate_limit_per_minute=100)
    with TestClient(create_app(settings)) as c:
        yield c


def test_create_read_list_db(client):
    t.test_create_read_list(client)


def test_update_db(client):
    t.test_update_needs_the_token_and_records_history(client)


def test_events_db(client):
    t.test_events_are_anonymous_counts(client)


def test_validation_db(client):
    t.test_validation(client)


def test_timestamps_and_like_escaping_db(client):
    c = t.create(client)
    s = c["study"]
    assert s["created_at"].endswith("Z") and len(s["created_at"]) == 20
    assert client.get(f"{t.V1}/researcher-studies", params={"q": "%"}).json() == []
    assert client.get(f"{t.V1}/researcher-studies", params={"q": "_"}).json() == []
    with psycopg.connect(URL) as conn:
        h = conn.execute("select token_hash from researcher_studies where id = %s", (c["id"],)).fetchone()[0]
    assert c["edit_token"] not in h and len(h) == 64
