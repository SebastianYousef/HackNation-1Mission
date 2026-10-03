"""Serve every file in the real contract/fixtures through the API (skipped if absent)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from atlas_api.config import Settings
from atlas_api.main import create_app

FX = Path(__file__).resolve().parents[3] / "contract" / "fixtures"
ROUTES = {"nodes": "/nodes/{}", "neighborhood": "/nodes/{}/neighborhood", "edges": "/edges/{}",
          "similar": "/diseases/{}/similar", "paths": "/paths?from={}", "clusters": "/clusters/{}",
          "action-view": "/diseases/{}/action-view", "mechanism-view": "/mechanisms/{}/view"}


def _cases() -> list[str]:
    if not FX.is_dir():
        return []
    out = ["/meta", "/clusters"]
    for folder, tmpl in ROUTES.items():
        for f in sorted((FX / folder).glob("*.json")):
            data = json.loads(f.read_text())
            real_id = {"nodes": lambda d: d["node"]["id"], "neighborhood": lambda d: d["center"],
                       "edges": lambda d: d["edge"]["id"], "clusters": lambda d: d["cluster"]["id"],
                       "action-view": lambda d: d["disease"]["id"],
                       "mechanism-view": lambda d: d["mechanism"]["id"]}.get(folder)
            if real_id is None:  # list fixtures: recover the ':' from the file name heuristically
                ident = f.stem.replace("_", ":", 1)
            else:
                ident = real_id(data)
            out.append(tmpl.format(ident))
    return out


@pytest.mark.skipif(not FX.is_dir(), reason="contract/fixtures not present")
@pytest.mark.parametrize("path", _cases())
def test_fixture_served(path: str) -> None:
    settings = Settings(_env_file=None, data_mode="fixtures", fixtures_dir=FX, redis_url=None)
    with TestClient(create_app(settings)) as c:
        r = c.get("/api/v1" + path)
        assert r.status_code == 200, (path, r.text[:200])
