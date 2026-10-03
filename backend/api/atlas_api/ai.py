"""POST /explain and /outreach-draft: OpenAI Structured Outputs grounded ONLY in
evidence loaded from the DB for the requested edge_ids (via api_edge)."""
from __future__ import annotations

import hashlib
import json
from typing import Any

from openai import AsyncOpenAI
from pydantic import BaseModel

from .config import Settings
from .data import Data
from .errors import bad_request, not_found, unavailable

# ---- LLM output schemas (subset of the contract; the server fills the rest) ----


class _Step(BaseModel):
    text: str
    edge_id: str


class _Explanation(BaseModel):
    headline: str
    steps: list[_Step]
    uncertainties: list[str]
    what_to_check_next: str


class _Citation(BaseModel):
    n: int
    edge_id: str


class _Outreach(BaseModel):
    subject: str
    body: str
    citations: list[_Citation]


SYSTEM_EXPLAIN = """You explain connections in a rare-disease knowledge graph.
Use ONLY the edges and evidence provided in the user message. Never add facts from memory.
Write one step per edge, in the given order; each step is ONE plain sentence and its edge_id
MUST be the id of the edge it describes. Respect edge status: 'curated' = established data,
'literature' = reported claim, 'inferred' = computed similarity, 'hypothesis' = untested idea;
word the sentence accordingly (never present inferred/hypothesis as fact).
List contradicting evidence and missing information under uncertainties.
Audience 'family': no jargon, short sentences. Audience 'expert': precise terminology."""

SYSTEM_OUTREACH = """You draft a short, polite first-contact email from a rare-disease patient
family/advocate to an organization or researcher. Use ONLY the facts in the provided evidence.
Cite facts in the body as [1], [2] ... and return one citation per number with the edge_id it
comes from (only edge_ids from the input). Plain text, under 220 words, no medical claims
beyond the evidence, clearly state that connections marked inferred/hypothesis are unconfirmed."""


def cache_key(audience: str, edge_ids: list[str]) -> str:
    return hashlib.sha1(f"{audience}|{','.join(edge_ids)}".encode()).hexdigest()


def _evidence_digest(er: dict) -> dict[str, Any]:
    """Compact, prompt-friendly view of one EdgeResponse."""
    e = er["edge"]

    def ev(items: list[dict]) -> list[dict]:
        return [{k: v.get(k) for k in ("source_name", "source_ref", "quote", "method")} for v in items[:6]]

    return {
        "edge_id": e["id"], "type": e["type"], "label": e.get("label"), "status": e["status"],
        "confidence": e["confidence"], "score": e.get("score"), "attrs": e.get("attrs") or {},
        "source": {k: er["source"].get(k) for k in ("id", "type", "label", "summary")},
        "target": {k: er["target"].get(k) for k in ("id", "type", "label", "summary")},
        "supporting": ev(er.get("supporting", [])), "contradicting": ev(er.get("contradicting", [])),
        "context": ev(er.get("context", [])),
    }


class AI:
    def __init__(self, settings: Settings, data: Data):
        self.s, self.data = settings, data
        self.client = AsyncOpenAI(api_key=settings.openai_api_key, timeout=40) if settings.openai_api_key else None

    def _require_client(self) -> AsyncOpenAI:
        if self.client is None:
            raise unavailable("AI is not configured on this server (OPENAI_API_KEY missing)")
        return self.client

    async def _load_edges(self, edge_ids: list[str]) -> list[dict]:
        out = []
        for eid in edge_ids:
            er = await self.data.edge_obj(eid)
            if er is None:
                raise not_found(f"edge {eid}")
            out.append(er)
        return out

    async def _parse(self, system: str, user: dict, schema: type[BaseModel]) -> BaseModel:
        client = self._require_client()
        try:
            resp = await client.chat.completions.parse(
                model=self.s.openai_model,
                messages=[{"role": "system", "content": system},
                          {"role": "user", "content": json.dumps(user, ensure_ascii=False, default=str)}],
                response_format=schema,
            )
        except Exception as exc:  # network, auth, quota
            raise unavailable(f"AI provider error: {type(exc).__name__}", 502) from exc
        parsed = resp.choices[0].message.parsed
        if parsed is None:
            raise unavailable("AI provider returned no structured output (refusal)", 502)
        return parsed

    # ---- explain ----------------------------------------------------------
    async def explain(self, edge_ids: list[str], audience: str) -> dict:
        if not 1 <= len(edge_ids) <= 8:
            raise bad_request("edge_ids must contain 1..8 edges")
        db = self.data.db if self.s.uses_db("explain") else None
        key = cache_key(audience, edge_ids)
        if db is not None:
            hit = await db.scalar("select payload::text from explanations where cache_key = %s", (key,))
            if hit:
                return {**json.loads(hit), "cached": True}
        if db is None:  # fixtures mode -> canned answer
            fx = self.data.fx.load("explain.json")
            if fx is not None:
                return fx
        edges = await self._load_edges(edge_ids)
        self._require_client()
        out: _Explanation = await self._parse(  # type: ignore[assignment]
            SYSTEM_EXPLAIN, {"audience": audience, "edges": [_evidence_digest(e) for e in edges]}, _Explanation)
        by_id = {e["edge"]["id"]: e["edge"] for e in edges}
        steps = [{"text": st.text, "edge_id": st.edge_id, "status": by_id[st.edge_id]["status"],
                  "confidence": by_id[st.edge_id]["confidence"]}
                 for st in out.steps if st.edge_id in by_id]  # drop any uncited/invented step
        payload = {"headline": out.headline, "steps": steps, "uncertainties": out.uncertainties,
                   "what_to_check_next": out.what_to_check_next, "model": self.s.openai_model, "cached": False}
        if db is not None:
            await db.scalar(
                "insert into explanations(cache_key, audience, edge_ids, payload, model) values (%s,%s,%s,%s::jsonb,%s)"
                " on conflict (cache_key) do nothing returning 1",
                (key, audience, edge_ids, json.dumps(payload), self.s.openai_model))
        return payload

    # ---- outreach ---------------------------------------------------------
    async def outreach(self, disease_id: str, target_id: str, edge_ids: list[str]) -> dict:
        if not 1 <= len(edge_ids) <= 12:
            raise bad_request("edge_ids must contain 1..12 edges")
        if not self.s.uses_db("outreach"):  # fixtures mode -> canned answer
            fx = self.data.fx.load("outreach-draft.json")
            if fx is not None:
                return fx
        disease, target = await self.data.node_obj(disease_id), await self.data.node_obj(target_id)
        if disease is None:
            raise not_found(f"node {disease_id}")
        if target is None:
            raise not_found(f"node {target_id}")
        edges = await self._load_edges(edge_ids)
        self._require_client()
        brief = lambda n: {k: n["node"].get(k) for k in ("id", "type", "label", "summary")}  # noqa: E731
        out: _Outreach = await self._parse(  # type: ignore[assignment]
            SYSTEM_OUTREACH,
            {"sender_disease": brief(disease), "recipient": {**brief(target), "attrs": target["node"].get("attrs")},
             "evidence": [_evidence_digest(e) for e in edges]},
            _Outreach)
        by_id = {e["edge"]["id"]: e for e in edges}
        citations = []
        for c in out.citations:
            er = by_id.get(c.edge_id)
            if er is None:
                continue
            ev = (er.get("supporting") or er.get("context") or [{}])[0]
            citations.append({"n": c.n, "edge_id": c.edge_id, "source_ref": ev.get("source_ref"), "url": ev.get("url")})
        return {"subject": out.subject, "body": out.body, "citations": citations, "model": self.s.openai_model}
