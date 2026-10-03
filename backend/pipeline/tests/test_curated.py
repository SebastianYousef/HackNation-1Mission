"""ingest/curated: build() against the real slice + HGNC table, the status rule, malformed-entry guards and the
quote verifier (offline, with a fake fetcher)."""
import json
import shutil

import pytest

from atlas_pipeline.config import PIPELINE_DIR, curated_config
from atlas_pipeline.models import EDGE_ENDPOINTS

REAL = PIPELINE_DIR / "data" / "interim"
Q = dict(source_type="publication", source_name="PubMed", source_ref="PMID:1", url="https://pubmed.ncbi.nlm.nih.gov/1/",
         quote="CLN5 acts in lysosomal protein degradation.", published_at=2020)
NOQ = dict(source_type="patient_org_site", source_name="Org", url="https://example.org/")


@pytest.fixture()
def sl(interim):
    """The real slice.json + hgnc_genes.parquet copied into the throwaway data dir (read-only use of data/)."""
    if not (REAL / "slice.json").exists() or not (REAL / "hgnc_genes.parquet").exists():
        pytest.skip("needs data/interim/slice.json and hgnc_genes.parquet (run the pipeline ingest first)")
    for f in ("slice.json", "hgnc_genes.parquet"):
        shutil.copy(REAL / f, interim / f)
    from atlas_pipeline.ingest import hgnc
    hgnc.lookup.cache_clear()
    yield json.loads((interim / "slice.json").read_text())
    hgnc.lookup.cache_clear()


def _types(g, sl):
    t = {d: "disease" for d in sl["diseases"]} | {x: "gene" for x in sl["genes"]} | {"ORG:test-org": "organization"}
    return t | {n["id"]: n["type"] for n in g.nodes.values()}


def _check_endpoints(g, sl):
    types = _types(g, sl)
    for e in g.edges.values():
        src_t, dst_t = EDGE_ENDPOINTS[e["type"]]
        assert types.get(e["src"]) in src_t and types.get(e["dst"]) in dst_t, e
        assert any(v["edge_id"] == e["id"] for v in g.evidence.values()), e


def test_real_yaml_is_all_literature_with_quotes(sl):
    from atlas_pipeline.ingest import curated
    cur = curated_config()
    g = curated.build(cur, sl)
    assert g.edges and all(e["status"] == "literature" for e in g.edges.values())
    assert all(v["quote"] and v["source_type"] != "computed" for v in g.evidence.values())
    _check_endpoints(g, sl)
    # every owner / mechanism referenced from the YAML is defined in the YAML
    ids = {n for n in g.nodes}
    for a in cur["assets"]:
        if a.get("owner"):
            assert a["owner"]["id"] in ids, a["id"]
    # nothing in the YAML was dropped: one edge per (entry, ref) pair
    refs = sum(len(o.get("serves") or []) for o in cur["organizations"]) \
        + sum(len(m.get("genes") or []) + len(m.get("diseases") or []) for m in cur["mechanisms"]) \
        + sum(len(a.get("covers") or []) + len(a.get("targets") or []) + bool(a.get("owner")) for a in cur["assets"]) \
        + sum(len(i.get("treats") or []) + len(i.get("targets") or []) for i in cur["interventions"])
    assert len(g.edges) == refs
    bdsra = g.nodes["ORG:bdsra"]
    assert "Advocacy" in bdsra["label"] and bdsra["url"] == "https://bdsrafoundation.org"
    assert "ORG:bdsra-foundation" not in g.nodes                                  # merged into ORG:bdsra
    assert all(n["description"] for n in g.nodes.values() if n["type"] == "mechanism")


def test_new_sections_and_status_rule(sl):
    from atlas_pipeline.ingest import curated
    cur = {
        "organizations": [{"label": "Test Org", "subtype": "patient_group", "synonyms": ["TO"],
                           "serves": [{"disease": "MONDO:0016295", "evidence": [NOQ]},          # no quote
                                      {"disease": "OMIM:204200"},                               # no evidence
                                      {"disease": "MONDO:9999999", "evidence": [Q]}]}],         # not in slice
        "mechanisms": [{"label": "Lysosomal protein degradation", "subtype": "biological_process",
                        "description": "d",
                        "genes": [{"gene": "TPP1", "evidence": [Q]}, {"gene": "HGNC:2073", "evidence": [Q]},
                                  {"gene": "NOT_A_GENE_XYZ", "evidence": [Q]},
                                  {"gene": "BRCA1", "evidence": [Q]}],                          # outside slice
                        "diseases": [{"disease": "OMIM:204500", "evidence": [Q]}]}],
        "assets": [{"id": "ASSET:test-registry", "label": "Test registry", "subtype": "registry", "access": "open",
                    "species": "Mus musculus",
                    "owner": {"id": "ORG:test-org", "evidence": [Q]},
                    "covers": [{"disease": "MONDO:0016295", "evidence": [Q]}],
                    "targets": [{"gene": "TPP1", "evidence": [dict(Q, source_type="computed")]}]}],  # bad ev
        "interventions": [{"label": "Drug X", "subtype": "other",
                           "treats": [{"disease": "OMIM:204500", "approval": "investigational", "evidence": [Q]}],
                           "targets": [{"mechanism": "ATLAS:mech-lysosomal-protein-degradation",
                                        "evidence": [Q]},
                                       {"mechanism": "HGNC:2073", "evidence": [Q]}]}],         # wrong endpoint
    }
    g = curated.build(cur, sl)
    types = _types(g, sl)
    assert types["ATLAS:mech-lysosomal-protein-degradation"] == "mechanism"
    assert types["ASSET:test-registry"] == "asset" and types["ATLAS:int-drug-x"] == "intervention"
    assert g.nodes["ORG:test-org"]["synonyms"] == ["TO"]
    a = g.nodes["ASSET:test-registry"]["attrs"]
    assert a["asset_kind"] == "registry" and a["access"] == "open" and a["owner_id"] == "ORG:test-org"
    assert a["species"] == "Mus musculus"
    by = {(e["type"], e["src"], e["dst"]): e for e in g.edges.values()}
    serves = [e for e in g.edges.values() if e["type"] == "organization_serves_disease"]
    assert len(serves) == 1 and serves[0]["status"] == "hypothesis"
    assert not any(e["type"] == "asset_targets_gene" for e in g.edges.values())   # only evidence was invalid
    assert ("gene_in_mechanism", "HGNC:2073", "ATLAS:mech-lysosomal-protein-degradation") in by
    assert types.get("HGNC:1100") == "gene"                                         # BRCA1 node added
    assert by[("intervention_treats_disease", "ATLAS:int-drug-x", "MONDO:0008769")]["attrs"] == {
        "approval": "investigational"}
    assert ("intervention_targets_mechanism", "ATLAS:int-drug-x", "ATLAS:mech-lysosomal-protein-degradation") in by
    assert ("intervention_targets_mechanism", "ATLAS:int-drug-x", "HGNC:2073") not in by
    assert ("organization_maintains_asset", "ORG:test-org", "ASSET:test-registry") in by
    assert all(e["status"] in ("literature", "hypothesis") for e in g.edges.values())
    _check_endpoints(g, sl)
    ev = next(v for v in g.evidence.values() if v["source_ref"] == "PMID:1")
    assert str(ev["published_at"]) == "2020-01-01"


def test_malformed_structure_is_skipped_not_fatal(sl):
    from atlas_pipeline.ingest import curated
    cur = {
        "organizations": [
            {"label": "Old Format Org", "serves": ["MONDO:0016295"]},                   # bare string entry
            {"label": "Single Evidence Org", "serves": [{"disease": "MONDO:0016295", "evidence": Q}]},  # one dict
            {"label": "Mixed Evidence Org", "serves": [{"disease": "MONDO:0016295", "evidence": ["oops", Q]}]},
            {"subtype": "foundation"},                                                 # no label
            "just a string",
            {"id": 5, "label": "Numeric id"},                                          # bad id type -> prefix check
        ],
        "mechanisms": {"label": "Not a list", "subtype": "pathway"},                   # one mapping, not a list
        "assets": None,
    }
    g = curated.build(cur, sl)
    assert set(g.nodes) == {"ORG:old-format-org", "ORG:single-evidence-org", "ORG:mixed-evidence-org",
                            "ATLAS:mech-not-a-list"}
    assert {e["src"] for e in g.edges.values()} == {"ORG:single-evidence-org", "ORG:mixed-evidence-org"}
    assert all(e["status"] == "literature" for e in g.edges.values())
    assert curated.build(None, sl).nodes == {}


def test_bad_id_prefix_skipped(sl):
    from atlas_pipeline.ingest import curated
    g = curated.build({"assets": [{"id": "ORG:wrong", "label": "x", "subtype": "registry"}]}, sl)
    assert not g.nodes


def test_verify_quotes_offline():
    from atlas_pipeline.ingest import curated
    pages = {
        "https://a.example/": ["Some text. CLN5Y392X cells store subunit c. More."],
        "https://pdf.example/x.pdf": curated.NotCheckable("PDF"),
        "https://down.example/": RuntimeError("HTTP 404 for GET https://down.example/"),
    }

    def fetch(url):
        p = pages[url]
        if isinstance(p, Exception):
            raise p
        return p
    ev = lambda url, q: dict(source_type="web", source_name="x", url=url, quote=q)  # noqa: E731
    cur = {"organizations": [{"label": "o", "serves": [{"disease": "MONDO:1", "evidence": [
        ev("https://a.example/", "CLN5Y392X   cells store\nsubunit c."),               # whitespace differs: ok
        ev("https://a.example/", "CLN5Y392X cells store subunit c."),                  # duplicate quote: once
        ev("https://a.example/", "Not on the page."),
        ev("https://pdf.example/x.pdf", "label text"),
        ev("https://down.example/", "gone"),
        dict(source_type="web", source_name="x", url="https://a.example/", quote=None)]}]}]}
    res = curated.verify_quotes(cur, fetch=fetch)
    assert [q for _, q in res["ok"]] == ["CLN5Y392X cells store subunit c."]
    assert [q for _, q in res["missing"]] == ["Not on the page."]
    assert len(res["unchecked"]) == 1 and len(res["failed"]) == 1


def test_page_text_and_source_routing(monkeypatch):
    from atlas_pipeline.ingest import curated
    raw = "<p>The <i>CLN3</i> gene &amp; CLN5<sup>Y392X</sup></p><script>var x='hidden'</script>"
    assert curated.page_text(raw) == "The CLN3 gene & CLN5 Y392X"
    assert curated.page_text(raw, inline_join=True) == "The CLN3 gene & CLN5Y392X"
    calls = []

    class R:
        def __init__(self, text, ctype="text/html", data=None):
            self.text, self.content, self.headers, self._data = text, text.encode(), {"content-type": ctype}, data

        def json(self):
            return self._data

    def fake_request(method, url, **kw):
        calls.append((url, kw.get("params") or {}))
        if "clinicaltrials.gov/api" in url:
            return R("", "application/json", {"protocolSection": {"briefSummary": "A  phase 1\nstudy."}})
        if "/server/api/pid/find" in url:
            return R('{"metadata": {"dc.description.abstract": [{"value": "A poster about sheep."}]}}')
        if url.endswith(".pdf"):
            return R("%PDF-1.4", "application/pdf")
        return R("Line one\nline two.")
    monkeypatch.setattr("atlas_pipeline.http.request", fake_request)
    assert curated.source_texts("https://pubmed.ncbi.nlm.nih.gov/27722792/") == ["Line one line two."]
    assert calls[-1][1]["db"] == "pubmed" and calls[-1][1]["id"] == "27722792" and "efetch" in calls[-1][0]
    curated.source_texts("https://pmc.ncbi.nlm.nih.gov/articles/PMC6961983/")
    assert calls[-1][1]["db"] == "pmc" and calls[-1][1]["id"] == "6961983"
    assert "A phase 1 study." in curated.source_texts("https://clinicaltrials.gov/study/NCT04613089")
    assert calls[-1][0].endswith("/api/v2/studies/NCT04613089")
    assert "A poster about sheep." in curated.source_texts("https://researcharchive.lincoln.ac.nz/handle/10182/8067")
    assert calls[-1][0] == "https://researcharchive.lincoln.ac.nz/server/api/pid/find?id=hdl:10182/8067"
    with pytest.raises(curated.NotCheckable):
        curated.source_texts("https://www.accessdata.fda.gov/x.pdf")
