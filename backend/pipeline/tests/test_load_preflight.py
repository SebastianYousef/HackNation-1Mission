"""load refuses to wipe views/paths/clusters or to load ones older than their inputs, unless --force."""
import os

import pytest

from atlas_pipeline import load
from atlas_pipeline.store import write_json, write_jsonl

ROW = [{"id": "x"}]


def _age(interim, name, t):
    os.utime(interim / name, ns=(t, t))


def _fresh(interim):
    """Files in the order a real run writes them: ingest -> analytics -> views."""
    write_jsonl("curated.nodes.jsonl", ROW)
    write_jsonl("curated.edges.jsonl", ROW)
    write_json("slice", {"focus": []})
    write_json("coverage_pubmed", {})
    for n in ("clusters.jsonl", "cluster_members.jsonl", "overlap.jsonl", "paths.jsonl", "views.jsonl"):
        write_jsonl(n, ROW)
    t = 1_700_000_000_000_000_000
    for i, n in enumerate(("curated.nodes.jsonl", "curated.edges.jsonl", "slice.json", "coverage_pubmed.json",
                           "clusters.jsonl", "cluster_members.jsonl", "overlap.jsonl", "paths.jsonl",
                           "views.jsonl")):
        _age(interim, n, t + i * 10**9)
    return t


def test_fresh_outputs_pass(interim):
    _fresh(interim)
    assert load.preflight(ROW, ROW, ROW) == []


@pytest.mark.parametrize("name", ["views.jsonl", "paths.jsonl", "clusters.jsonl"])
def test_missing_or_empty_output_is_refused(interim, name):
    _fresh(interim)
    rows = {n: ([] if n == name else ROW) for n in ("clusters.jsonl", "paths.jsonl", "views.jsonl")}
    args = rows["clusters.jsonl"], rows["paths.jsonl"], rows["views.jsonl"]
    what = name.removesuffix("s.jsonl")
    assert f"{name} is empty: loading would delete every {what} in the database" in load.preflight(*args)
    (interim / name).unlink()
    assert f"{name} is missing: loading would delete every {what} in the database" in load.preflight(*args)


def test_views_older_than_analytics_are_stale(interim):
    t = _fresh(interim)
    _age(interim, "paths.jsonl", t + 100 * 10**9)  # analytics re-run, views not
    assert load.preflight(ROW, ROW, ROW) == ["views.jsonl is older than paths.jsonl: stale, re-run "
                                             "`atlas-pipeline views`"]


def test_ingest_without_analytics_is_stale(interim):
    t = _fresh(interim)
    _age(interim, "curated.edges.jsonl", t + 100 * 10**9)  # `ingest curated && load` (deploy order broken)
    probs = load.preflight(ROW, ROW, ROW)
    assert "paths.jsonl is older than curated.edges.jsonl: stale, re-run `atlas-pipeline analytics` and `views`" \
        in probs
    assert {p.split()[0] for p in probs} == {"clusters.jsonl", "paths.jsonl", "views.jsonl"}


def test_run_refuses_without_force_and_proceeds_with_it(interim, caplog):
    with pytest.raises(SystemExit, match="load refused: 3 problem"):
        load.run(None, dry_run=True)
    load.run(None, dry_run=True, force=True)  # empty graph validates; dry run writes nothing
    assert "views.jsonl is missing" in caplog.text
