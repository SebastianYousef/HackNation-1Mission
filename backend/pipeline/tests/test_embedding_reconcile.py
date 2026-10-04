"""Tier-3 reconcile (embeddings) and llm.embed, with fake embeddings and a fake OpenAI client (no network)."""
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from atlas_pipeline import extract, llm, reconcile
from atlas_pipeline.graph import load_graph
from atlas_pipeline.store import GraphWriter, read_json, read_jsonl, write_jsonl

DIM = 8
CLN5, CLN6 = "MONDO:0012588", "MONDO:0011142"
GENE = "HGNC:2076"
AUTO = "R-HSA-1"
ABSTRACT = ("Mutations in CLN5 cause late infantile Batten disease in children. CLN5 loss impairs autophagic flux "
            "in cultured neurons. CLN5 disease might involve autophagic flux defects in the brain.")
REC = {"pmid": "123", "title": "t", "abstract": ABSTRACT, "year": "2021", "query_diseases": []}


def vec(**w: float) -> np.ndarray:
    """Unit vector from axis weights, e.g. vec(a=1, b=0.3); axes a..h."""
    v = np.zeros(DIM, np.float32)
    for k, x in w.items():
        v["abcdefgh".index(k)] = x
    return v / np.linalg.norm(v)


class FakeEmbed:
    """text -> vector; unknown texts get None (like an uncached text without a key)."""

    def __init__(self, table: dict[str, np.ndarray]):
        self.table, self.calls = table, []

    def __call__(self, texts):
        self.calls.append(list(texts))
        return [self.table.get(t) for t in texts]


class FakeJudge:
    def __init__(self, answer=None):
        self.answer, self.calls = answer, []

    def __call__(self, name, context, cands):
        self.calls.append((name, context, [c["id"] for c in cands]))
        return self.answer


NODE_VECS = {
    "CLN5 disease; neuronal ceroid lipofuscinosis 5": vec(a=1),
    "CLN6 disease": vec(a=0.9, b=0.44),
    "autophagy": vec(c=1),
    "CLN5": vec(a=1),           # gene with the same vector as the disease: type must keep them apart
}


def _seed():
    g = GraphWriter("hgnc")
    g.node(id=GENE, type="gene", label="CLN5")
    g.node(id=CLN5, type="disease", label="CLN5 disease", synonyms=["neuronal ceroid lipofuscinosis 5"])
    g.node(id=CLN6, type="disease", label="CLN6 disease")
    g.node(id=AUTO, type="mechanism", label="autophagy")
    g.close()


def _run(claims, table, judge=None):
    rows, dropped = extract.check_quotes(REC, extract.Claims(claims=claims))
    assert dropped == 0
    write_jsonl("claims.jsonl", rows)
    emb, jg = FakeEmbed({**NODE_VECS, **table}), judge or FakeJudge()
    reconcile.run(embed=emb, judge=jg)
    edges = {(e["type"], e["src"], e["dst"]): e for e in read_jsonl("reconcile.edges.jsonl")}
    return edges, emb, jg


def _claim(s, rel, o, quote, stance="supports"):
    return extract.Claim(subject=s, relation=rel, object=o, stance=stance, quote=quote)


GENE_MECH = _claim("CLN5", "gene_in_mechanism", "autophagic flux",
                   "CLN5 loss impairs autophagic flux in cultured neurons.")
GENE_DIS = _claim("CLN5", "gene_associated_with_disease", "late infantile Batten disease",
                  "Mutations in CLN5 cause late infantile Batten disease in children.")


def test_confident_match_is_recorded_honestly(interim):
    _seed()
    edges, emb, judge = _run([GENE_MECH], {"autophagic flux": vec(c=1, d=0.2)})
    e = edges[("gene_in_mechanism", GENE, AUTO)]   # reached the existing node, no ATLAS:mech-* created
    assert e["status"] == "literature"             # the claim's status, never upgraded
    assert e["attrs"]["confidence_cap"] == reconcile.EMBED_CONF_CAP
    (m,) = e["attrs"]["name_match"]
    assert m["method"] == "embedding" and m["id"] == AUTO and m["name"] == "autophagic flux"
    assert 0.95 < m["score"] < 1
    assert list(read_jsonl("reconcile.nodes.jsonl")) == []
    assert judge.calls == []
    mp = pd.read_parquet(interim / "reconcile_mapping.parquet").set_index("name")
    assert mp.loc["autophagic flux", "method"] == "embedding"
    assert mp.loc["autophagic flux", "confidence"] <= 0.6
    assert pd.isna(mp.loc["CLN5", "score"]) and mp.loc["CLN5", "method"] == "label"
    # node + query texts went out in ONE batched call; load gets the embedded texts
    assert len(emb.calls) == 1 and "autophagy" in emb.calls[0] and "autophagic flux" in emb.calls[0]
    assert read_json("embedded_nodes") == {AUTO: "autophagy"}
    # graph merge keeps it literature with the cap; analytics paths read confidence_cap
    ge = load_graph().edges[e["id"]]
    assert ge["status"] == "literature" and ge["attrs"]["confidence_cap"] == 0.6


def test_close_top2_goes_to_judge_not_auto_accepted(interim):
    _seed()
    q = {"late infantile Batten disease": vec(a=1, b=0.2)}    # ~0.98 to CLN5, ~0.98 to CLN6
    edges, _, judge = _run([GENE_DIS], q, FakeJudge(None))
    assert ("gene_associated_with_disease", GENE, CLN5) not in edges
    assert ("gene_associated_with_disease", GENE, CLN6) not in edges
    assert judge.calls and set(judge.calls[0][2]) == {CLN5, CLN6}
    assert judge.calls[0][1].startswith("Mutations in CLN5")     # the quote is the judge's context
    mp = pd.read_parquet(interim / "reconcile_mapping.parquet").set_index("name")
    assert mp.loc["late infantile Batten disease", "method"] == "unresolved"


def test_judge_pick_is_kept_with_its_method(interim):
    _seed()
    q = {"late infantile Batten disease": vec(a=1, b=0.2)}
    edges, _, _ = _run([GENE_DIS], q, FakeJudge(CLN5))
    e = edges[("gene_associated_with_disease", GENE, CLN5)]
    assert e["status"] == "literature"
    assert e["attrs"]["name_match"][0]["method"] == "embedding+llm"
    mp = pd.read_parquet(interim / "reconcile_mapping.parquet").set_index("name")
    assert mp.loc["late infantile Batten disease", "confidence"] == reconcile.JUDGE_CONF


def test_judge_cannot_invent_an_id(interim):
    _seed()
    edges, _, _ = _run([GENE_DIS], {"late infantile Batten disease": vec(a=1, b=0.2)}, FakeJudge("MONDO:9999999"))
    assert not [k for k in edges if k[0] == "gene_associated_with_disease"]


def test_low_score_is_unresolved(interim):
    _seed()
    edges, _, judge = _run([GENE_MECH], {"autophagic flux": vec(c=0.5, e=1)})   # cos ~0.45
    assert ("gene_in_mechanism", GENE, AUTO) not in edges
    assert judge.calls == []
    assert [n["id"] for n in read_jsonl("reconcile.nodes.jsonl")] == ["ATLAS:mech-autophagic-flux"]


def test_same_type_only(interim):
    _seed()
    idx = reconcile.NameIndex.from_stage_files()
    ei = reconcile.EmbeddingIndex(idx, embed=FakeEmbed({**NODE_VECS, "ceroid gene five": vec(a=1)}),
                                  judge=FakeJudge(GENE))
    m = ei.match("ceroid gene five", {"disease"})
    assert m is not None and m.id == CLN5 and m.method == "embedding"   # the gene with the same vector never wins
    assert ei.match("ceroid gene five", {"gene"}).id == GENE
    assert ei.match("ceroid gene five", None) is None and ei.match("ceroid gene five", {"gene", "disease"}) is None


def test_partial_node_coverage_turns_the_type_off(interim):
    _seed()
    idx = reconcile.NameIndex.from_stage_files()
    table = {k: v for k, v in NODE_VECS.items() if k != "CLN6 disease"} | {"ceroid five": vec(a=1)}
    ei = reconcile.EmbeddingIndex(idx, embed=FakeEmbed(table), judge=FakeJudge())
    assert ei.match("ceroid five", {"disease"}) is None


def test_umbrella_term_never_matches_a_subtype(interim):
    _seed()
    idx = reconcile.NameIndex.from_stage_files()
    ei = reconcile.EmbeddingIndex(idx, embed=FakeEmbed({**NODE_VECS, "Batten disease": vec(a=1)}), judge=FakeJudge())
    assert ei.match("Batten disease", {"disease"}) is None


def test_speculative_claim_stays_hypothesis(interim):
    _seed()
    c = _claim("CLN5 disease", "disease_involves_mechanism", "autophagic flux",
               "CLN5 disease might involve autophagic flux defects in the brain.", stance="speculative")
    rows, _ = extract.check_quotes(REC, extract.Claims(claims=[c]))
    write_jsonl("claims.jsonl", rows)
    reconcile.run(embed=FakeEmbed({**NODE_VECS, "autophagic flux": vec(c=1, d=0.2)}), judge=FakeJudge())
    (e,) = [e for e in read_jsonl("reconcile.edges.jsonl") if e["dst"] == AUTO]
    assert e["status"] == "hypothesis" and e["attrs"]["name_match"][0]["method"] == "embedding"


def test_no_key_keeps_exact_matching(interim):
    """Default path (ATLAS_LLM=off in tests): llm.embed answers from the empty cache, tier 3 is off."""
    _seed()
    rows, _ = extract.check_quotes(REC, extract.Claims(claims=[GENE_MECH]))
    write_jsonl("claims.jsonl", rows)
    reconcile.run()
    assert [n["id"] for n in read_jsonl("reconcile.nodes.jsonl")] == ["ATLAS:mech-autophagic-flux"]
    assert read_json("embedded_nodes") == {}


# ----------------------------------------------------------------------------- llm.embed with a fake client
class FakeClient:
    def __init__(self, fail: Exception | None = None):
        self.requests, self.fail = [], fail
        self.embeddings = SimpleNamespace(create=self.create)

    def create(self, model, input, **kw):
        if self.fail:
            raise self.fail
        self.requests.append((model, list(input), kw))
        data = [SimpleNamespace(index=i, embedding=[float(len(t)), 1.0] + [0.0] * (llm.EMBED_DIM - 2))
                for i, t in enumerate(input)]
        return SimpleNamespace(data=data, usage=SimpleNamespace(prompt_tokens=10 * len(input),
                                                                total_tokens=10 * len(input)))


@pytest.fixture()
def openai(interim, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-not-a-key")
    monkeypatch.setenv("ATLAS_LLM", "on")
    monkeypatch.setattr(llm, "_embed_stopped", None)
    monkeypatch.setattr(llm, "_embed_caches", {})
    monkeypatch.setattr(llm, "EMBED_BATCH", 2)
    fake = FakeClient()
    monkeypatch.setattr(llm, "_client", fake)
    return fake


def test_embed_batches_caches_and_counts(openai):
    out = llm.embed(["a", "bb", "ccc", "a"])
    assert [len(r[1]) for r in openai.requests] == [2, 1]          # deduplicated, EMBED_BATCH per request
    assert openai.requests[0][0] == "text-embedding-3-small" and openai.requests[0][2] == {"dimensions": 1536}
    assert all(v is not None and abs(np.linalg.norm(v) - 1) < 1e-5 for v in out)
    assert np.allclose(out[0], out[3])
    u = llm.usage()["embeddings"]
    assert u["requests"] == 2 and u["tokens"] == 30 and u["usd"] == pytest.approx(30 * 0.02 / 1e6)
    llm.embed(["ccc", "bb"])
    assert len(openai.requests) == 2                                 # cached: nothing re-embedded
    llm._embed_caches.clear()
    assert llm.cached_embeddings(["a"])[0] is not None                # persisted on disk


def test_embed_stops_at_budget(openai, monkeypatch):
    monkeypatch.setenv("OPENAI_BUDGET_USD", "0")
    assert llm.embed(["a"]) == [None]
    assert openai.requests == [] and llm._embed_stopped == "budget"


def test_embed_budget_counts_other_sections(openai, monkeypatch):
    llm.record_usage("extract", usd=6.999999)
    monkeypatch.setenv("OPENAI_BUDGET_USD", "7")
    assert llm.embed(["x" * 3000]) == [None] and openai.requests == []


def test_embed_insufficient_quota_degrades(openai, monkeypatch):
    err = Exception("Error code: 429 - {'error': {'code': 'insufficient_quota'}}")
    monkeypatch.setattr(llm, "_client", FakeClient(fail=err))
    assert llm.embed(["a", "b"]) == [None, None]
    assert llm._embed_stopped == "insufficient_quota"
    monkeypatch.setattr(llm, "_client", FakeClient())
    assert llm.embed(["a"]) == [None]                               # no further requests this run
    assert llm._client.requests == []


def test_embed_without_key_never_calls(interim, monkeypatch):
    monkeypatch.setattr(llm, "_embed_caches", {})
    fake = FakeClient()
    monkeypatch.setattr(llm, "_client", fake)
    assert llm.embed(["a"]) == [None] and fake.requests == []


def test_load_reads_node_vectors_from_cache(openai):
    from atlas_pipeline import load
    _seed()
    llm.embed(["autophagy"])
    from atlas_pipeline.store import write_json
    write_json("embedded_nodes", {AUTO: "autophagy", "NOPE:1": "x"})
    emb = load.node_embeddings(load_graph())
    assert set(emb) == {AUTO}
    lit = emb[AUTO]
    assert lit.startswith("[") and lit.endswith("]") and len(lit[1:-1].split(",")) == llm.EMBED_DIM
