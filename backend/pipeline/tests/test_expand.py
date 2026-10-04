"""Atlas expansion: several disease families in one slice, MONDO gene links and obsolete-term mapping,
MedlinePlus / Orphanet plain summaries, and the Bright Data request budget, registry searches and query names.
No network: canned XML, fake HTTP responses and small synthetic tables."""
import json
import textwrap

import httpx
import pandas as pd
import pytest

from atlas_pipeline import config, slice as slice_mod, views
from atlas_pipeline.graph import load_graph
from atlas_pipeline.ingest import brightdata_orgs as bd
from atlas_pipeline.ingest import clinicaltrials as ct
from atlas_pipeline.ingest import medlineplus as mp
from atlas_pipeline.ingest import mondo
from atlas_pipeline.obo import parse_obo
from atlas_pipeline.reconcile import NameIndex
from atlas_pipeline.store import GraphWriter, read_json, read_jsonl, write_json


# ---- config: families --------------------------------------------------------------------------------------
def test_families_merge_into_slice_lists(tmp_path):
    p = tmp_path / "slice.yaml"
    p.write_text(textwrap.dedent("""
        version: t
        name: t
        roots: [MONDO:1]
        focus_roots: []
        seed_genes: [A]
        families:
          - {key: lyso, label: lysosomal storage, roots: [MONDO:1], focus_roots: [MONDO:2], seed_genes: [B],
             umbrella_terms: [Batten]}
          - {key: mito, label: primary mitochondrial disease, roots: [MONDO:3], focus_roots: [MONDO:4],
             query_by_gene: true, umbrella_terms: [Mitochondrial Disease]}
    """))
    cfg = config.slice_config(str(p))
    assert cfg.roots == ["MONDO:1", "MONDO:3"] and cfg.focus_roots == ["MONDO:2", "MONDO:4"]
    assert cfg.seed_genes == ["A", "B"]
    assert cfg.umbrella_terms == {"batten", "mitochondrial disease"}
    assert [f.query_by_gene for f in cfg.families] == [False, True]


def test_family_of_prefers_the_family_whose_focus_covers_it(tmp_path):
    cfg = config.SliceConfig(version="t", name="t", roots=[], focus_roots=[], extra_diseases=[], seed_genes=[],
                             families=[config.Family("a", "A group", roots=["R1"], focus_roots=["F1"]),
                                       config.Family("b", "B group", roots=["R2"], focus_roots=["X"])])
    children = {"R1": ["X", "F1"], "R2": ["Y"]}
    fams, family_of = slice_mod.families(cfg, children, {"R1", "X", "F1", "R2", "Y"})
    assert family_of == {"F1": "a", "R1": "a", "R2": "b", "X": "b", "Y": "b"}
    assert [f["focus"] for f in fams] == [["F1"], ["X"]]


def test_shipped_slice_keeps_ncl_and_adds_two_families():
    cfg = config.slice_config()
    assert "MONDO:0016295" in cfg.focus_roots and "MONDO:0002561" in cfg.roots
    assert {f.key for f in cfg.families} == {"lysosomal", "mitochondrial", "dee"}
    assert "MONDO:0100135" in cfg.focus_roots and "MONDO:0044970" in cfg.roots
    assert "batten disease" in cfg.umbrella_terms


# ---- MONDO: gene links, exact synonyms, obsolete replaced_by mapping ----------------------------------------
OBO = """format-version: 1.2

[Term]
id: MONDO:0011794
name: obsolete Dravet syndrome
xref: Orphanet:33069 {source="MONDO:obsoleteEquivalent", source="OMIM:607208"}
is_obsolete: true
replaced_by: MONDO:0100135

[Term]
id: MONDO:0100135
name: Dravet syndrome
synonym: "severe myoclonic epilepsy of infancy" EXACT []
synonym: "SMEI" EXACT ABBREVIATION []
synonym: "Dravet-like" RELATED []
xref: DOID:0080422 {source="MONDO:equivalentTo"}
is_a: MONDO:0100062 ! genetic developmental and epileptic encephalopathy
relationship: has_material_basis_in_germline_mutation_in http://identifiers.org/hgnc/10585 {source="x"} ! SCN1A
relationship: disease_shares_features_of MONDO:0100079 ! DEE6A
"""


def test_obo_relationships_and_obsolete_equivalents(tmp_path):
    p = tmp_path / "m.obo"
    p.write_text(OBO)
    terms = {t["id"]: t for t in parse_obo(p)}
    assert terms["MONDO:0011794"]["xrefs"][0]["obsolete_equivalent"] is True
    rels = terms["MONDO:0100135"]["relationships"]
    assert ("has_material_basis_in_germline_mutation_in", "http://identifiers.org/hgnc/10585") in rels
    assert mondo.material_basis_genes(rels) == ["HGNC:10585"]


def _mondo_df(rows):
    base = {"def": None, "obsolete": False, "parents": [], "synonyms": [], "exact_synonyms": [], "abbreviations": [],
            "xrefs": [], "equiv_xrefs": [], "obsolete_equiv_xrefs": [], "replaced_by": "", "material_basis_genes": [],
            "subsets": []}
    return pd.DataFrame([base | r for r in rows])


def test_xref_map_uses_replaced_by_only_for_unclaimed_xrefs():
    df = _mondo_df([
        {"id": "MONDO:old", "name": "obsolete x", "obsolete": True, "replaced_by": "MONDO:new",
         "obsolete_equiv_xrefs": ["ORPHA:1", "OMIM:2"]},
        {"id": "MONDO:new", "name": "x"},
        {"id": "MONDO:live", "name": "y", "equiv_xrefs": ["OMIM:2"]},
        {"id": "MONDO:old2", "name": "obsolete z", "obsolete": True, "replaced_by": "MONDO:gone",
         "obsolete_equiv_xrefs": ["ORPHA:3"]},
    ])
    m = mondo.xref_map(df)
    assert m["ORPHA:1"] == "MONDO:new"      # obsolete term's equivalent -> its live replacement
    assert m["OMIM:2"] == "MONDO:live"      # a live equivalentTo always wins
    assert "ORPHA:3" not in m               # replacement not a live term


# ---- MedlinePlus Genetics / Orphanet summaries ------------------------------------------------------------
MP_XML = """<?xml version="1.0" encoding="utf-8"?>
<summaries xmlns="https://medlineplus.gov/download/ghr-summaries-20250602.xsd" xmlns:html="http://www.w3.org/1999/xhtml">
<health-condition-summary id="1">
<name>CLN5 disease</name>
<ghr-page>https://medlineplus.gov/genetics/condition/cln5-disease</ghr-page>
<text-list><text><text-role>description</text-role><html><html:p>CLN5 disease is an inherited disorder that
primarily affects the nervous system. The signs begin in childhood. Mutations in the CLN5 gene cause it.</html:p>
<html:p>Second paragraph.</html:p></html></text></text-list>
<related-gene-list><related-gene><gene-symbol>CLN5</gene-symbol></related-gene></related-gene-list>
<db-key-list><db-key><db>OMIM</db><key>256731</key></db-key><db-key><db>MeSH</db><key>D009472</key></db-key></db-key-list>
</health-condition-summary>
<health-condition-summary id="2">
<name>Leigh syndrome</name>
<ghr-page>https://medlineplus.gov/genetics/condition/leigh-syndrome</ghr-page>
<text-list><text><text-role>description</text-role><html><html:p>Leigh syndrome is a severe neurological disorder.
It usually begins in the first year of life.</html:p></html></text></text-list>
<related-gene-list><related-gene><gene-symbol>SURF1</gene-symbol></related-gene></related-gene-list>
<db-key-list><db-key><db>OMIM</db><key>256000</key></db-key><db-key><db>OMIM</db><key>220111</key></db-key>
<db-key><db>OMIM</db><key>999999</key></db-key></db-key-list>
</health-condition-summary>
<gene-summary id="3">
<gene-symbol>CLN5</gene-symbol>
<ghr-page>https://medlineplus.gov/genetics/gene/cln5</ghr-page>
<text-list><text><text-role>function</text-role><html><html:p>The CLN5 gene provides instructions for making a
protein. Its function is unknown.</html:p></html></text></text-list>
</gene-summary>
</summaries>
"""

ORPHA_XML = """<?xml version="1.0" encoding="UTF-8"?>
<JDBOR><DisorderList count="1"><Disorder id="1"><OrphaCode>33069</OrphaCode><Name lang="en">Dravet syndrome</Name>
<SummaryInformationList><SummaryInformation><TextSectionList><TextSection>
<TextSectionType><Name lang="en">Definition</Name></TextSectionType>
<Contents>A rare epilepsy with onset at 4-8&amp;nbsp;months. Seizures are often triggered by fever. Third.</Contents>
</TextSection></TextSectionList></SummaryInformation></SummaryInformationList></Disorder></DisorderList></JDBOR>
"""


@pytest.fixture()
def mp_files(tmp_path):
    a, b = tmp_path / "ghr.xml", tmp_path / "p1.xml"
    a.write_text(MP_XML)
    b.write_text(ORPHA_XML)
    return a, b


def test_parse_medlineplus_and_orphanet(mp_files):
    conds, genes = mp.parse_medlineplus(mp_files[0])
    assert [c["name"] for c in conds] == ["CLN5 disease", "Leigh syndrome"]
    assert conds[0]["omim"] == ["OMIM:256731"] and conds[0]["genes"] == ["CLN5"]
    assert conds[0]["paragraphs"][0].startswith("CLN5 disease is an inherited disorder that primarily affects")
    assert genes[0]["symbol"] == "CLN5" and genes[0]["paragraphs"]
    orpha = mp.parse_orphanet_definitions(mp_files[1])
    assert orpha["ORPHA:33069"]["definition"].startswith("A rare epilepsy with onset at 4-8 months.")
    assert "&nbsp;" not in orpha["ORPHA:33069"]["definition"]


def test_lead_sentences_and_gene_sentence():
    text = "First one is here. Second (B) follows. Third."
    assert mp.lead_sentences(text) == "First one is here. Second (B) follows."
    assert mp.lead_sentences(text, max_chars=20) == "First one is here."
    assert mp.gene_sentence(["Mutations in the CLN5 gene cause it.", "CLN50 is not it."], "CLN5") == \
        "Mutations in the CLN5 gene cause it."
    assert mp.gene_sentence(["Nothing here."], "CLN5") is None


def test_build_summaries_statuses_and_gene_edges(interim, mp_files, monkeypatch):
    genes = {"CLN5": {"hgnc_id": "HGNC:2076", "symbol": "CLN5"}, "SURF1": {"hgnc_id": "HGNC:11474", "symbol": "SURF1"}}
    monkeypatch.setattr(mp.hgnc, "resolve", lambda k: genes.get(k))
    monkeypatch.setattr(mp.hgnc, "gene_node", lambda g, rec: g.node(id=rec["hgnc_id"], type="gene", label=rec["symbol"]))
    df = _mondo_df([
        {"id": "MONDO:cln5", "name": "neuronal ceroid lipofuscinosis 5", "def": "Any NCL in which the cause of the "
                                                                              "disease is a mutation in CLN5."},
        {"id": "MONDO:cln5j", "name": "juvenile neuronal ceroid lipofuscinosis 5", "parents": ["MONDO:cln5"]},
        {"id": "MONDO:leigh", "name": "Leigh syndrome", "exact_synonyms": ["subacute necrotizing encephalopathy"]},
        {"id": "MONDO:lsfc", "name": "Leigh syndrome, French Canadian type"},
        {"id": "MONDO:dravet", "name": "Dravet syndrome", "def": "Dravet syndrome is a channelopathy."},
        {"id": "MONDO:defd", "name": "some disease", "def": "A clear description of some disease."},
    ])
    sl = {"diseases": ["MONDO:cln5", "MONDO:cln5j", "MONDO:leigh", "MONDO:lsfc", "MONDO:dravet", "MONDO:defd"],
          "genes": ["HGNC:2076"],
          "xref_to_mondo": {"OMIM:256731": "MONDO:cln5", "OMIM:256000": "MONDO:leigh", "OMIM:220111": "MONDO:lsfc",
                            "ORPHA:33069": "MONDO:dravet"}}
    conds, gs = mp.parse_medlineplus(mp_files[0])
    orpha = mp.parse_orphanet_definitions(mp_files[1])
    g = GraphWriter("medlineplus")
    patches = {p["id"]: p for p in mp.build(sl, df, conds, gs, orpha, g, "2026-10-04T00:00:00Z", None)}
    g.close()
    src = lambda d: patches[d]["attrs"]["plain_summary_source"]  # noqa: E731
    # exact by OMIM (the MONDO definition is a generated rule, so MedlinePlus wins anyway)
    assert patches["MONDO:cln5"]["plain_summary"] == ("CLN5 disease is an inherited disorder that primarily "
                                                      "affects the nervous system. The signs begin in childhood.")
    assert src("MONDO:cln5")["source_name"] == "MedlinePlus Genetics" and src("MONDO:cln5")["status"] == "curated"
    assert src("MONDO:cln5")["quote"] == patches["MONDO:cln5"]["plain_summary"]
    # exact by name although the condition lists three OMIM ids; the other listed disease gets the lead-in
    assert src("MONDO:leigh")["match"] == "name"
    assert patches["MONDO:lsfc"]["plain_summary"].startswith("A form of Leigh syndrome. Leigh syndrome is a severe")
    assert src("MONDO:lsfc")["match"] == "listed" and src("MONDO:lsfc")["status"] == "curated"
    # a subtype with no own record inherits its parent's text behind a fixed lead-in, marked inferred
    assert patches["MONDO:cln5j"]["plain_summary"].startswith("A form of neuronal ceroid lipofuscinosis 5. CLN5 disease")
    assert src("MONDO:cln5j")["status"] == "inferred" and src("MONDO:cln5j")["match"] == "parent"
    # Orphanet before the MONDO definition; a good MONDO definition is left to the views (no patch)
    assert src("MONDO:dravet")["source_name"] == "Orphanet"
    assert patches["MONDO:dravet"]["plain_summary"] == ("A rare epilepsy with onset at 4-8 months. Seizures are "
                                                        "often triggered by fever.")
    assert "MONDO:defd" not in patches
    # gene summary for a slice gene
    assert patches["HGNC:2076"]["plain_summary"].startswith("The CLN5 gene provides instructions")
    # gene links: curated, MedlinePlus evidence, the quote is the sentence naming the gene, else the record
    edges = {(e["src"], e["dst"]): e for e in read_jsonl("medlineplus.edges.jsonl")}
    ev = {v["edge_id"]: v for v in read_jsonl("medlineplus.evidence.jsonl")}
    e = edges[("HGNC:2076", "MONDO:cln5")]
    assert e["status"] == "curated" and e["type"] == "gene_associated_with_disease"
    assert ev[e["id"]]["quote"] == "Mutations in the CLN5 gene cause it."
    assert ev[e["id"]]["source_type"] == "database" and ev[e["id"]]["url"].endswith("/cln5-disease")
    assert ev[edges[("HGNC:11474", "MONDO:leigh")]["id"]]["quote"] == "Leigh syndrome | related gene: SURF1"
    assert ("HGNC:11474", "MONDO:lsfc") not in edges   # a group's genes are not pinned on one listed subtype


def test_node_patch_sets_plain_summary_in_graph(interim):
    g = GraphWriter("mondo")
    g.node(id="MONDO:1", type="disease", label="x")
    g.close()
    g = GraphWriter("medlineplus")
    g.close()
    from atlas_pipeline.store import write_node_patches
    write_node_patches("medlineplus", [{"id": "MONDO:1", "plain_summary": "X is rare.",
                                        "attrs": {"plain_summary_source": {"source_name": "MedlinePlus Genetics"}}}])
    n = load_graph().nodes["MONDO:1"]
    assert n["plain_summary"] == "X is rare." and n["attrs"]["plain_summary_source"]["source_name"]


# ---- views: per-family template -------------------------------------------------------------------------
def test_family_template_names_the_family(interim):
    g = GraphWriter("mondo")
    for i, lab in (("MONDO:1", "Leigh syndrome"), ("MONDO:2", "gene-hop disease"), ("MONDO:3", "parent")):
        g.node(id=i, type="disease", label=lab)
    g.edge("disease_subtype_of", "MONDO:2", "MONDO:3", status="curated",
           evidence=dict(source_type="database", source_name="MONDO", quote="MONDO:2 is_a MONDO:3"))
    g.close()
    write_json("slice", {"families": [{"key": "mito", "label": "primary mitochondrial disease"}],
                         "family_of": {"MONDO:1": "mito"}})
    ix = views.Index(load_graph())
    assert views.family_template(ix, "MONDO:1") == "Leigh syndrome is a rare disease in the primary mitochondrial disease group."
    assert views.family_template(ix, "MONDO:2") == "gene-hop disease is a rare disease, a form of parent."
    assert views.family_template(ix, "MONDO:3") == "parent is a rare disease."


# ---- ClinicalTrials.gov: family umbrella terms ----------------------------------------------------------
def test_family_umbrella_terms_never_match_one_subtype():
    idx = NameIndex()
    idx.add({"id": "MONDO:x", "type": "disease", "label": "MELAS syndrome", "synonyms": ["mitochondrial disease"]})
    idx.add({"id": "MONDO:y", "type": "disease", "label": "DEE 1", "synonyms": ["epilepsy"]})
    assert ct.condition_match(idx, "Mitochondrial Disease") is None
    assert ct.condition_match(idx, "Epilepsy") is None
    assert ct.condition_match(idx, "MELAS Syndrome") == ("MONDO:x", "literature", "label")


# ---- Bright Data: budget --------------------------------------------------------------------------------
def test_budget_persists_and_stops(tmp_path):
    p = tmp_path / "budget.json"
    b = bd.Budget(p, limit=2)
    b.take("serp", "q1")
    assert bd.Budget(p, limit=2).used == 1     # persisted
    b.take("unlocker", "https://x.org")
    with pytest.raises(bd.BudgetExhausted):
        b.take("serp", "q3")
    st = json.loads(p.read_text())
    assert st["used"] == 2 and st["by_kind"] == {"serp": 1, "unlocker": 1} and len(st["log"]) == 2


def test_brightdata_request_counts_only_billed_calls(interim, monkeypatch, tmp_path):
    p = tmp_path / "b.json"
    monkeypatch.setenv("BRIGHTDATA_API_KEY", "test-key")
    calls = []

    def fake_request(method, url, **kw):
        calls.append(kw["json"]["url"])
        return httpx.Response(200, json={"organic": [{"title": "t", "link": "https://a.org", "description": "d"}]})
    monkeypatch.setattr(bd, "request", fake_request)
    budget = bd.Budget(p, limit=1)
    bd.brightdata_request("expand budget query", budget)
    bd.brightdata_request("expand budget query", budget)    # cache hit: free, not counted
    assert budget.used == 1 and len(calls) == 1
    with pytest.raises(bd.BudgetExhausted):
        bd.brightdata_request("another expand query", budget)
    assert len(calls) == 1                                   # nothing sent once the budget is spent


def test_emit_stops_gracefully_at_the_budget(interim, monkeypatch):
    monkeypatch.setenv("BRIGHTDATA_API_KEY", "test-key")
    monkeypatch.setenv("BRIGHTDATA_BROWSER_WSS", "")
    monkeypatch.setattr(bd, "curated_config", lambda: {})
    sent = []

    def fake_search(q):
        if len(sent) >= 1:
            raise bd.BudgetExhausted("Bright Data budget exhausted (1/1 requests used)")
        sent.append(q)
        return [{"title": "CLN5 support group", "url": "https://cln5group.org/", "snippet": "Support for CLN5 families."}]
    monkeypatch.setattr(bd, "brightdata_request", fake_search)
    monkeypatch.setattr(bd, "fetch_page", lambda u: (_ for _ in ()).throw(httpx.ConnectError("down")))
    dis = {"id": "MONDO:0009745", "label": "neuronal ceroid lipofuscinosis 5", "synonyms": ["CLN5"], "attrs": {}}
    other = {"id": "MONDO:0008767", "label": "neuronal ceroid lipofuscinosis 3", "synonyms": ["CLN3"], "attrs": {}}
    bd.emit(diseases=[dis, other], per_disease=5)
    cov = read_json("coverage_brightdata")
    assert sent == ['"CLN5" disease foundation families support']
    assert cov[dis["id"]]["result_count"] == 1 and cov[other["id"]]["result_count"] is None
    (e,) = read_jsonl("brightdata_orgs.edges.jsonl")
    assert e["status"] == "hypothesis"


def test_registry_search_only_without_exact_group_and_groupless_first(interim, monkeypatch):
    monkeypatch.setenv("BRIGHTDATA_API_KEY", "test-key")
    monkeypatch.setenv("BRIGHTDATA_BROWSER_WSS", "")
    monkeypatch.setattr(bd, "curated_config", lambda: {})
    g = GraphWriter("curated")    # a curated (literature) group serves CLN3 exactly
    g.node(id="ORG:x", type="organization", label="X")
    g.node(id="MONDO:0008767", type="disease", label="neuronal ceroid lipofuscinosis 3")
    g.edge("organization_serves_disease", "ORG:x", "MONDO:0008767", status="literature",
           evidence=dict(source_type="patient_org_site", source_name="x.org", quote="X serves CLN3 families."))
    g.close()
    sent = []
    monkeypatch.setattr(bd, "brightdata_request", lambda q: sent.append(q) or [])
    monkeypatch.setattr(bd, "fetch_page", lambda u: (_ for _ in ()).throw(httpx.ConnectError("down")))
    cln3 = {"id": "MONDO:0008767", "label": "neuronal ceroid lipofuscinosis 3", "synonyms": ["CLN3"], "attrs": {}}
    cln5 = {"id": "MONDO:0009745", "label": "neuronal ceroid lipofuscinosis 5", "synonyms": ["CLN5"], "attrs": {}}
    bd.emit(diseases=[cln3, cln5], per_disease=5)
    assert sent == ['"CLN5" disease foundation families support', '"CLN3" disease foundation families support',
                    '"CLN5" patient registry']
    assert read_json("coverage_brightdata")["MONDO:0009745"]["query"] == \
        '"CLN5" disease foundation families support | "CLN5" patient registry'


def test_query_names_by_family():
    dee11 = {"id": "MONDO:0013388", "label": "developmental and epileptic encephalopathy, 11",
             "synonyms": ["DEE11", "EIEE11"], "attrs": {}}
    assert bd.search_query(dee11, by_gene=True, genes=["SCN2A"]) == '"SCN2A" foundation families support'
    assert bd.search_query(dee11) == '"DEE11" disease foundation families support'   # the original rule
    melas = {"id": "MONDO:0800032", "label": "MELAS syndrome caused by mutation in MTTL1", "synonyms": [], "attrs": {}}
    assert bd.registry_query(melas, by_gene=True, genes=["MT-TL1"]) == '"MELAS syndrome" patient registry'
    leigh_cm = {"id": "MONDO:0019083", "label": "Leigh syndrome with cardiomyopathy", "synonyms": [], "attrs": {}}
    assert bd.search_query(leigh_cm, by_gene=True, genes=[], rollup="Leigh syndrome") == \
        '"Leigh syndrome" foundation families support'
    barth = {"id": "MONDO:0010543", "label": "Barth syndrome", "synonyms": ["MGA2"], "attrs": {}}
    assert bd.search_query(barth, by_gene=True, genes=["TAFAZZIN"]) == '"Barth syndrome" foundation families support'
    # the lysosomal family keeps the cached CLN-style queries
    cln5 = {"id": "MONDO:0009745", "label": "neuronal ceroid lipofuscinosis 5", "synonyms": ["CLN5"], "attrs": {}}
    assert bd.search_query(cln5) == '"CLN5" disease foundation families support'


def test_unlocker_fetch_is_budgeted_and_cached(interim, monkeypatch, tmp_path):
    monkeypatch.setenv("BRIGHTDATA_API_KEY", "test-key")
    calls = []

    def fake_request(method, url, **kw):
        calls.append(kw["json"])
        return httpx.Response(200, text="<p>We support families with CLN5 disease.</p>")
    monkeypatch.setattr(bd, "request", fake_request)
    budget = bd.Budget(tmp_path / "u.json", limit=5)
    assert bd.unlocker_fetch("https://blocked.org/expand", budget)[1].startswith("<p>We support")
    assert bd.unlocker_fetch("https://blocked.org/expand", budget)[0] == "https://blocked.org/expand"
    assert budget.used == 1 and len(calls) == 1 and calls[0]["url"] == "https://blocked.org/expand"

    def failing(method, url, **kw):
        return httpx.Response(200, headers={"x-brd-error": "proxy_error"}, content=b"")
    monkeypatch.setattr(bd, "request", failing)
    with pytest.raises(bd.BrightDataError, match="proxy_error"):
        bd.unlocker_fetch("https://blocked.org/other", budget)


def test_configuration_error_switches_to_cached_searches_only(interim, monkeypatch):
    monkeypatch.setenv("BRIGHTDATA_API_KEY", "test-key")
    monkeypatch.setenv("BRIGHTDATA_BROWSER_WSS", "")
    monkeypatch.setattr(bd, "curated_config", lambda: {})
    monkeypatch.setattr(bd, "fetch_page", lambda u: (_ for _ in ()).throw(httpx.ConnectError("down")))
    cln3 = {"id": "MONDO:0008767", "label": "neuronal ceroid lipofuscinosis 3", "synonyms": ["CLN3"], "attrs": {}}
    cln5 = {"id": "MONDO:0009745", "label": "neuronal ceroid lipofuscinosis 5", "synonyms": ["CLN5"], "attrs": {}}
    ok = httpx.Response(200, json={"organic": [{"title": "CLN3 families", "link": "https://cln3group.org/",
                                                "description": "Support for CLN3 families."}]})
    monkeypatch.setattr(bd, "request", lambda *a, **kw: ok)
    bd.brightdata_request(bd.search_query(cln3))           # an earlier run cached this search
    sent = []

    def not_whitelisted(method, url, **kw):
        sent.append(kw["json"]["url"])
        return httpx.Response(200, content=b"", headers={
            "x-brd-err-code": "client_10030", "x-brd-err-msg": "The IP address ... is not whitelisted in this zone"})
    monkeypatch.setattr(bd, "request", not_whitelisted)
    bd.emit(diseases=[cln5, cln3], per_disease=5)
    assert len(sent) == 1                                   # one rejected request, then nothing is sent
    cov = read_json("coverage_brightdata")
    assert cov[cln5["id"]]["result_count"] is None and cov[cln3["id"]]["result_count"] == 1


def test_cache_only_env_sends_nothing(interim, monkeypatch):
    monkeypatch.setenv("BRIGHTDATA_API_KEY", "test-key")
    monkeypatch.setenv("BRIGHTDATA_BROWSER_WSS", "")
    monkeypatch.setenv("ATLAS_BRIGHTDATA_CACHE_ONLY", "1")
    monkeypatch.setattr(bd, "curated_config", lambda: {})
    monkeypatch.setattr(bd, "request", lambda *a, **kw: pytest.fail("nothing may be sent in cache-only mode"))
    dis = {"id": "MONDO:x-cache-only", "label": "cache only disease 7", "synonyms": ["COD7"], "attrs": {}}
    bd.emit(diseases=[dis], per_disease=5)
    assert read_json("coverage_brightdata")[dis["id"]]["result_count"] is None
