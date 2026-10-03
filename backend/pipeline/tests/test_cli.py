"""CLI wiring: `extract --batch` reaches extract.run, `all` passes DATABASE_URL to load."""
from typer.testing import CliRunner

from atlas_pipeline import cli


def _stub(monkeypatch):
    import atlas_pipeline.analytics as an
    from atlas_pipeline import extract, ingest, load, reconcile, views
    calls = {}
    monkeypatch.setattr(ingest, "run", lambda *a, **k: None)
    monkeypatch.setattr(extract, "run", lambda **k: calls.setdefault("extract", k))
    monkeypatch.setattr(reconcile, "run", lambda: None)
    monkeypatch.setattr(an, "run", lambda: None)
    monkeypatch.setattr(views, "run", lambda: None)
    monkeypatch.setattr(load, "run", lambda url, **k: calls.setdefault("load", url))
    return calls


def test_extract_batch(monkeypatch):
    calls = _stub(monkeypatch)
    r = CliRunner().invoke(cli.app, ["extract", "--batch", "--limit", "5"])
    assert r.exit_code == 0, r.output
    assert calls["extract"] == {"limit": 5, "concurrency": 4, "batch": True}


def test_all_uses_database_url(monkeypatch):
    calls = _stub(monkeypatch)
    r = CliRunner().invoke(cli.app, ["all"], env={"DATABASE_URL": "postgresql://x@localhost/y"})
    assert r.exit_code == 0, r.output
    assert calls["load"] == "postgresql://x@localhost/y"
