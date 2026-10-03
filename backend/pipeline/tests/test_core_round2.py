"""ids (doubled prefixes, asset_id), the literature quote guardrail (graph + load.validate), and the
Bright Data stage on canned responses (no network)."""
import httpx
import pytest

from atlas_pipeline import load
from atlas_pipeline.graph import load_graph
from atlas_pipeline.ids import asset_id, normalize_curie
from atlas_pipeline.ingest import brightdata_orgs as bd
from atlas_pipeline.store import GraphWriter, read_json, read_jsonl

DIS = {"id": "MONDO:0009745", "label": "neuronal ceroid lipofuscinosis 5",
       "synonyms": ["CLN5", "CLN5 disease, juvenile"], "attrs": {}}


# ---- ids ---------------------------------------------------------------------------------------------
@pytest.mark.parametrize("raw,want", [
    ("MONDO:MONDO:0008769", "MONDO:0008769"), ("HP:HP:0001250", "HP:0001250"), ("HGNC:HGNC:2073", "HGNC:2073"),
    ("hgnc:HGNC:2073", "HGNC:2073"), ("Orphanet:ORPHA:558", "ORPHA:558"), ("MONDO:0008769", "MONDO:0008769"),
    ("Orphanet:558", "ORPHA:558"), ("NCT01234567", "NCT01234567"),
])
def test_normalize_curie_drops_doubled_prefix(raw, want):
    assert normalize_curie(raw) == want


def test_asset_id_matches_hand_built_ids():
    assert asset_id("BDSRA Foundation Family Register") == "ASSET:bdsra-foundation-family-register"
    # the id brightdata_orgs wrote before the helper existed: ASSET:<host with dots as dashes>-registry
    assert asset_id("bdsrafoundation.org registry") == "ASSET:bdsrafoundation-org-registry"


# ---- quote guardrail ---------------------------------------------------------------------------------
def _seed_lit(quote):
    g = GraphWriter("curated")
    g.node(id="ORG:x", type="organization", label="X")
    g.node(id=DIS["id"], type="disease", label=DIS["label"])
    g.edge("organization_serves_disease", "ORG:x", DIS["id"], status="literature",
           evidence=dict(source_type="patient_org_site", source_name="x.org", url="https://x.org", quote=quote))
    g.close()


@pytest.mark.parametrize("quote,status,downgraded", [(None, "hypothesis", 1), ("   ", "hypothesis", 1),
                                                     ("X supports families with CLN5 disease.", "literature", 0)])
def test_literature_without_quote_is_downgraded(interim, quote, status, downgraded):
    _seed_lit(quote)
    g = load_graph()
    (e,) = g.edges.values()
    assert (e["status"], g.downgraded) == (status, downgraded)
    if status == "hypothesis":
        assert e["confidence"] <= 0.4
    assert load.validate(g, [], [], [], []) == []


def test_validate_rejects_literature_without_quote(interim):
    _seed_lit(None)
    g = load_graph()
    (e,) = g.edges.values()
    e["status"] = "literature"  # e.g. a later step re-upgrading it
    assert any("literature without a quoted" in x for x in load.validate(g, [], [], [], []))


# ---- Bright Data: response parsing --------------------------------------------------------------------
def test_parse_response_errors_in_headers_with_http_200():
    r = httpx.Response(200, headers={"x-brd-err-code": "client_10000", "x-brd-err-msg": "zone not found"},
                       content=b"")
    with pytest.raises(bd.BrightDataError, match="client_10000: zone not found"):
        bd.parse_response(r)
    with pytest.raises(bd.BrightDataError, match="empty"):
        bd.parse_response(httpx.Response(200, content=b""))
    with pytest.raises(bd.BrightDataError, match="non-JSON"):
        bd.parse_response(httpx.Response(200, content=b"<html>"))
    assert bd.parse_response(httpx.Response(200, json={"organic": []})) == {"organic": []}


def test_search_query_has_no_or_operator():
    q = bd.search_query(DIS)
    assert q.startswith('"CLN5"') and " OR " not in q


# ---- Bright Data: page reading ------------------------------------------------------------------------
PAGE = """<html><head><title>CLN5 &amp; families</title><meta property="og:site_name" content="Hope For CLN5 -">
<script>var x = "CLN5 disease is in a script and must not be quoted.";</script></head>
<body><nav><a href="/contact">Contact us</a></nav><h1>Welcome</h1>
<p>We support families affected by CLN5 disease across Europe. Donate today.</p>
<p>Join our patient registry to help research.</p></body></html>"""


def test_read_page_quotes_visible_text_only():
    site = bd.read_page("https://hope.org/cln5", PAGE, bd.search_names(DIS, k=8))
    assert site["disease_sentences"] == ["We support families affected by CLN5 disease across Europe."]
    assert bd.pick_quote(site["disease_sentences"], ["Hope For CLN5 -", "hope"], site["final_url"]) == \
        "We support families affected by CLN5 disease across Europe."
    assert site["registry_quote"] == "Join our patient registry to help research"
    assert site["contact_url"] == "https://hope.org/contact"
    assert site["title"] == "CLN5 & families" and site["site_name"] == "Hope For CLN5 -"


def test_registry_needs_ownership_wording():
    assert bd.REGISTRY_RX.search("Keep up to date with clinical trial and natural history study news") is None
    assert bd.REGISTRY_RX.search("Enrol in the CLN5 patient registry today")


# ---- Bright Data: emit ---------------------------------------------------------------------------------
def test_emit_is_honest_and_dedupes_curated(interim, monkeypatch):
    monkeypatch.setenv("BRIGHTDATA_API_KEY", "test-key")
    monkeypatch.setenv("BRIGHTDATA_BROWSER_WSS", "")
    monkeypatch.setattr(bd, "curated_config", lambda: {"organizations": [
        {"id": "ORG:bdsra", "label": "BDSRA", "website": "https://bdsrafoundation.org"}]})
    other = {"id": "MONDO:0008767", "label": "neuronal ceroid lipofuscinosis 3", "synonyms": ["CLN3"], "attrs": {}}
    serp = [
        {"title": "Hope For CLN5", "url": "https://www.hope.org/cln5", "snippet": "A family foundation."},
        {"title": "CLN5 update", "url": "https://bdsrafoundation.org/cln5-update/", "snippet": "BDSRA news on CLN5"},
        {"title": "Warhol Foundation", "url": "https://warholfoundation.org/", "snippet": "Visual arts grants."},
        {"title": "CLN5 support group", "url": "https://cln5group.org/", "snippet": "Support for CLN5 families."},
        {"title": "CLN5 Foundation preprint", "url": "https://www.biorxiv.org/x", "snippet": "CLN5 family"},
    ]

    def fake_search(q):
        if '"CLN3"' in q:
            raise bd.BrightDataError("Bright Data error : redirect location was rejected")
        return serp
    pages = {"https://www.hope.org/cln5": PAGE,
             "https://bdsrafoundation.org/cln5-update/": "<p>Join our registry. Nothing about the disease.</p>"}

    def fake_fetch(url):
        if url not in pages:
            raise httpx.ConnectError("down")
        return url, pages[url]
    monkeypatch.setattr(bd, "brightdata_request", fake_search)
    monkeypatch.setattr(bd, "fetch_page", fake_fetch)
    bd.emit(diseases=[DIS, other], per_disease=5)

    nodes = {n["id"]: n for n in read_jsonl("brightdata_orgs.nodes.jsonl")}
    edges = {(e["type"], e["src"], e["dst"]): e for e in read_jsonl("brightdata_orgs.edges.jsonl")}
    ev = {v["edge_id"]: v for v in read_jsonl("brightdata_orgs.evidence.jsonl")}
    # curated org: no duplicate node, and no edge because its page does not name the disease
    assert "ORG:bdsrafoundation-org" not in nodes and not any(k[1] == "ORG:bdsra" for k in edges)
    assert not any("warhol" in k or "biorxiv" in k for k in nodes)  # generic / journal hits dropped
    assert set(nodes) == {"ORG:hope-org", "ORG:cln5group-org", "ASSET:hope-org-registry"}
    assert nodes["ORG:hope-org"]["label"] == "Hope For CLN5" and nodes["ORG:hope-org"]["attrs"]["unverified"]
    assert all(e["status"] == "hypothesis" for e in edges.values())
    assert all(v["method"] == "scrape:brightdata" for v in ev.values())
    hope = ev[edges[("organization_serves_disease", "ORG:hope-org", DIS["id"])]["id"]]
    assert hope["quote"] in PAGE and hope["source_type"] == "patient_org_site"
    # page fetch failed: lead known only from the search result -> no quote, snippet kept in attrs
    grp = edges[("organization_serves_disease", "ORG:cln5group-org", DIS["id"])]
    assert ev[grp["id"]]["quote"] is None and ev[grp["id"]]["source_type"] == "web"
    assert grp["attrs"]["search_snippet"] == "Support for CLN5 families."
    assert ("asset_covers_disease", "ASSET:hope-org-registry", DIS["id"]) in edges
    cov = read_json("coverage_brightdata")
    assert cov[DIS["id"]]["result_count"] == 2 and cov["MONDO:0008767"]["result_count"] is None


def test_skip_domains_match_whole_domains():
    def org(u):
        return bd.looks_like_org({"url": u, "title": "CLN5 Foundation", "snippet": ""})
    assert not org("https://x.com/cln5") and not org("https://en.wikipedia.org/wiki/CLN5")
    assert not org("https://battendiseasenews.com/a") and not org("https://hcp.biomarin.com/en-us/cln2/")
    assert org("https://fedex.com/cln5-foundation") and org("https://cln5foundation.org/")


# ---- round 2: scraped quotes never make 'literature' ------------------------------------------------
def test_scraped_quote_does_not_satisfy_literature_guardrail(interim):
    """A curated literature edge whose only quote is a Bright Data scrape is not literature."""
    _seed_lit(None)
    g = GraphWriter("brightdata_orgs")
    g.edge("organization_serves_disease", "ORG:x", DIS["id"], status="hypothesis",
           evidence=dict(source_type="patient_org_site", source_name="x.org", url="https://x.org/faq",
                         quote="Where can I find information about diagnosis of CLN5 disease?",
                         method="scrape:brightdata"))
    g.close()
    g = load_graph()
    (e,) = g.edges.values()
    assert e["status"] == "hypothesis" and g.downgraded == 1 and e["confidence"] <= 0.4
    e["status"] = "literature"
    assert any("literature without a quoted" in x for x in load.validate(g, [], [], [], []))


# ---- round 2: the quote must be about the org itself -------------------------------------------------
DIRECTORY = """<html><head><title>Family funds</title><meta property="og:site_name" content="All About Batten">
</head><body><h1>Family-run funds</h1>
<p>Noah's Hope was founded by the family of Noah Hyatt, who has CLN5 Batten disease.</p>
<p>Neurogene Inc licenses a CLN5 gene therapy.</p>
<p>CLN5 research is moving fast.</p></body></html>"""


def test_directory_sentence_about_another_charity_is_not_quoted():
    names = bd.search_names(DIS, k=8)
    site = bd.read_page("https://allaboutbattendisease.org/family-funds", DIRECTORY, names)
    assert len(site["disease_sentences"]) == 3
    own = ["All About Batten", "allaboutbattendisease"]
    assert bd.pick_quote(site["disease_sentences"], own, site["final_url"]) is None
    # on the homepage a neutral sentence may stand, but still never one naming another org
    assert bd.pick_quote(site["disease_sentences"], own, "https://allaboutbattendisease.org/") == \
        "CLN5 research is moving fast."
    # the org naming itself, also in a form with an org word ('BDSRA Foundation'), is fine
    bdsra = ["Batten Disease Support, Research, and Advocacy Foundation (BDSRA)", "BDSRA Foundation"]
    assert bd.pick_quote(["The BDSRA Foundation funds CLN5 research."], bdsra, "https://x.org/news/a")
    assert bd.pick_quote(["Theranexus licensing deal for CLN3 announced"], bdsra, "https://x.org/news/a") is None


def test_emit_drops_quote_from_directory_page(interim, monkeypatch):
    monkeypatch.setenv("BRIGHTDATA_API_KEY", "test-key")
    monkeypatch.setenv("BRIGHTDATA_BROWSER_WSS", "")
    monkeypatch.setattr(bd, "curated_config", lambda: {})
    url = "https://allaboutbattendisease.org/family-funds"
    monkeypatch.setattr(bd, "brightdata_request", lambda q: [
        {"title": "CLN5 family funds", "url": url, "snippet": "Family foundations for CLN5."}])
    monkeypatch.setattr(bd, "fetch_page", lambda u: (u, DIRECTORY))
    bd.emit(diseases=[DIS], per_disease=5)
    (e,) = read_jsonl("brightdata_orgs.edges.jsonl")
    (v,) = read_jsonl("brightdata_orgs.evidence.jsonl")
    assert e["status"] == "hypothesis" and v["quote"] is None and v["source_type"] == "web"
    assert e["attrs"]["search_snippet"] == "Family foundations for CLN5."


# ---- round 2: redirects, retries, browser gating -----------------------------------------------------
def test_redirect_to_curated_site_reuses_curated_id(interim, monkeypatch):
    monkeypatch.setenv("BRIGHTDATA_API_KEY", "test-key")
    monkeypatch.setenv("BRIGHTDATA_BROWSER_WSS", "")
    monkeypatch.setattr(bd, "curated_config", lambda: {"organizations": [
        {"id": "ORG:bdsra", "label": "BDSRA", "website": "https://bdsrafoundation.org"}]})
    monkeypatch.setattr(bd, "brightdata_request", lambda q: [
        {"title": "BDSRA", "url": "https://old-bdsra.org/cln5", "snippet": "Support for CLN5 families."}])
    page = "<p>BDSRA supports families living with CLN5 disease.</p>"
    monkeypatch.setattr(bd, "fetch_page", lambda u: ("https://bdsrafoundation.org/cln5", page))
    bd.emit(diseases=[DIS], per_disease=5)
    assert list(read_jsonl("brightdata_orgs.nodes.jsonl")) == []  # no ORG:old-bdsra-org duplicate
    (e,) = read_jsonl("brightdata_orgs.edges.jsonl")
    (v,) = read_jsonl("brightdata_orgs.evidence.jsonl")
    assert e["src"] == "ORG:bdsra" and v["url"] == "https://bdsrafoundation.org/cln5"
    assert v["quote"] == "BDSRA supports families living with CLN5 disease."


def test_fetch_page_caches_final_url(interim, monkeypatch):
    calls = []

    def fake_request(method, url, **kw):
        calls.append(kw.get("retries"))
        return httpx.Response(200, text="<p>hi</p>", request=httpx.Request("GET", "https://new.org/x"))
    monkeypatch.setattr(bd, "request", fake_request)
    assert bd.fetch_page("https://old.org/x-round2") == ("https://new.org/x", "<p>hi</p>")
    assert bd.fetch_page("https://old.org/x-round2") == ("https://new.org/x", "<p>hi</p>")  # cached
    assert len(calls) == 1


def test_brightdata_request_does_not_retry_inside(interim, monkeypatch):
    seen = {}

    def fake_request(method, url, **kw):
        seen.update(kw)
        return httpx.Response(200, json={"organic": [{"title": "t", "link": "https://a.org", "description": "d"}]})
    monkeypatch.setenv("BRIGHTDATA_API_KEY", "test-key")
    monkeypatch.setattr(bd, "request", fake_request)
    assert bd.brightdata_request("round2 retries query")[0]["url"] == "https://a.org"
    assert seen["retries"] == 1  # emit() owns the 2-attempt policy


@pytest.mark.parametrize("flag,visits", [("", False), ("1", True)])
def test_browser_only_for_failed_searches_unless_opted_in(interim, monkeypatch, flag, visits):
    monkeypatch.setenv("BRIGHTDATA_API_KEY", "test-key")
    monkeypatch.setenv("BRIGHTDATA_BROWSER_WSS", "wss://example.invalid")
    monkeypatch.setenv("ATLAS_BRIGHTDATA_BROWSER_VISITS", flag)
    monkeypatch.setattr(bd, "curated_config", lambda: {})
    other = {"id": "MONDO:0008767", "label": "neuronal ceroid lipofuscinosis 3", "synonyms": ["CLN3"], "attrs": {}}

    def fake_search(q):
        if '"CLN3"' in q:
            raise bd.BrightDataError("Bright Data error : redirect location was rejected")
        return [{"title": "CLN5 support group", "url": "https://cln5group.org/", "snippet": "CLN5 families."}]
    runs = []

    async def fake_browser(wss, jobs, per_disease, visit=False):
        runs.append(([j["disease"]["id"] for j in jobs], visit))
        for j in jobs:
            if j["leads"] is None:
                j["leads"] = []
    monkeypatch.setattr(bd, "brightdata_request", fake_search)
    monkeypatch.setattr(bd, "fetch_page", lambda u: (_ for _ in ()).throw(httpx.ConnectError("down")))
    monkeypatch.setattr(bd, "_browser_run", fake_browser)
    bd.emit(diseases=[DIS, other], per_disease=5)
    want = [(["MONDO:0008767"], False)] + ([([DIS["id"]], True)] if visits else [])
    assert runs == want
