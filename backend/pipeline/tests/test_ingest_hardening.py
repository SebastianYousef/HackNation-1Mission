"""Ingest guards: name matching, honest CT.gov statuses, secret redaction, HPO/RePORTER parsing helpers."""
import httpx
import pandas as pd
import pytest

from atlas_pipeline import http
from atlas_pipeline.ingest import clinicaltrials as ct
from atlas_pipeline.ingest import hpo, nih_reporter, pubmed
from atlas_pipeline.ingest._common import context_terms, mentions, search_names
from atlas_pipeline.reconcile import NameIndex

INCL = {"id": "MONDO:0019261", "label": "infantile neuronal ceroid lipofuscinosis",
        "synonyms": ["INCL", "infantile NCL", "Santavuori disease"], "attrs": {}}


def test_mentions_word_bounded_and_case_sensitive_for_acronyms():
    names = ["infantile neuronal ceroid lipofuscinosis", "INCL"]
    assert mentions("Infantile neuronal ceroid-lipofuscinosis (INCL) is fatal", names)
    assert mentions("in INCL mice", names) == "INCL"
    for junk in ("regulatory RNAs incl. tRNA", "IncL/M plasmids", "Rb3InCl6 perovskite",
                 "late-infantile neuronal ceroid lipofuscinosis", "late infantile neuronal ceroid lipofuscinosis"):
        assert mentions(junk, names) is None, junk
    assert mentions("Cln3 activates G1 transcription", ["CLN3"]) is None
    assert mentions("CLN10 patients", ["CLN1"]) is None


def test_search_names_and_pubmed_term_scope_acronyms():
    assert "INCL" in search_names(INCL)
    assert "INCL" not in search_names(INCL, abbreviations=False)
    assert "CLN5" in search_names({"label": "neuronal ceroid lipofuscinosis 5", "synonyms": ["CLN5"]},
                                  abbreviations=False)
    assert context_terms(INCL) == ["ceroid", "lipofuscinosis"]
    term = pubmed.build_term(INCL, search_names(INCL))
    assert '("INCL"[tiab] AND ("ceroid"[tiab] OR "lipofuscinosis"[tiab]))' in term
    assert '"infantile NCL"[tiab]' in term


def test_http_error_does_not_leak_api_key(monkeypatch):
    transport = httpx.MockTransport(lambda req: httpx.Response(400, text="bad key"))
    monkeypatch.setattr(http, "_client", lambda: httpx.Client(transport=transport))
    with pytest.raises(httpx.HTTPStatusError) as ei:
        http.request("GET", "https://example.org/esearch.fcgi", params={"api_key": "SECRET123", "term": "x"})
    assert "SECRET123" not in str(ei.value)
    assert ei.value.__suppress_context__
    assert http.redact("https://x/?db=pubmed&api_key=SECRET123&email=a@b.c") == \
        "https://x/?db=pubmed&api_key=***&email=***"


def test_efetch_error_body_is_not_cached(monkeypatch):
    body = "<eFetchResult><ERROR>API key invalid</ERROR></eFetchResult>"
    transport = httpx.MockTransport(lambda req: httpx.Response(200, text=body))
    monkeypatch.setattr(http, "_client", lambda: httpx.Client(transport=transport))
    url, params = "https://example.org/efetch.fcgi", {"id": "1"}
    with pytest.raises(ValueError):
        http.get_text(url, params, ns="test", validate=pubmed._check_efetch)
    key = url + "?" + http.json.dumps(params, sort_keys=True)
    assert not http._cache_path("test", key, "txt").exists()


@pytest.mark.parametrize("body", ["<html>503 Service Temporarily Unavailable</html>",
                                  '{"error":"API rate limit exceeded"}',
                                  '{"esearchresult":{"ERROR":"Empty term and query_key - nothing todo"}}'])
def test_esearch_non_json_or_error_body_is_not_cached(monkeypatch, body):
    calls = []

    def handler(req):
        calls.append(req)
        return httpx.Response(200, text=body if len(calls) == 1 else '{"esearchresult":{"idlist":["1"],"count":"1"}}')
    monkeypatch.setattr(http, "_client", lambda: httpx.Client(transport=httpx.MockTransport(handler)))
    url, params = f"https://example.org/esearch-{abs(hash(body))}.fcgi", {"term": "x"}
    with pytest.raises(ValueError):
        http.get_json(url, params, ns="test", validate=pubmed._check_esearch)
    key = url + "?" + http.json.dumps(params, sort_keys=True)
    assert not http._cache_path("test", key, "txt").exists()
    # the next run fetches again and caches the good answer
    assert http.get_json(url, params, ns="test", validate=pubmed._check_esearch)["esearchresult"]["idlist"] == ["1"]
    assert http.get_json(url, params, ns="test", validate=pubmed._check_esearch)["esearchresult"]["count"] == "1"
    assert len(calls) == 2


def test_esearch_warning_body_is_cached():
    pubmed._check_esearch({"esearchresult": {"idlist": [], "count": "0",
                                             "errorlist": {"phrasesnotfound": ["xyz"]}, "warninglist": {}}})


def test_invalid_cached_body_is_refetched(monkeypatch):
    url, params = "https://example.org/poisoned.fcgi", {"term": "y"}
    p = http._cache_path("test", url + "?" + http.json.dumps(params, sort_keys=True), "txt")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("<html>Bad Gateway</html>")  # cached by a version without the check
    transport = httpx.MockTransport(lambda req: httpx.Response(200, json={"ok": True}))
    monkeypatch.setattr(http, "_client", lambda: httpx.Client(transport=transport))
    assert http.get_json(url, params, ns="test") == {"ok": True}
    assert http.json.loads(p.read_text()) == {"ok": True}


def test_cache_key_ignores_api_key_and_email(monkeypatch):
    calls = []

    def handler(req):
        calls.append(str(req.url))
        return httpx.Response(200, json={"n": len(calls)})
    monkeypatch.setattr(http, "_client", lambda: httpx.Client(transport=httpx.MockTransport(handler)))
    url, base = "https://example.org/shared.fcgi", {"db": "pubmed", "term": "CLN5"}
    assert http.get_json(url, base, ns="test") == {"n": 1}
    # a keyed run (another machine) reuses the keyless answer, and the cache key never holds the secret
    assert http.get_json(url, base | {"api_key": "SECRET123", "email": "a@b.c"}, ns="test") == {"n": 1}
    assert len(calls) == 1
    assert http._cache_key(url, base | {"api_key": "SECRET123"}) == url + "?" + http.json.dumps(base, sort_keys=True)
    assert http.get_json(url, base | {"term": "CLN6"}, ns="test") == {"n": 2}  # other params still key


def test_ctgov_condition_statuses():
    idx = NameIndex()
    idx.add({"id": "MONDO:0019262", "type": "disease", "label": "juvenile neuronal ceroid lipofuscinosis",
             "synonyms": ["batten disease"]})
    idx.add({"id": "MONDO:0008769", "type": "disease", "label": "neuronal ceroid lipofuscinosis 2",
             "synonyms": ["CLN2 disease"]})
    assert ct.condition_match(idx, "Juvenile Neuronal Ceroid Lipofuscinosis") == ("MONDO:0019262", "literature", "label")
    assert ct.condition_match(idx, "CLN2 Disease") == ("MONDO:0008769", "hypothesis", "synonym")
    assert ct.condition_match(idx, "Batten Disease") is None  # umbrella term, never the juvenile form
    assert ct.condition_match(idx, "Rare Disorders") is None


def test_ctgov_officials_and_interventions():
    assert ct.official_name("Medical Director, MD", None) is None
    assert ct.official_name("Medical Monitor, MD", "Acme") is None
    assert ct.official_name("DEHARO Jean Claude, MD", None) == ("DEHARO", "Jean", "Jean Claude Deharo")
    assert ct.official_name("An N Dang Do, M.D.", None)[0] == "Do"
    assert ct.official_name("Ronald G. Crystal, MD", None)[:2] == ("Crystal", "Ronald")
    assert ct.official_name("JC Smith, MD", None) == ("Smith", "JC", "JC Smith")
    assert ct.int_subtype("BIOLOGICAL", "AAVrh.10CUhCLN2 vector") == "gene_therapy"
    assert ct.int_subtype("DRUG", "scAAV9.CB.CLN6 (dose: 1.5E14 vector genomes)") == "gene_therapy"
    assert ct.int_subtype("BIOLOGICAL", "HuCNS-SC") == "other"
    assert ct.int_subtype("BIOLOGICAL", "Hematopoetic Stem Cell Transplantation") == "other"
    assert ct.int_subtype("DRUG", "Cerliponase Alfa") == "enzyme_replacement"
    assert ct.int_subtype("DRUG", "Miglustat") == "other"
    for name in ("Cysteamine bitartrate delayed-release", "Extended release capsule", "Increase dose"):
        assert ct.int_subtype("DRUG", name) == "other", name
    assert ct.PLACEBO.search("Liquid Placebo")


def test_hpo_onset_descendants_and_zero_frequency():
    terms = pd.DataFrame({"id": ["HP:0003674", "HP:0003593", "HP:0011420", "HP:0001522"],
                          "parents": [[], ["HP:0003674"], ["HP:0000001"], ["HP:0011420"]]}).set_index("id")
    assert hpo.descendants(terms, hpo.ONSET) == {"HP:0003674", "HP:0003593"}
    assert hpo.freq_value("0/3")[1] == 0.0 and hpo.freq_value("HP:0040285")[1] == 0.0


def test_reporter_prefers_parent_row_and_requires_a_mention():
    sub = {"subproject_id": "7517", "fiscal_year": 2026, "award_amount": 799137}
    parent = {"subproject_id": None, "fiscal_year": 2025, "award_amount": 1788368}
    assert min([sub, parent], key=nih_reporter._rank) is parent
    names = ["infantile neuronal ceroid lipofuscinosis", "infantile NCL"]
    assert nih_reporter._sentence("We study opioids. Data incl. HIV.", names) is None
    assert nih_reporter._sentence("Intro. Infantile NCL is fatal. More.", names) == "Infantile NCL is fatal."


def test_reporter_acronym_only_quote_needs_disease_context():
    cln3 = {"id": "MONDO:0008767", "label": "neuronal ceroid lipofuscinosis 3", "synonyms": ["CLN3"]}
    names = ["neuronal ceroid lipofuscinosis 3", "CLN3"]
    yeast = {"project_title": "Cyclin C-CDK8/19 kinases in development and in cancer",
             "abstract_text": "Cyclin C was cloned as a human gene able to rescue the proliferative block in yeast "
                              "Saccharomyces cerevisiae lacking CLN1, CLN2 and CLN3 genes. We study cancer."}
    assert nih_reporter._quote(yeast, cln3, names) is None
    batten = {"project_title": "Gene therapy for Batten disease",
              "abstract_text": "We treat CLN3 mice. Outcomes are measured."}
    assert nih_reporter._quote(batten, cln3, names) == "We treat CLN3 mice."
    gt = {"project_title": "Network modulation to improve gene therapy in CLN3 disease", "abstract_text": ""}
    assert nih_reporter._quote(gt, cln3, names) == gt["project_title"]
    cell_cycle = {"project_title": "Coupling of protein synthesis with cell division",
                  "abstract_text": "Project 2 focuses on the translational control of CLN3."}
    assert nih_reporter._quote(cell_cycle, cln3, names) is None
    full = {"project_title": "x", "abstract_text": "Neuronal ceroid lipofuscinosis 3 is fatal."}
    assert nih_reporter._quote(full, cln3, names) == "Neuronal ceroid lipofuscinosis 3 is fatal."
