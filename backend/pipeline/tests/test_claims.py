"""extract.check_quotes -> claims.jsonl -> reconcile.run -> graph.load_graph, on synthetic data (no LLM)."""
from atlas_pipeline import extract, reconcile
from atlas_pipeline.confidence import edge_confidence
from atlas_pipeline.graph import load_graph
from atlas_pipeline.ids import edge_id
from atlas_pipeline.store import GraphWriter, read_jsonl, write_jsonl

CLN5 = "HGNC:2076"
DIS = "MONDO:0012588"
HP = "HP:0001250"

ABSTRACT = ("Mutations in CLN5 cause CLN5 disease in children. CLN5 deficiency impairs autophagy in neurons. "
            "CLN5 disease involves impaired autophagy in the brain. Patients with CLN5 disease develop seizures "
            "in early childhood. The variant CLN5 p.Arg112His causes loss of enzyme activity in vitro. "
            "Gene therapy AAV9-CLN5 treats CLN5 disease in sheep models. CLN5 might regulate lysosomal "
            "pH in some cells. APOE e4 is the major risk factor for Alzheimer disease. CLN5 does not alter "
            "lysosomal pH in neurons. AAV9-CLN5 did not treat CLN5 disease in mice.")
REC = {"pmid": "123", "title": "CLN5 study", "abstract": ABSTRACT, "year": "2021", "query_diseases": [DIS]}


def _seed(prior_edge: bool = False) -> None:
    g = GraphWriter("hgnc")
    g.node(id=CLN5, type="gene", label="CLN5")
    g.node(id="HGNC:613", type="gene", label="APOE")
    g.node(id=DIS, type="disease", label="CLN5 disease")
    g.node(id=HP, type="phenotype", label="Seizure", synonyms=["seizures"])
    g.node(id="R-HSA-1", type="mechanism", label="autophagy")
    if prior_edge:
        g.edge("gene_associated_with_disease", CLN5, DIS, status="curated",
               evidence=dict(source_type="database", source_name="HGNC", source_ref="HGNC:2076"))
    g.close()


def _claim(subject, relation, obj, quote, stance="supports"):
    return extract.Claim(subject=subject, relation=relation, object=obj, stance=stance, quote=quote)


def _run(claims):
    rows, dropped = extract.check_quotes(REC, extract.Claims(claims=claims))
    assert dropped == 0
    write_jsonl("claims.jsonl", rows)
    reconcile.run()
    edges = {(e["type"], e["src"], e["dst"]): e for e in read_jsonl("reconcile.edges.jsonl")}
    ev = list(read_jsonl("reconcile.evidence.jsonl"))
    return edges, ev


def test_every_relation_becomes_an_edge(interim):
    _seed()
    edges, ev = _run([
        _claim("CLN5", "gene_associated_with_disease", "CLN5 disease", "Mutations in CLN5 cause CLN5 disease"),
        _claim("CLN5", "gene_in_mechanism", "autophagy", "CLN5 deficiency impairs autophagy in neurons."),
        _claim("CLN5 disease", "disease_involves_mechanism", "autophagy",
               "CLN5 disease involves impaired autophagy in the brain."),
        _claim("CLN5 disease", "disease_has_phenotype", "seizures",
               "Patients with CLN5 disease develop seizures in early childhood."),
        _claim("CLN5 p.Arg112His", "variant_effect", "loss of enzyme activity",
               "The variant CLN5 p.Arg112His causes loss of enzyme activity in vitro."),
        _claim("AAV9-CLN5", "intervention_treats_disease", "CLN5 disease",
               "Gene therapy AAV9-CLN5 treats CLN5 disease in sheep models."),
    ])
    mech_new = "ATLAS:mech-loss-of-enzyme-activity"
    expected = {
        ("gene_associated_with_disease", CLN5, DIS),
        ("gene_in_mechanism", CLN5, "R-HSA-1"),          # gene_in_mechanism gets no disease edge
        ("disease_involves_mechanism", DIS, "R-HSA-1"),
        ("disease_has_phenotype", DIS, HP),
        ("gene_in_mechanism", CLN5, mech_new),
        ("intervention_treats_disease", "ATLAS:int-aav9-cln5", DIS),
    }
    assert set(edges) == expected
    assert {e["status"] for e in edges.values()} == {"literature"}
    assert edges[("gene_in_mechanism", CLN5, mech_new)]["attrs"] == {"variant": "CLN5 p.Arg112His"}
    assert {v["stance"] for v in ev} == {"supports"}
    assert {v["published_at"] for v in ev} == {"2021-01-01"}


def test_speculative_claim_is_hypothesis(interim):
    _seed()
    edges, ev = _run([_claim("CLN5", "gene_in_mechanism", "lysosomal pH",
                             "CLN5 might regulate lysosomal pH in some cells.", stance="speculative")])
    (e,) = edges.values()
    assert e["status"] == "hypothesis" and ev[0]["stance"] == "supports"
    g = load_graph()
    assert g.edges[e["id"]]["confidence"] <= 0.4


def test_unresolved_disease_name_does_not_fall_back_to_query_disease(interim):
    _seed()
    edges, _ = _run([_claim("APOE", "gene_associated_with_disease", "Alzheimer disease",
                            "APOE e4 is the major risk factor for Alzheimer disease.")])
    assert edges == {}
    import pandas as pd
    m = pd.read_parquet(interim / "reconcile_mapping.parquet")
    assert "unresolved" in set(m["method"])


def test_contradicting_claim_never_creates_an_edge(interim):
    _seed()
    edges, ev = _run([_claim("CLN5", "gene_in_mechanism", "autophagy",
                             "CLN5 deficiency impairs autophagy in neurons.", stance="contradicts")])
    assert edges == {} and ev == []


def test_contradicting_claim_attaches_to_existing_edge(interim):
    _seed(prior_edge=True)
    edges, ev = _run([_claim("CLN5", "gene_associated_with_disease", "CLN5 disease",
                             "Mutations in CLN5 cause CLN5 disease", stance="contradicts")])
    eid = edge_id("gene_associated_with_disease", CLN5, DIS)
    assert [v["stance"] for v in ev] == ["contradicts"] and ev[0]["edge_id"] == eid
    g = load_graph()
    assert g.edges[eid]["status"] == "curated"
    assert g.edges[eid]["confidence"] == 0.75


def test_contradiction_attaches_to_mechanism_node_created_in_same_run(interim):
    _seed()
    edges, ev = _run([
        _claim("CLN5", "gene_in_mechanism", "lysosomal pH", "CLN5 does not alter lysosomal pH in neurons.",
               stance="contradicts"),
        _claim("CLN5", "gene_in_mechanism", "lysosomal pH", "CLN5 might regulate lysosomal pH in some cells."),
    ])
    eid = edge_id("gene_in_mechanism", CLN5, "ATLAS:mech-lysosomal-ph")
    assert sorted(v["stance"] for v in ev) == ["contradicts", "supports"]
    assert {v["edge_id"] for v in ev} == {eid}
    assert edges[("gene_in_mechanism", CLN5, "ATLAS:mech-lysosomal-ph")]["status"] == "literature"


def test_contradiction_attaches_to_unresolved_intervention_edge(interim):
    _seed()
    edges, ev = _run([
        _claim("AAV9-CLN5", "intervention_treats_disease", "CLN5 disease",
               "Gene therapy AAV9-CLN5 treats CLN5 disease in sheep models."),
        _claim("AAV9-CLN5", "intervention_treats_disease", "CLN5 disease",
               "AAV9-CLN5 did not treat CLN5 disease in mice.", stance="contradicts"),
    ])
    eid = edge_id("intervention_treats_disease", "ATLAS:int-aav9-cln5", DIS)
    assert sorted(v["stance"] for v in ev) == ["contradicts", "supports"]
    assert {v["edge_id"] for v in ev} == {eid}


def test_contradiction_alone_creates_no_mechanism_or_intervention_node(interim):
    _seed()
    edges, ev = _run([
        _claim("CLN5", "gene_in_mechanism", "lysosomal pH", "CLN5 does not alter lysosomal pH in neurons.",
               stance="contradicts"),
        _claim("AAV9-CLN5", "intervention_treats_disease", "CLN5 disease",
               "AAV9-CLN5 did not treat CLN5 disease in mice.", stance="contradicts"),
    ])
    assert edges == {} and ev == []
    assert list(read_jsonl("reconcile.nodes.jsonl")) == []


def test_graph_drops_literature_edge_without_support(interim):
    _seed()
    g = GraphWriter("pubmed")
    g.edge("gene_associated_with_disease", "HGNC:613", DIS, status="literature",
           evidence=dict(source_type="publication", source_name="PubMed", source_ref="PMID:1", stance="contradicts"))
    g.close()
    graph = load_graph()
    assert edge_id("gene_associated_with_disease", "HGNC:613", DIS) not in graph.edges


def test_quote_guardrail():
    claims = extract.Claims(claims=[
        _claim("CLN5", "gene_associated_with_disease", "CLN3 disease", "CLN5"),             # too short
        _claim("CLN5", "gene_associated_with_disease", "CLN5 disease",
               "CLN5 study Mutations in CLN5 cause"),                                     # spans title/abstract
        _claim("CLN5", "gene_associated_with_disease", "CLN5 disease",
               "mutations  in cln5 CAUSE cln5 disease"),                                  # LLM casing/spacing
        _claim("TPP1", "gene_associated_with_disease", "CLN2 disease",
               "Patients with CLN5 disease develop seizures"),                            # names neither end
    ])
    rows, dropped = extract.check_quotes(REC, claims)
    assert dropped == 3
    assert [r["quote"] for r in rows] == ["Mutations in CLN5 cause CLN5 disease"]


def test_confidence_ignores_computed_rows():
    paper = {"stance": "supports", "source_type": "publication", "source_ref": "PMID:1"}
    computed = [{"stance": "supports", "source_type": "computed", "source_ref": f"E:{i}"} for i in range(3)]
    assert edge_confidence("literature", [paper, *computed]) == 0.5
    assert edge_confidence("literature", [paper, {**paper, "source_ref": "PMID:2"}]) == 0.6
