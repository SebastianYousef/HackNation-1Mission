"""analytics.paths + views on a real-data-shaped NCL graph: a counterexample's approved therapy never reaches CLN5
through its trials (even when every CT.gov condition link is 'hypothesis'), the soluble-deficiency route to CLN1
beats equal-score SCMAS-storage routes to counterexamples, and an approved-therapy caution only sits on routes to
the disease that has the therapy."""
from types import SimpleNamespace

from atlas_pipeline import views
from atlas_pipeline.analytics import paths
from atlas_pipeline.graph import load_graph
from atlas_pipeline.store import GraphWriter, write_json

D5, D2, D1, D6, P = "MONDO:5", "MONDO:2", "MONDO:1", "MONDO:6", "MONDO:9"
SCMAS, SOL = "ATLAS:mech-scmas-lysosomal-storage", "ATLAS:mech-soluble-lysosomal-deficiency"
ERT = "ATLAS:int-ert2"
DB = dict(source_type="database", source_name="DB", source_ref="DB:1")
CT = dict(source_type="trial_registry", source_name="ClinicalTrials.gov")


def _q(text):
    return dict(source_type="publication", source_name="TEST", source_ref=f"PMID:{abs(hash(text)) % 10**6}",
                quote=text, method="manual")


def _seed() -> None:
    w = GraphWriter("mondo")
    for d, lab in ((D5, "CLN5"), (D2, "CLN2"), (D1, "CLN1"), (D6, "CLN6A"), (P, "NCL")):
        w.node(id=d, type="disease", label=lab)
    for i, d in enumerate((D5, D2, D1, D6)):
        w.node(id=f"HGNC:{i}", type="gene", label=f"G{i}")
        w.edge("gene_associated_with_disease", f"HGNC:{i}", d, status="curated", evidence=DB)
        w.edge("disease_subtype_of", d, P, status="curated", evidence=DB)
    w.close()

    c = GraphWriter("curated")
    c.node(id=SOL, type="mechanism", label="soluble deficiency")
    c.node(id=SCMAS, type="mechanism", label="SCMAS storage")
    for d, ms in ((D5, (SOL, SCMAS)), (D2, (SOL, SCMAS)), (D1, (SOL,)), (D6, (SCMAS,))):
        for m in ms:
            c.edge("disease_involves_mechanism", d, m, status="literature", evidence=_q(f"{d} {m}"))
    c.node(id=ERT, type="intervention", label="ERT-2")
    c.edge("intervention_treats_disease", ERT, D2, status="literature", attrs={"approval": "approved"},
           evidence=_q("ERT-2 is approved for CLN2"))
    c.node(id="ASSET:hyp", type="asset", subtype="animal_model", label="unconfirmed CLN5 model")
    c.edge("asset_covers_disease", "ASSET:hyp", D5, status="hypothesis",
           evidence=dict(source_type="web", source_name="TEST", source_ref="https://example.org", quote=None))
    c.close()

    t = GraphWriter("clinicaltrials")
    t.node(id="NCTX", type="trial", subtype="interventional", label="ERT-2 extension")
    t.node(id="NCTO", type="trial", subtype="observational", label="ERT-2 observational study")
    t.node(id="NCTF", type="trial", subtype="interventional", label="ERT-2 in NCL")
    for nct in ("NCTX", "NCTO", "NCTF"):
        t.edge("trial_tests_intervention", nct, ERT, status="curated", evidence=CT | {"source_ref": nct})
    # after the CT.gov honesty change every condition link of the ERT-2 trial is 'hypothesis'
    t.edge("trial_studies_disease", "NCTX", D2, status="hypothesis",
           evidence=CT | dict(source_ref="NCTX", quote="CLN2", method="algorithm:ctgov_condition_match:synonym"))
    # filed under the umbrella only: names no subtype, but it tests CLN2's therapy
    t.edge("trial_studies_disease", "NCTF", P, status="literature",
           evidence=CT | dict(source_ref="NCTF", quote="Condition: NCL", method="algorithm:ctgov_condition_match:label"))
    t.close()

    a = GraphWriter("analytics")
    comp = {"phenotype": 0.5, "mechanism": 0.5, "gene": 0.0}
    for o, attrs in ((D2, {"caution": "CLN2 has an approved therapy (ERT-2); it is approved for CLN2 only, our "
                                      "sources show no test in CLN5, and whether it helps CLN5 must be checked.",
                           "caution_kind": "approved_therapy", "therapy_for": D2}),
                     (D6, {"caution": "CLN5 and CLN6A act through largely different mechanisms.",
                           "caution_kind": "mechanism"})):
        a.edge("disease_similar_to", D5, o, status="inferred", score=0.3, attrs={"components": comp} | attrs,
               evidence=dict(source_type="computed", source_name="Atlas analytics", source_ref=None, quote="x",
                             method="algorithm:test"))
    a.close()
    write_json("slice", {"focus": [D5, D2, D1]})


def _run(monkeypatch, per_kind=5):
    monkeypatch.setattr(paths, "slice_config", lambda: SimpleNamespace(
        analytics={"paths_per_kind": per_kind, "max_path_len": 4, "hub_degree": 25}))
    g = load_graph()
    return g, paths.run(g)


def test_counterexample_therapy_trials_never_reach_d(interim, monkeypatch):
    _seed()
    _, rows = _run(monkeypatch)
    for d in (D5, D1):   # CLN5 (counterexample of CLN2) and CLN1 (reaches ERT-2 only through CLN2)
        mine = [r for r in rows if r["from_id"] == d]
        assert not [r["title"] for r in mine if ERT in r["node_ids"] or r["to_id"] in ("NCTX", "NCTO", "NCTF")]
    own = {r["to_id"]: r for r in rows if r["from_id"] == D2}
    assert {"NCTX", "NCTO", ERT} <= set(own)          # CLN2 still reaches its own therapy and its trials
    assert own["NCTX"]["attrs"]["status_cap"] == "inferred"


def test_view_drops_umbrella_trial_of_counterexample_therapy(interim, monkeypatch):
    _seed()
    g, rows = _run(monkeypatch)
    v = views.action_view(views.Index(g), D5, rows, [], {})
    assert not {"NCTX", "NCTO", "NCTF"} & {t["nct_id"] for t in v["trials"]}
    v2 = views.action_view(views.Index(g), D2, rows, [], {})
    assert "NCTF" in {t["nct_id"] for t in v2["trials"]}   # filed under CLN2's family and tests CLN2's therapy


def test_soluble_route_beats_equal_score_counterexample_routes(interim, monkeypatch):
    _seed()
    g, rows = _run(monkeypatch, per_kind=1)
    rel = [r for r in rows if r["from_id"] == D5 and r["kind"] == "related_disease"]
    assert rel[0]["to_id"] == P
    assert rel[1]["node_ids"] == [D5, SOL, D1]        # not D5 -> SCMAS -> CLN2 / CLN6A on node-id order
    assert rel[1]["score"] == rel[2]["score"]
    # ActionView shows the route without a caution even when a caution route comes first
    mech = [r for r in rel if r["attrs"].get("bridge") == "shared_mechanism"]
    shuffled = [rel[0]] + sorted(mech, key=lambda r: not r["attrs"].get("caution"))
    assert shuffled[1]["attrs"].get("caution")
    v = views.action_view(views.Index(g), D5, shuffled, [], {})
    conn = [c for c in v["connections"] if c["kind"] == "related_disease"]
    assert [c["to"] for c in conn] == [P, D1]


def test_approved_therapy_caution_only_on_routes_to_the_therapy_disease(interim, monkeypatch):
    _seed()
    _, rows = _run(monkeypatch)
    rel = {(r["from_id"], r["to_id"]): r for r in rows if r["kind"] == "related_disease"}
    assert "approved for CLN2 only" in rel[(D5, D2)]["attrs"]["caution"]
    assert "caution" not in rel[(D2, D5)]["attrs"]


def test_mechanism_view_skips_hypothesis_asset_covers(interim):
    _seed()
    g = load_graph()
    mv = views.mechanism_view(views.Index(g), SOL, [], [], {})
    cln5 = next(r for r in mv["diseases"] if r["disease"]["id"] == D5)
    assert "ASSET:hyp" not in {a["node"]["id"] for a in cln5["assets"]}
