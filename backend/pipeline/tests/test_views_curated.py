"""views.action_view / mechanism_view and analytics.paths / mechanisms on a tiny synthetic NCL graph:
quoted (curated.yaml) mechanisms surface, hypothesis trial links are never same-disease trials, and only a
quoted 'approved' treatment link makes has_approved_treatment true."""
from atlas_pipeline import views
from atlas_pipeline.analytics import mechanisms, paths
from atlas_pipeline.graph import load_graph
from atlas_pipeline.ids import edge_id
from atlas_pipeline.store import GraphWriter, write_json

D5, D2, D1, D8, P, D2S, D10 = "MONDO:5", "MONDO:2", "MONDO:1", "MONDO:8", "MONDO:9", "MONDO:22", "MONDO:10"
G5, G2, G1, G8 = "HGNC:5", "HGNC:2", "HGNC:1", "HGNC:8"
SOL, ER, HP = "ATLAS:mech-soluble", "ATLAS:mech-er", "HP:0000001"


def _q(text, method="manual"):
    return dict(source_type="publication", source_name="TEST", source_ref=f"PMID:{abs(hash(text)) % 10**6}",
                quote=text, method=method)


DB = dict(source_type="database", source_name="DB", source_ref="DB:1")


def _seed() -> None:
    w = GraphWriter("mondo")
    for d, lab in ((D5, "CLN5"), (D2, "CLN2"), (D1, "CLN1"), (D8, "CLN8"), (P, "NCL"), (D2S, "late infantile CLN2"),
                   (D10, "CLN10")):
        w.node(id=d, type="disease", label=lab)
    for g_, lab in ((G5, "CLN5g"), (G2, "TPP1"), (G1, "PPT1"), (G8, "CLN8g")):
        w.node(id=g_, type="gene", label=lab)
    w.node(id=HP, type="phenotype", label="Seizure")
    for d, gg in ((D5, G5), (D2, G2), (D1, G1), (D8, G8), (D2S, G2)):
        w.edge("gene_associated_with_disease", gg, d, status="curated", evidence=DB)
    for c, p in ((D5, P), (D2, P), (D1, P), (D8, P), (D2S, D2)):
        w.edge("disease_subtype_of", c, p, status="curated", evidence=DB)
    for d in (D5, D1):
        w.edge("disease_has_phenotype", d, HP, status="curated", evidence=DB)
    w.close()

    c = GraphWriter("curated")
    c.node(id=SOL, type="mechanism", label="soluble protein deficiency")
    c.node(id=ER, type="mechanism", label="ER-Golgi trafficking")
    for d in (D5, D2, D1, D10):
        c.edge("disease_involves_mechanism", d, SOL, status="literature", evidence=_q(f"{d} is soluble"))
    c.edge("disease_involves_mechanism", D8, ER, status="literature", evidence=_q("CLN8 is ER"))
    c.edge("gene_in_mechanism", G5, SOL, status="literature", evidence=_q("CLN5 soluble"))
    c.edge("gene_in_mechanism", G5, ER, status="literature", evidence=_q("CLN5 binds EGRESS"))
    c.node(id="ATLAS:int-erp", type="intervention", label="ERT-2")
    c.node(id="ATLAS:int-gt5", type="intervention", label="GT-5")
    c.node(id="ATLAS:int-claim", type="intervention", label="unquoted claim")
    c.edge("intervention_treats_disease", "ATLAS:int-erp", D2, status="literature", attrs={"approval": "approved"},
           evidence=_q("ERT-2 is approved for CLN2"))
    c.edge("intervention_treats_disease", "ATLAS:int-gt5", D5, status="literature",
           attrs={"approval": "investigational"}, evidence=_q("GT-5 is in a CLN5 trial"))
    c.edge("intervention_treats_disease", "ATLAS:int-claim", D5, status="hypothesis", attrs={"approval": "approved"},
           evidence=dict(source_type="web", source_name="TEST", source_ref="https://example.org", quote=None))
    c.node(id="ASSET:cln1-mouse", type="asset", subtype="animal_model", label="CLN1 mouse")
    c.edge("asset_covers_disease", "ASSET:cln1-mouse", D1, status="literature", evidence=_q("mouse for CLN1"))
    c.node(id="ASSET:cln10-mouse", type="asset", subtype="animal_model", label="CLN10 mouse")
    c.edge("asset_covers_disease", "ASSET:cln10-mouse", D10, status="literature", evidence=_q("mouse for CLN10"))
    c.close()

    t = GraphWriter("clinicaltrials")
    t.node(id="NCT1", type="trial", subtype="interventional", label="GT-5 for CLN5 (title match)")
    t.node(id="NCT2", type="trial", subtype="observational", label="CLN5 natural history")
    t.edge("trial_studies_disease", "NCT1", D5, status="hypothesis",
           evidence=dict(source_type="trial_registry", source_name="ClinicalTrials.gov", source_ref="NCT1",
                         quote="GT-5 for CLN5", method="algorithm:ctgov_title_match"))
    t.edge("trial_studies_disease", "NCT2", D5, status="literature",
           evidence=dict(source_type="trial_registry", source_name="ClinicalTrials.gov", source_ref="NCT2",
                         quote="Condition: CLN5", method="algorithm:ctgov_condition_match:label"))
    t.close()

    a = GraphWriter("analytics")
    comp = {"phenotype": 0.5, "mechanism": 0.5, "gene": 0.0}
    for o, attrs in ((D2, {"caution": "CLN2 has an approved therapy (ERT-2); it is approved for CLN2 only, our "
                                      "sources show no test in CLN5, and whether it helps CLN5 must be checked.",
                           "caution_kind": "approved_therapy", "therapy_for": D2}),
                     (D1, {}), (D8, {"caution": "different mechanisms", "caution_kind": "mechanism"})):
        a.edge("disease_similar_to", D5, o, status="inferred", score=0.3, attrs={"components": comp} | attrs,
               evidence=dict(source_type="computed", source_name="Atlas analytics", source_ref=None, quote="x",
                             method="algorithm:test"))
    a.close()


def _view(d=D5):
    g = load_graph()
    return g, views.action_view(views.Index(g), d, [], [], {})


def test_hypothesis_trial_link_is_never_same_disease(interim):
    _seed()
    _, v = _view()
    by_id = {t["nct_id"]: t for t in v["trials"]}
    assert by_id["NCT2"]["relevance"] == "same_disease" and by_id["NCT2"]["eligibility_note"] is None
    assert by_id["NCT1"]["relevance"] != "same_disease"
    assert by_id["NCT1"]["eligibility_note"].startswith("Unconfirmed match")
    assert "title" in by_id["NCT1"]["eligibility_note"]
    assert by_id["NCT1"]["conditions"] == []   # the hypothesis link is not listed as a condition


def test_treatment_status_counts_only_quoted_approvals(interim):
    _seed()
    _, v = _view()
    ts = v["treatment_status"]
    assert ts["has_approved_treatment"] is False     # investigational + an unquoted 'approved' claim
    assert "Investigational, not approved: GT-5" in ts["note"]
    assert "Unconfirmed" in ts["note"] and "unquoted claim" in ts["note"]
    assert "ERT-2" not in ts["note"]                 # CLN2's approval never transfers to CLN5
    assert v["headline"].startswith("No approved treatment found yet")
    _, v2 = _view(D2)
    assert v2["treatment_status"]["has_approved_treatment"] is True
    _, sub = _view(D2S)                              # approved for the broader CLN2: unknown, not true
    assert sub["treatment_status"]["has_approved_treatment"] is None
    assert "broader diagnosis" in sub["treatment_status"]["note"]
    assert edge_id("intervention_treats_disease", "ATLAS:int-erp", D2) in sub["treatment_status"]["edge_ids"]


def test_documented_mechanism_in_communities_and_assets(interim):
    _seed()
    g, v = _view()
    comms = {c["disease"]["id"]: c for c in v["related_communities"]}
    assert "documented disease mechanism (soluble protein deficiency)" in comms[D1]["why"]
    assert any("ER-Golgi trafficking" in x for x in comms[D8]["differences"])
    assert comms[D2]["caution"] and "approved for CLN2 only" in comms[D2]["caution"]
    assets = {a["node"]["id"]: a for a in v["assets"]}
    assert assets["ASSET:cln1-mouse"]["reusability"] == "adaptable"   # a top look-alike's asset
    mate = assets["ASSET:cln10-mouse"]       # CLN10 only shares the documented mechanism with CLN5
    assert mate["reusability"] == "reference_only"
    assert any("shares soluble protein deficiency" in x for x in mate["what_differs"])
    assert edge_id("disease_involves_mechanism", D10, SOL) in mate["edge_ids"]
    steps = [s for s in v["next_steps"] if s["kind"] == "validate_experiment"]
    assert all("ERT-2" not in s["title"] for s in steps)


def test_paths_prefer_quoted_mechanism_over_symptom_bridge(interim):
    _seed()
    write_json("slice", {"focus": [D5]})
    g = load_graph()
    routes = paths.bounded_routes(g, paths.build_adj(g, 25), D5, 4)
    assert routes[D1][1] == [D5, SOL, D1]            # not D5 -> Seizure -> D1, not the similarity edge
    rows = paths.run(g)
    by_to = {r["to_id"]: r for r in rows if r["kind"] == "related_disease"}
    assert by_to[D1]["attrs"]["bridge"] == "shared_mechanism"
    assert by_to[D1]["attrs"]["via_mechanism"] == [SOL]
    assert "approved for CLN2 only" in by_to[D2]["attrs"].get("caution", "")
    # an approved therapy of a mechanism-mate never reaches CLN5
    assert not any(r["to_id"] == "ATLAS:int-erp" for r in rows)


def test_curated_mechanism_genes_propagate_only_through_quoted_diseases(interim):
    _seed()
    g = load_graph(exclude={"analytics"})
    mechanisms.run(g)
    g = load_graph(exclude={"analytics"})
    # CLN5 gene sits in both curated mechanisms, but only CLN5 disease is quoted in SOL
    assert g.edges[edge_id("disease_involves_mechanism", D5, SOL)]["status"] == "literature"
    assert edge_id("disease_involves_mechanism", D5, ER) not in g.edges


def test_mechanism_view_ranks_quoted_first_and_skips_hypothesis_trials(interim):
    _seed()
    g = load_graph()
    mv = views.mechanism_view(views.Index(g), SOL, [], [], {})
    assert [r["status"] for r in mv["diseases"]] == ["literature"] * 4
    assert {r["disease"]["id"] for r in mv["diseases"]} == {D5, D2, D1, D10}
    assert "NCT1" not in {t["nct_id"] for t in mv["trials"]}
    cln5 = next(r for r in mv["diseases"] if r["disease"]["id"] == D5)
    assert cln5["unmet_need"].startswith("No approved therapy") and "GT-5" in cln5["unmet_need"]
    assert next(r for r in mv["diseases"] if r["disease"]["id"] == D2)["unmet_need"] is None
