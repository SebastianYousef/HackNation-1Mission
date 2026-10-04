"""The ONLY module in the pipeline that talks to OpenAI (docs/ARCHITECTURE.md §6).

Used for exactly three things, everything else is deterministic:
  1. Extract    structured(...) / batch_*(...)  abstract -> {subject, relation, object, stance, quote}
  2. Reconcile  embed(...)                      name vectors for the top-5 cosine candidates of leftover names
                pick_candidate(...)             choose one of <=5 given ids or "none" for unclear ones
  3. Explain    rephrase(...)                   plain-language view text from supplied facts only
                                                (deterministic template fallback without a key)
Model from OPENAI_MODEL (default gpt-4.1-mini), embeddings from OPENAI_EMBED_MODEL (default
text-embedding-3-small). ATLAS_LLM=off forces templates.
Every call is cached on disk (data/interim/llm_cache/<ns>/<sha1>.json; embeddings in
data/interim/embedding_cache/<model>.{keys.json,npy}) so re-runs cost nothing.
Usage counters are persisted to data/interim/openai_usage.json -> dataset_meta('openai_usage').
Embedding spend is tracked there too and stops at OPENAI_BUDGET_USD (default 7, docs/RESUME.md section 3).
"""
from __future__ import annotations

import hashlib
import json
import logging
import threading
from pathlib import Path
from typing import Any, Callable, TypeVar

from pydantic import BaseModel

from .config import INTERIM, env
from .models import now_iso

log = logging.getLogger(__name__)
T = TypeVar("T", bound=BaseModel)
CACHE = INTERIM / "llm_cache"
USAGE_FILE = INTERIM / "openai_usage.json"
_lock = threading.Lock()


def model_name() -> str:
    return env("OPENAI_MODEL", "gpt-4.1-mini")


def available() -> bool:
    return bool(env("OPENAI_API_KEY")) and env("ATLAS_LLM", "on") != "off"


_client = None


def client():
    global _client
    if _client is None:
        from openai import OpenAI
        _client = OpenAI(max_retries=4, timeout=120)
    return _client


# ----------------------------------------------------------------------------- usage counters
def record_usage(section: str, **counts: Any) -> None:
    """Overwrite counters of one section (extract | reconcile | views) in openai_usage.json."""
    with _lock:
        data = json.loads(USAGE_FILE.read_text()) if USAGE_FILE.exists() else {}
        data[section] = {**counts, "model": model_name(), "updated_at": now_iso()}
        USAGE_FILE.parent.mkdir(parents=True, exist_ok=True)
        USAGE_FILE.write_text(json.dumps(data, indent=1))


def usage() -> dict:
    return json.loads(USAGE_FILE.read_text()) if USAGE_FILE.exists() else {}


# ----------------------------------------------------------------------------- core call + cache
def _cache_file(ns: str, key: str):
    return CACHE / ns / (hashlib.sha1(key.encode()).hexdigest() + ".json")


def cache_key(prompt_version: str, key: str) -> str:
    return f"{prompt_version}|{model_name()}|{key}"


def cached(ns: str, schema: type[T], key: str, prompt_version: str) -> T | None:
    f = _cache_file(ns, cache_key(prompt_version, key))
    return schema.model_validate_json(f.read_text()) if f.exists() else None


def _store(ns: str, key: str, prompt_version: str, obj: BaseModel) -> None:
    f = _cache_file(ns, cache_key(prompt_version, key))
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(obj.model_dump_json())


def structured(ns: str, system: str, user: str, schema: type[T], key: str, prompt_version: str) -> T | None:
    """One Structured-Outputs call; None when no key / on failure. Cached forever per (version, model, key)."""
    hit = cached(ns, schema, key, prompt_version)
    if hit is not None or not available():
        return hit
    try:
        c = client()
        if hasattr(c, "responses") and hasattr(c.responses, "parse"):
            out = c.responses.parse(model=model_name(), instructions=system, input=user, text_format=schema).output_parsed
        else:  # older SDKs
            out = c.beta.chat.completions.parse(model=model_name(), response_format=schema, messages=[
                {"role": "system", "content": system}, {"role": "user", "content": user}]).choices[0].message.parsed
    except Exception as e:  # never crash the pipeline on an LLM hiccup
        log.warning("LLM call failed (%s): %s", ns, e)
        return None
    if out is not None:
        _store(ns, key, prompt_version, out)
    return out


# ----------------------------------------------------------------------------- Batch API (offline extract)
BATCH_STATE = INTERIM / "llm_batch_{ns}.json"


def _strict_schema(schema: type[BaseModel]) -> dict:
    try:
        from openai.lib._pydantic import to_strict_json_schema
        return to_strict_json_schema(schema)
    except Exception:  # pragma: no cover - SDK internals moved
        return schema.model_json_schema()


def batch_step(ns: str, system: str, items: list[tuple[str, str]], schema: type[T], prompt_version: str) -> str:
    """Advance an OpenAI Batch job one step. items = [(key, user_prompt)] (uncached ones are sent).
    Returns 'submitted' | 'pending:<status>' | 'done' | 'nothing' | 'unavailable'.
    Call repeatedly (e.g. `extract --batch` every few minutes); results land in the normal cache."""
    state_f = INTERIM / f"llm_batch_{ns}.json"
    if not available():
        return "unavailable"
    c = client()
    if state_f.exists():
        st = json.loads(state_f.read_text())
        b = c.batches.retrieve(st["batch_id"])
        if b.status in ("validating", "in_progress", "finalizing", "cancelling"):
            return f"pending:{b.status}"
        if b.status == "completed" and b.output_file_id:
            text = c.files.content(b.output_file_id).text
            n_ok = 0
            for line in text.splitlines():
                r = json.loads(line)
                key = st["keys"].get(r.get("custom_id"))
                body = ((r.get("response") or {}).get("body") or {})
                try:
                    content = body["choices"][0]["message"]["content"]
                    _store(ns, key, prompt_version, schema.model_validate_json(content))
                    n_ok += 1
                except Exception as e:
                    log.warning("batch item %s unusable: %s", r.get("custom_id"), e)
            log.info("batch %s: %d results cached", st["batch_id"], n_ok)
        else:
            log.warning("batch %s ended with status %s", st["batch_id"], b.status)
        state_f.unlink()
        return "done"
    todo = [(k, u) for k, u in items if cached(ns, schema, k, prompt_version) is None]
    if not todo:
        return "nothing"
    js = _strict_schema(schema)
    lines, keys = [], {}
    for i, (k, u) in enumerate(todo):
        cid = f"{ns}-{i}"
        keys[cid] = k
        lines.append(json.dumps({"custom_id": cid, "method": "POST", "url": "/v1/chat/completions", "body": {
            "model": model_name(),
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": u}],
            "response_format": {"type": "json_schema",
                                "json_schema": {"name": schema.__name__, "schema": js, "strict": True}}}}))
    req = INTERIM / f"llm_batch_{ns}.requests.jsonl"
    req.write_text("\n".join(lines) + "\n")
    f = c.files.create(file=open(req, "rb"), purpose="batch")
    b = c.batches.create(input_file_id=f.id, endpoint="/v1/chat/completions", completion_window="24h")
    state_f.write_text(json.dumps({"batch_id": b.id, "keys": keys, "submitted_at": now_iso()}))
    log.info("submitted batch %s with %d requests", b.id, len(todo))
    return "submitted"


# ----------------------------------------------------------------------------- Embeddings (reconcile)
EMBED_CACHE = INTERIM / "embedding_cache"
EMBED_DIM = 1536            # nodes.embedding is vector(1536)
EMBED_BATCH = 256           # inputs per request (the API allows 2048)
USD_PER_MTOK = {"text-embedding-3-small": 0.02, "text-embedding-3-large": 0.13, "text-embedding-ada-002": 0.10}
_embed_stopped: str | None = None   # set on insufficient_quota / budget: no more requests this process


def embed_model() -> str:
    return env("OPENAI_EMBED_MODEL", "text-embedding-3-small")


def budget_usd() -> float:
    return float(env("OPENAI_BUDGET_USD", "7"))


def spent_usd() -> float:
    """Estimated spend recorded in openai_usage.json (sections that track `usd`)."""
    return sum(float(v.get("usd") or 0) for v in usage().values() if isinstance(v, dict))


def _add_usage(section: str, **inc: float) -> None:
    """Add to the cumulative counters of one section (unlike record_usage, which overwrites)."""
    with _lock:
        data = json.loads(USAGE_FILE.read_text()) if USAGE_FILE.exists() else {}
        old = data.get(section) or {}
        data[section] = {**old, **{k: round(old.get(k, 0) + v, 10) for k, v in inc.items()},
                         "model": embed_model(), "updated_at": now_iso()}
        USAGE_FILE.parent.mkdir(parents=True, exist_ok=True)
        USAGE_FILE.write_text(json.dumps(data, indent=1))


class EmbeddingCache:
    """sha1(text) -> unit vector (float32) for one model, kept as <model>.keys.json + <model>.npy."""

    def __init__(self, model: str) -> None:
        slug = "".join(ch if ch.isalnum() or ch in "-." else "_" for ch in model)
        self.keys_f: Path = EMBED_CACHE / f"{slug}.keys.json"
        self.vecs_f: Path = EMBED_CACHE / f"{slug}.npy"
        self.vecs: dict[str, Any] = {}
        self.dirty = False
        if self.keys_f.exists() and self.vecs_f.exists():
            import numpy as np
            keys, mat = json.loads(self.keys_f.read_text()), np.load(self.vecs_f)
            self.vecs = dict(zip(keys, mat))

    @staticmethod
    def key(text: str) -> str:
        return hashlib.sha1(text.encode()).hexdigest()

    def get(self, text: str):
        return self.vecs.get(self.key(text))

    def put(self, text: str, vec) -> None:
        import numpy as np
        v = np.asarray(vec, dtype=np.float32)
        n = float(np.linalg.norm(v))
        self.vecs[self.key(text)] = v / n if n else v
        self.dirty = True

    def save(self) -> None:
        if not self.dirty:
            return
        import numpy as np
        EMBED_CACHE.mkdir(parents=True, exist_ok=True)
        keys = list(self.vecs)
        tmp = self.vecs_f.with_suffix(".tmp.npy")
        np.save(tmp, np.stack([self.vecs[k] for k in keys]) if keys else np.zeros((0, EMBED_DIM), np.float32))
        tmp.replace(self.vecs_f)
        self.keys_f.write_text(json.dumps(keys))
        self.dirty = False


_embed_caches: dict[str, EmbeddingCache] = {}


def embedding_cache() -> EmbeddingCache:
    m = embed_model()
    if m not in _embed_caches:
        _embed_caches[m] = EmbeddingCache(m)
    return _embed_caches[m]


def _est_tokens(text: str) -> int:
    return len(text) // 3 + 1   # conservative for short biomedical names (~4 chars/token in English)


def embed(texts: list[str]) -> list[Any]:
    """Unit vectors (numpy float32) for texts, None where unavailable. Cached per (model, text); only
    uncached texts are sent, EMBED_BATCH per request. Without a key, after `insufficient_quota` or when the
    next request would pass OPENAI_BUDGET_USD, the rest stay None (callers keep exact matching)."""
    global _embed_stopped
    cache = embedding_cache()
    todo = list(dict.fromkeys(t for t in texts if t and cache.get(t) is None))
    if todo and available() and _embed_stopped is None:
        model = embed_model()
        price = float(env("OPENAI_EMBED_USD_PER_MTOK", str(USD_PER_MTOK.get(model, 0.13))))
        kw = {"dimensions": EMBED_DIM} if model.startswith("text-embedding-3") else {}
        try:
            for i in range(0, len(todo), EMBED_BATCH):
                chunk = todo[i:i + EMBED_BATCH]
                est = sum(map(_est_tokens, chunk)) * price / 1e6
                if spent_usd() + est > budget_usd():
                    _embed_stopped = "budget"
                    log.warning("embeddings: stopping, OPENAI_BUDGET_USD %.2f would be exceeded (spent %.4f)",
                                budget_usd(), spent_usd())
                    break
                try:
                    resp = client().embeddings.create(model=model, input=chunk, **kw)
                except Exception as e:
                    quota = getattr(e, "code", None) == "insufficient_quota" or "insufficient_quota" in str(e)
                    _embed_stopped = "insufficient_quota" if quota else "error"
                    log.warning("embeddings: request failed, keeping what is cached (%s)", e)
                    break
                for d in resp.data:
                    cache.put(chunk[d.index], d.embedding)
                tokens = int(getattr(resp.usage, "total_tokens", None) or getattr(resp.usage, "prompt_tokens", 0))
                _add_usage("embeddings", requests=1, inputs=len(chunk), tokens=tokens, usd=tokens * price / 1e6)
        finally:
            cache.save()
    return [cache.get(t) if t else None for t in texts]


def cached_embeddings(texts: list[str]) -> list[Any]:
    """Cache lookups only (no API call), e.g. for load filling nodes.embedding."""
    cache = embedding_cache()
    return [cache.get(t) if t else None for t in texts]


# ----------------------------------------------------------------------------- Reconcile leftovers
PICK_SYSTEM = """You map a biomedical name to an ontology identifier. You are given the NAME, the CONTEXT
sentence it came from, and at most 5 CANDIDATES (id + label + synonyms). Answer with the id of the
candidate that denotes the same entity, or "none" if no candidate clearly does. Never invent an id."""


class Pick(BaseModel):
    choice: str
    reason: str


def pick_candidate(name: str, context: str | None, candidates: list[dict]) -> str | None:
    """LLM picks one of <=5 candidate ids or None. Output is validated against the candidate ids."""
    cands = candidates[:5]
    if not cands:
        return None
    user = json.dumps({"name": name, "context": context, "candidates": cands}, ensure_ascii=False)
    out = structured("reconcile", PICK_SYSTEM, user, Pick, key=user, prompt_version="pick-v1")
    if out is None:
        return None
    ids = {c["id"] for c in cands}
    return out.choice if out.choice in ids else None


# ----------------------------------------------------------------------------- Explain (views text)
REPHRASE_SYSTEM = """You write for families of children with rare diseases and for busy clinicians.
Rewrite the FACTS into the requested fields in plain language (reading age ~12, no jargon without a
short gloss). Use ONLY the supplied facts: never add a claim, number, drug, organisation or outcome
that is not in FACTS. If a fact is marked uncertain, keep the uncertainty. Be concise."""


def rephrase(task: str, facts: dict[str, Any], schema: type[T], fallback: Callable[[], T],
             prompt_version: str = "rephrase-v1") -> tuple[T, str | None]:
    """Returns (object, model-or-None). `fallback` builds the deterministic template version."""
    key = task + "|" + json.dumps(facts, sort_keys=True, default=str)
    out = structured("rephrase", REPHRASE_SYSTEM,
                     f"TASK: {task}\nFACTS (JSON):\n{json.dumps(facts, default=str, indent=1)}",
                     schema, key, prompt_version)
    if out is not None:
        return out, model_name()
    return fallback(), None
