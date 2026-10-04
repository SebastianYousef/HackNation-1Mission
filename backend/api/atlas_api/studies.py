"""Researcher-published studies (contract v1.1.0): a research team publishes a study without an
account and keeps a private edit token to update it later. Only the token's sha256 is stored.

The public shape (ResearcherStudy in contract/atlas.ts) is built here for both backends:
Postgres (table researcher_studies) and, in DATA_MODE=fixtures only, an in-process dict for local
development (lost on restart, one replica). Counters are anonymous totals, never answers.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from .data import Data, Db

StudyKind = Literal["preclinical", "phase1", "phase1_2", "phase2", "phase2_3", "phase3", "phase4", "observational",
                    "natural_history", "registry", "diagnostic", "biomarker", "survey", "other"]
RecruitmentStatus = Literal["not_yet_recruiting", "recruiting", "enrolling_by_invitation", "active_not_recruiting",
                            "completed", "suspended", "terminated", "withdrawn"]
EVENT_KINDS = ("view", "screening_started", "screening_completed", "potential_match", "contact_clicked")
DATE = r"^\d{4}-(0[1-9]|1[0-2])(-(0[1-9]|[12]\d|3[01]))?$"
EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,190}\.[^@\s]{2,}$")
CONDITION_TYPES = {"disease", "phenotype"}


class Question(BaseModel):
    """A screening question the research team defines. `required` questions decide the
    potential-match result; the others are only passed on with an application."""
    id: str = Field(pattern=r"^[a-z0-9_-]{1,40}$")
    text: str = Field(min_length=3, max_length=300)
    type: Literal["yes_no", "number", "choice"]
    options: list[str] = Field(default_factory=list, max_length=12)
    accept: list[str] = Field(default_factory=list, max_length=12)  # yes_no: ["yes"] or ["no"]; choice: options
    min: float | None = None
    max: float | None = None
    unit: str | None = Field(default=None, max_length=30)
    required: bool = True

    @field_validator("options", "accept")
    @classmethod
    def _short(cls, v: list[str]) -> list[str]:
        v = [x.strip() for x in v if x and x.strip()]
        if any(len(x) > 120 for x in v):
            raise ValueError("each option is at most 120 characters")
        return v

    @model_validator(mode="after")
    def _consistent(self) -> "Question":
        if self.type == "yes_no" and not set(self.accept) <= {"yes", "no"}:
            raise ValueError("yes_no questions accept 'yes' or 'no'")
        if self.type == "choice":
            if len(self.options) < 2:
                raise ValueError("choice questions need at least 2 options")
            if not set(self.accept) <= set(self.options):
                raise ValueError("accepted answers must be among the options")
        if self.type == "number" and self.min is not None and self.max is not None and self.min > self.max:
            raise ValueError("min is larger than max")
        return self


class Location(BaseModel):
    facility: str | None = Field(default=None, max_length=200)
    city: str | None = Field(default=None, max_length=120)
    country: str = Field(min_length=2, max_length=80)


class Contact(BaseModel):
    name: str | None = Field(default=None, max_length=120)
    email: str | None = Field(default=None, max_length=254)
    url: str | None = Field(default=None, max_length=500)

    @field_validator("email")
    @classmethod
    def _email(cls, v: str | None) -> str | None:
        v = (v or "").strip() or None
        if v and not EMAIL.match(v):
            raise ValueError("not an email address")
        return v

    @field_validator("url")
    @classmethod
    def _url(cls, v: str | None) -> str | None:
        v = (v or "").strip() or None
        if v and not re.match(r"^https?://[^\s]+$", v):
            raise ValueError("url must start with http:// or https://")
        return v


class Eligibility(BaseModel):
    min_age: float | None = Field(default=None, ge=0, le=120)
    max_age: float | None = Field(default=None, ge=0, le=120)
    sex: Literal["all", "female", "male"] = "all"
    criteria: str = Field(default="", max_length=4000)

    @model_validator(mode="after")
    def _ages(self) -> "Eligibility":
        if self.min_age is not None and self.max_age is not None and self.min_age > self.max_age:
            raise ValueError("min_age is larger than max_age")
        return self


class StudyDraft(BaseModel):
    title: str = Field(min_length=3, max_length=200)
    summary: str = Field(min_length=10, max_length=2000)
    research_focus: str = Field(min_length=2, max_length=200)
    condition_ids: list[str] = Field(default_factory=list, max_length=10)
    condition_text: str | None = Field(default=None, max_length=200)
    kind: StudyKind
    status: RecruitmentStatus
    institution: str = Field(min_length=2, max_length=200)
    team: str | None = Field(default=None, max_length=300)
    locations: list[Location] = Field(default_factory=list, max_length=30)
    start_date: str | None = Field(default=None, pattern=DATE)
    end_date: str | None = Field(default=None, pattern=DATE)
    eligibility: Eligibility = Field(default_factory=Eligibility)
    screening: list[Question] = Field(default_factory=list, max_length=15)
    contact: Contact
    registry_id: str | None = Field(default=None, pattern=r"^NCT\d{8}$")
    published: bool = True

    @model_validator(mode="after")
    def _checks(self) -> "StudyDraft":
        self.condition_ids = list(dict.fromkeys(i.strip() for i in self.condition_ids if i.strip()))
        self.condition_text = (self.condition_text or "").strip() or None
        if not self.condition_ids and not self.condition_text:
            raise ValueError("name at least one condition")
        if not (self.contact.email or self.contact.url):
            raise ValueError("give a contact email or a contact page")
        if len({q.id for q in self.screening}) != len(self.screening):
            raise ValueError("screening question ids must be unique")
        if self.start_date and self.end_date and self.end_date < self.start_date:
            raise ValueError("end_date is before start_date")
        return self


class CreateRequest(BaseModel):
    study: StudyDraft


class TokenRequest(BaseModel):
    edit_token: str = Field(min_length=20, max_length=200)


class UpdateRequest(TokenRequest):
    study: StudyDraft


class EventRequest(BaseModel):
    kind: Literal["view", "screening_started", "screening_completed", "potential_match", "contact_clicked"]


class BadCondition(ValueError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_id() -> str:
    return "RS:" + secrets.token_hex(5)


def changed_fields(old: dict, new: dict) -> list[str]:
    keys = [k for k in StudyDraft.model_fields if k != "condition_ids"] + ["condition_ids"]
    return [k for k in keys if old.get(k) != new.get(k)]


def public(row: dict) -> dict:
    """Contract shape. Never includes the token hash or the counters."""
    d = row["data"]
    return {"id": row["id"], "source": "researcher", "verification": "researcher_submitted",
            **{k: d.get(k) for k in StudyDraft.model_fields if k != "published"},
            "conditions": d.get("conditions", []), "published": row["published"],
            "created_at": row["created_at"], "updated_at": row["updated_at"],
            "update_history": row["history"]}


def _ts(v: Any) -> str:
    return v.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if isinstance(v, datetime) else str(v)


class Studies:
    def __init__(self, data: Data, db: Db | None):
        self.data, self.db = data, db
        self.mem: dict[str, dict] = {}  # DATA_MODE=fixtures only: local development

    async def _conditions(self, ids: list[str]) -> list[dict]:
        out = []
        for i in ids:
            n = await self.data.node_obj(i)
            node = (n or {}).get("node")
            if not node or node.get("type") not in CONDITION_TYPES:
                raise BadCondition(f"unknown condition {i} (use a disease or symptom id from /search)")
            out.append({k: node.get(k) for k in ("id", "type", "subtype", "label", "summary")})
        return out

    async def _doc(self, draft: StudyDraft) -> dict:
        doc = draft.model_dump(mode="json")
        doc["conditions"] = await self._conditions(draft.condition_ids)
        return doc

    # ---- writes -----------------------------------------------------------------
    async def create(self, draft: StudyDraft) -> tuple[str, dict]:
        doc, token, sid, now = await self._doc(draft), secrets.token_urlsafe(24), new_id(), _now()
        history = [{"at": now, "fields": ["created"]}]
        if self.db is not None:
            await self.db.scalar(
                "insert into researcher_studies(id, token_hash, data, published, history) values (%s,%s,%s::jsonb,%s,%s::jsonb) returning id",
                (sid, _hash(token), json.dumps(doc), draft.published, json.dumps(history)))
            row = await self._row(sid)
        else:
            row = {"id": sid, "token_hash": _hash(token), "data": doc, "published": draft.published, "history": history,
                   "counts": {}, "created_at": now, "updated_at": now}
            self.mem[sid] = row
        assert row is not None
        return token, public(row)

    async def update(self, sid: str, token: str, draft: StudyDraft) -> dict | None:
        row = await self._authorized(sid, token)
        if row is None:
            return None
        doc, now = await self._doc(draft), _now()
        fields = changed_fields(row["data"], doc) or ["no changes"]
        history = (row["history"] + [{"at": now, "fields": fields}])[-50:]
        if self.db is not None:
            await self.db.scalar(
                "update researcher_studies set data=%s::jsonb, published=%s, history=%s::jsonb, updated_at=now() where id=%s returning id",
                (json.dumps(doc), draft.published, json.dumps(history), sid))
            row = await self._row(sid)
        else:
            row.update(data=doc, published=draft.published, history=history, updated_at=now)
        return public(row) if row else None

    async def event(self, sid: str, kind: str) -> bool:
        if kind not in EVENT_KINDS:
            return False
        if self.db is not None:
            r = await self.db.scalar(
                "update researcher_studies set counts = jsonb_set(counts, array[%s], to_jsonb(coalesce((counts->>%s)::int, 0) + 1)) "
                "where id=%s and published returning id", (kind, kind, sid))
            return r is not None
        row = self.mem.get(sid)
        if not row or not row["published"]:
            return False
        row["counts"][kind] = row["counts"].get(kind, 0) + 1
        return True

    # ---- reads ------------------------------------------------------------------
    async def _row(self, sid: str) -> dict | None:
        if self.db is None:
            return self.mem.get(sid)
        raw = await self.db.scalar(
            "select jsonb_build_object('id', id, 'token_hash', token_hash, 'data', data, 'published', published, "
            "'history', history, 'counts', counts, 'created_at', created_at, 'updated_at', updated_at) "
            "from researcher_studies where id=%s", (sid,))
        if raw is None:
            return None
        row = raw if isinstance(raw, dict) else json.loads(raw)
        row["created_at"], row["updated_at"] = _ts_str(row["created_at"]), _ts_str(row["updated_at"])
        return row

    async def _authorized(self, sid: str, token: str) -> dict | None:
        row = await self._row(sid)
        if row is None or not hmac.compare_digest(row["token_hash"], _hash(token)):
            return None
        return row

    async def get(self, sid: str) -> dict | None:
        row = await self._row(sid)
        return public(row) if row and row["published"] else None

    async def manage(self, sid: str, token: str) -> dict | None:
        row = await self._authorized(sid, token)
        if row is None:
            return None
        counts = {k: int(row["counts"].get(k, 0)) for k in EVENT_KINDS}
        return {"study": public(row), "stats": counts}

    async def list(self, condition: list[str] | None, q: str | None, limit: int) -> list[dict]:
        q = (q or "").strip().lower() or None
        if self.db is not None:
            raw = await self.db.scalar(
                "select coalesce(jsonb_agg(x order by x->>'updated_at' desc), '[]'::jsonb) from ("
                " select jsonb_build_object('id', id, 'token_hash', '', 'data', data, 'published', published,"
                "   'history', history, 'counts', '{}'::jsonb, 'created_at', created_at, 'updated_at', updated_at) x"
                " from researcher_studies"
                " where published"
                "   and (%s::text[] is null or data->'condition_ids' ?| %s::text[])"
                "   and (%s::text is null or lower(data->>'title') like '%%' || %s || '%%'"
                "        or lower(coalesce(data->>'condition_text','')) like '%%' || %s || '%%'"
                "        or lower(data->>'research_focus') like '%%' || %s || '%%'"
                "        or lower(data->>'institution') like '%%' || %s || '%%')"
                " order by updated_at desc limit %s) t",
                (condition, condition, q, _like(q), _like(q), _like(q), _like(q), limit))
            rows = raw if isinstance(raw, list) else json.loads(raw or "[]")
            for r in rows:
                r["created_at"], r["updated_at"] = _ts_str(r["created_at"]), _ts_str(r["updated_at"])
        else:
            rows = sorted((r for r in self.mem.values() if r["published"]), key=lambda r: r["updated_at"], reverse=True)
            if condition:
                rows = [r for r in rows if set(condition) & set(r["data"].get("condition_ids", []))]
            if q:
                rows = [r for r in rows if any(q in str(r["data"].get(k) or "").lower()
                                               for k in ("title", "condition_text", "research_focus", "institution"))]
            rows = rows[:limit]
        return [public(r) for r in rows]


def _like(q: str | None) -> str | None:
    return None if q is None else q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _ts_str(v: Any) -> str:
    """Postgres jsonb renders timestamptz as '2026-10-04T11:02:45.123+00:00'; the contract uses Z seconds."""
    s = str(v)
    m = re.match(r"^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})", s)
    if not m:
        return s
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return _ts(dt)
    except ValueError:
        return f"{m.group(1)}T{m.group(2)}Z"
