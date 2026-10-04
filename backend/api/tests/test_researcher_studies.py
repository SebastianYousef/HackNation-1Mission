"""Researcher-published studies (contract v1.1.0) in fixtures mode (in-process store)."""
from __future__ import annotations

import copy
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from atlas_api.config import Settings
from atlas_api.main import create_app
from conftest import D1

V1 = "/api/v1"
DRAFT = {
    "title": "Natural history of CLN5 disease in Sweden",
    "summary": "We follow children with CLN5 disease for three years to learn how the disease changes.",
    "research_focus": "Disease course", "condition_ids": [D1["id"]], "condition_text": None,
    "kind": "natural_history", "status": "recruiting", "institution": "Karolinska Institutet", "team": "NCL team",
    "locations": [{"facility": "Astrid Lindgren Children's Hospital", "city": "Stockholm", "country": "Sweden"}],
    "start_date": "2026-11", "end_date": "2029-12",
    "eligibility": {"min_age": 2, "max_age": 12, "sex": "all", "criteria": "Confirmed CLN5 diagnosis."},
    "screening": [{"id": "age", "text": "How old is the person who would take part?", "type": "number", "min": 2, "max": 12, "unit": "years"},
                  {"id": "dx", "text": "Has a doctor confirmed CLN5 disease?", "type": "yes_no", "accept": ["yes"]},
                  {"id": "country", "text": "Where do you live?", "type": "choice", "options": ["Sweden", "Norway", "Other"],
                   "accept": ["Sweden", "Norway"], "required": False}],
    "contact": {"name": "Study nurse", "email": "ncl-study@example.org", "url": None},
    "registry_id": None, "published": True,
}


@pytest.fixture
def client(fixtures_dir: Path):
    """Own client: these tests write more often than the shared fixture's 3/min limit allows."""
    settings = Settings(_env_file=None, data_mode="fixtures", fixtures_dir=fixtures_dir, instance_id="test-1",
                        redis_url=None, database_url=None, ai_rate_limit_per_minute=100)
    with TestClient(create_app(settings)) as c:
        yield c


def create(client, draft=DRAFT):
    r = client.post(f"{V1}/researcher-studies", json={"study": draft})
    assert r.status_code == 201, r.text
    return r.json()


def test_create_read_list(client):
    c = create(client)
    sid, tok, s = c["id"], c["edit_token"], c["study"]
    assert sid.startswith("RS:") and len(tok) >= 20
    assert s["source"] == "researcher" and s["verification"] == "researcher_submitted"
    assert s["conditions"] == [D1] and s["update_history"][0]["fields"] == ["created"]
    assert "token_hash" not in s and "counts" not in s and "edit_token" not in s
    r = client.get(f"{V1}/researcher-studies/{sid}")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store" and r.json() == s
    assert [x["id"] for x in client.get(f"{V1}/researcher-studies", params={"condition": "MONDO:1"}).json()] == [sid]
    assert client.get(f"{V1}/researcher-studies", params={"condition": "MONDO:2"}).json() == []
    assert [x["id"] for x in client.get(f"{V1}/researcher-studies", params={"q": "karolinska"}).json()] == [sid]
    assert client.get(f"{V1}/researcher-studies", params={"q": "nothing like this"}).json() == []


def test_update_needs_the_token_and_records_history(client):
    c = create(client)
    sid = c["id"]
    d = copy.deepcopy(DRAFT) | {"status": "active_not_recruiting"}
    bad = client.post(f"{V1}/researcher-studies/{sid}/update", json={"edit_token": "x" * 30, "study": d})
    assert bad.status_code == 404
    r = client.post(f"{V1}/researcher-studies/{sid}/update", json={"edit_token": c["edit_token"], "study": d})
    assert r.status_code == 200 and r.json()["status"] == "active_not_recruiting"
    assert r.json()["update_history"][-1]["fields"] == ["status"]
    # unpublish: gone from public reads, still manageable
    d["published"] = False
    client.post(f"{V1}/researcher-studies/{sid}/update", json={"edit_token": c["edit_token"], "study": d})
    assert client.get(f"{V1}/researcher-studies/{sid}").status_code == 404
    assert client.get(f"{V1}/researcher-studies").json() == []
    m = client.post(f"{V1}/researcher-studies/{sid}/manage", json={"edit_token": c["edit_token"]})
    assert m.status_code == 200 and m.json()["study"]["published"] is False


def test_events_are_anonymous_counts(client):
    c = create(client)
    sid = c["id"]
    for kind in ("view", "view", "screening_completed", "potential_match"):
        assert client.post(f"{V1}/researcher-studies/{sid}/events", json={"kind": kind}).status_code == 204
    assert client.post(f"{V1}/researcher-studies/{sid}/events", json={"kind": "answers"}).status_code == 422
    stats = client.post(f"{V1}/researcher-studies/{sid}/manage", json={"edit_token": c["edit_token"]}).json()["stats"]
    assert stats == {"view": 2, "screening_started": 0, "screening_completed": 1, "potential_match": 1, "contact_clicked": 0}
    assert client.post(f"{V1}/researcher-studies/RS:0000000000/events", json={"kind": "view"}).status_code == 404


def test_validation(client):
    def post(**over):
        return client.post(f"{V1}/researcher-studies", json={"study": copy.deepcopy(DRAFT) | over})
    assert post(condition_ids=["MONDO:999"]).status_code == 400            # not in the Atlas
    assert post(condition_ids=["HGNC:1"]).status_code == 400               # a gene is not a condition
    assert post(condition_ids=[], condition_text=None).status_code == 422
    assert post(condition_ids=[], condition_text="CLN5 disease").status_code == 201
    assert post(contact={"name": "x", "email": None, "url": None}).status_code == 422
    assert post(contact={"email": "not-an-email"}).status_code == 422
    assert post(eligibility={"min_age": 10, "max_age": 2}).status_code == 422
    assert post(start_date="2027-01", end_date="2026-01").status_code == 422
    assert post(kind="phase9").status_code == 422
    assert post(screening=[{"id": "a", "text": "Pick one", "type": "choice", "options": ["x"], "accept": ["x"]}]).status_code == 422
    assert post(screening=[{"id": "a", "text": "Yes?", "type": "yes_no", "accept": ["maybe"]}]).status_code == 422
    assert post(screening=[DRAFT["screening"][0], DRAFT["screening"][0]]).status_code == 422
    assert client.get(f"{V1}/researcher-studies/../../etc").status_code == 404
    assert client.get(f"{V1}/researcher-studies/RS:0000000000").status_code == 404
