"""Orphanet prevalence (en_product9_prev.xml) -> disease attrs.prevalence. No network: a canned XML file."""
import shutil

import pytest

from atlas_pipeline.config import RAW
from atlas_pipeline.graph import load_graph
from atlas_pipeline.ingest import orphanet
from atlas_pipeline.store import GraphWriter, read_jsonl, write_json

PREV_XML = """<?xml version="1.0" encoding="UTF-8"?>
<JDBOR date="2025-06-01" version="1.3.39">
  <DisorderList count="3">
    <Disorder id="1">
      <OrphaCode>228346</OrphaCode>
      <Name lang="en">CLN5 disease</Name>
      <PrevalenceList count="3">
        <Prevalence id="11">
          <Source>12345[PMID]_67890[PMID]</Source>
          <PrevalenceType id="1"><Name lang="en">Point prevalence</Name></PrevalenceType>
          <PrevalenceQualification id="2"><Name lang="en">Value and class</Name></PrevalenceQualification>
          <PrevalenceClass id="3"><Name lang="en">1-9 / 1 000 000</Name></PrevalenceClass>
          <ValMoy>0.2</ValMoy>
          <PrevalenceGeographic id="4"><Name lang="en">Finland</Name></PrevalenceGeographic>
          <PrevalenceValidationStatus id="5"><Name lang="en">Validated</Name></PrevalenceValidationStatus>
        </Prevalence>
        <Prevalence id="12">
          <Source>ORPHANET_EXPERTS</Source>
          <PrevalenceType id="1"><Name lang="en">Birth prevalence</Name></PrevalenceType>
          <PrevalenceQualification id="2"><Name lang="en">Class only</Name></PrevalenceQualification>
          <PrevalenceClass id="3"><Name lang="en">&lt;1 / 1 000 000</Name></PrevalenceClass>
          <ValMoy>0.0</ValMoy>
          <PrevalenceGeographic id="4"><Name lang="en">Worldwide</Name></PrevalenceGeographic>
          <PrevalenceValidationStatus id="5"><Name lang="en">Not yet validated</Name></PrevalenceValidationStatus>
        </Prevalence>
        <Prevalence id="13">
          <Source>11111[PMID]</Source>
          <PrevalenceType id="1"><Name lang="en">Annual incidence</Name></PrevalenceType>
          <PrevalenceQualification id="2"><Name lang="en">Class only</Name></PrevalenceQualification>
          <PrevalenceClass id="3"><Name lang="en">1-9 / 1 000 000</Name></PrevalenceClass>
          <ValMoy></ValMoy>
          <PrevalenceGeographic id="4"><Name lang="en">Worldwide</Name></PrevalenceGeographic>
          <PrevalenceValidationStatus id="5"><Name lang="en">Validated</Name></PrevalenceValidationStatus>
        </Prevalence>
      </PrevalenceList>
    </Disorder>
    <Disorder id="2">
      <OrphaCode>999999</OrphaCode>
      <Name lang="en">Disease outside the slice</Name>
      <PrevalenceList count="1">
        <Prevalence id="21">
          <PrevalenceType id="1"><Name lang="en">Point prevalence</Name></PrevalenceType>
          <PrevalenceClass id="3"><Name lang="en">1-5 / 10 000</Name></PrevalenceClass>
        </Prevalence>
      </PrevalenceList>
    </Disorder>
    <Disorder id="3">
      <OrphaCode>79262</OrphaCode>
      <Name lang="en">CLN6 disease</Name>
      <PrevalenceList count="0"/>
    </Disorder>
  </DisorderList>
</JDBOR>
"""
GENES_XML = """<?xml version="1.0" encoding="UTF-8"?><JDBOR><DisorderList count="0"/></JDBOR>"""
X2M = {"ORPHA:228346": "MONDO:0016295", "ORPHA:999999": "MONDO:9999999", "ORPHA:79262": "MONDO:0012345"}
DISEASES = {"MONDO:0016295", "MONDO:0012345"}


@pytest.fixture()
def prev_file(tmp_path):
    p = tmp_path / "en_product9_prev.xml"
    p.write_text(PREV_XML)
    return p


def test_parse_prevalence_maps_orpha_to_mondo_and_ranks_validated_worldwide_first(prev_file):
    out = orphanet.parse_prevalence(prev_file, X2M, DISEASES)
    assert set(out) == {"MONDO:0016295"}  # outside the slice dropped; no records -> no key
    recs = out["MONDO:0016295"]
    assert [(r["type"], r["geographic"], r["validation"]) for r in recs] == [
        ("Annual incidence", "Worldwide", "Validated"),
        ("Point prevalence", "Finland", "Validated"),
        ("Birth prevalence", "Worldwide", "Not yet validated"),
    ]
    assert recs[1] == {"type": "Point prevalence", "class": "1-9 / 1 000 000", "qualification": "Value and class",
                       "mean": 0.2, "geographic": "Finland", "validation": "Validated",
                       "source": "12345[PMID]_67890[PMID]", "pmids": ["PMID:12345", "PMID:67890"]}
    assert recs[2]["class"] == "<1 / 1 000 000"
    assert "mean" not in recs[0] and "mean" not in recs[2]  # empty / 0.0 ValMoy means "not given"
    assert "pmids" not in recs[2] and recs[2]["source"] == "ORPHANET_EXPERTS"


def test_parse_prevalence_dedups_when_two_orpha_codes_share_a_mondo_id(prev_file):
    out = orphanet.parse_prevalence(prev_file, {**X2M, "ORPHA:79262": "MONDO:0016295"}, DISEASES)
    assert len(out["MONDO:0016295"]) == 3


def test_emit_patches_disease_attrs_prevalence(interim, prev_file):
    raw = RAW / "orphanet"
    raw.mkdir(parents=True, exist_ok=True)
    try:
        (raw / "en_product6.xml").write_text(GENES_XML)
        shutil.copy(prev_file, raw / "en_product9_prev.xml")
        write_json("slice", {"xref_to_mondo": X2M, "diseases": sorted(DISEASES)})
        g = GraphWriter("mondo")
        g.node(id="MONDO:0016295", type="disease", label="CLN5 disease")
        g.close()
        orphanet.emit()
        patches = list(read_jsonl("orphanet.node_patches.jsonl"))
        assert [p["id"] for p in patches] == ["MONDO:0016295"]
        prev = load_graph().nodes["MONDO:0016295"]["attrs"]["prevalence"]
        assert prev[0]["validation"] == "Validated" and prev[0]["geographic"] == "Worldwide"
        assert len(prev) == 3
    finally:
        shutil.rmtree(raw, ignore_errors=True)


GENE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<JDBOR><DisorderList count="1">
  <Disorder id="1">
    <OrphaCode>228346</OrphaCode>
    <Name lang="en">CLN5 disease</Name>
    <DisorderGeneAssociationList count="1">
      <DisorderGeneAssociation>
        <SourceOfValidation>34733232[PMID]_2412666[PMID]_34684815[PMID]_34733232[PMID]</SourceOfValidation>
        <Gene id="9"><Symbol>CLN5</Symbol><ExternalReferenceList count="1">
          <ExternalReference id="9"><Source>HGNC</Source><Reference>2076</Reference></ExternalReference>
        </ExternalReferenceList></Gene>
        <DisorderGeneAssociationType id="1"><Name lang="en">Disease-causing germline mutation(s) in</Name></DisorderGeneAssociationType>
        <DisorderGeneAssociationStatus id="1"><Name lang="en">Assessed</Name></DisorderGeneAssociationStatus>
      </DisorderGeneAssociation>
    </DisorderGeneAssociationList>
  </Disorder>
</DisorderList></JDBOR>
"""


def test_gene_evidence_links_to_orphanet_not_to_the_papers_it_cites(interim, monkeypatch):
    """Issue #13: the quote is Orphanet's own line, so it must not be shown as a quote from a PubMed paper."""
    monkeypatch.setattr(orphanet, "retrieved_at", lambda p: "2026-10-04T00:00:00Z")
    monkeypatch.setattr(orphanet.hgnc, "resolve", lambda k: {"hgnc_id": "HGNC:2076", "symbol": "CLN5"})
    monkeypatch.setattr(orphanet.hgnc, "gene_node", lambda g, rec: g.node(id=rec["hgnc_id"], type="gene",
                                                                           label=rec["symbol"]))
    raw = RAW / "orphanet"
    raw.mkdir(parents=True, exist_ok=True)
    try:
        (raw / "en_product6.xml").write_text(GENE_XML)
        write_json("slice", {"xref_to_mondo": X2M, "diseases": sorted(DISEASES)})
        g = GraphWriter("mondo")
        g.node(id="MONDO:0016295", type="disease", label="CLN5 disease")
        g.close()
        orphanet.emit()
        graph = load_graph()
        (edge,) = graph.edges_of("gene_associated_with_disease")
        assert edge["status"] == "curated"
        assert [(v["source_ref"], v["url"]) for v in graph.evidence[edge["id"]]] == [
            ("ORPHA:228346", "https://www.orpha.net/en/disease/detail/228346")]
        assert edge["attrs"]["orphanet_pmids"] == ["PMID:34733232", "PMID:34684815"]  # deduped, wrong one dropped
    finally:
        shutil.rmtree(raw, ignore_errors=True)
