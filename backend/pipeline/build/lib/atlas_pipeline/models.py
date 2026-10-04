"""Pydantic models mirroring backend/db/migrations/*_schema.sql, enums from contract/atlas.ts.

`contract.check_enums()` asserts these Literal lists equal the unions in atlas.ts.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Literal, get_args

from pydantic import BaseModel, Field, field_validator, model_validator

NodeType = Literal["disease", "gene", "variant", "phenotype", "mechanism", "intervention",
                   "organization", "person", "publication", "trial", "grant", "asset"]
EdgeType = Literal[
    "gene_associated_with_disease", "variant_of_gene", "variant_associated_with_disease",
    "gene_in_mechanism", "disease_involves_mechanism", "disease_has_phenotype",
    "disease_subtype_of", "disease_similar_to", "intervention_targets_mechanism",
    "intervention_treats_disease", "trial_studies_disease", "trial_tests_intervention",
    "publication_about", "person_authored", "person_studies", "person_affiliated_with",
    "grant_funds_person", "grant_studies", "organization_funds_grant",
    "organization_serves_disease", "organization_maintains_asset", "asset_covers_disease",
    "asset_targets_gene"]
EdgeStatus = Literal["curated", "literature", "inferred", "hypothesis"]
EvidenceStance = Literal["supports", "contradicts", "context"]
SourceType = Literal["database", "publication", "trial_registry", "grant_database",
                     "patient_org_site", "web", "computed"]
PathKind = Literal["related_disease", "patient_group", "asset", "researcher", "trial", "intervention"]

NODE_TYPES = set(get_args(NodeType))
EDGE_TYPES = set(get_args(EdgeType))
EDGE_STATUSES = set(get_args(EdgeStatus))
STANCES = set(get_args(EvidenceStance))
SOURCE_TYPES = set(get_args(SourceType))
PATH_KINDS = set(get_args(PathKind))

# documented direction of each edge type: (allowed src types, allowed dst types)
EDGE_ENDPOINTS: dict[str, tuple[set[str], set[str]]] = {
    "gene_associated_with_disease": ({"gene"}, {"disease"}),
    "variant_of_gene": ({"variant"}, {"gene"}),
    "variant_associated_with_disease": ({"variant"}, {"disease"}),
    "gene_in_mechanism": ({"gene"}, {"mechanism"}),
    "disease_involves_mechanism": ({"disease"}, {"mechanism"}),
    "disease_has_phenotype": ({"disease"}, {"phenotype"}),
    "disease_subtype_of": ({"disease"}, {"disease"}),
    "disease_similar_to": ({"disease"}, {"disease"}),
    "intervention_targets_mechanism": ({"intervention"}, {"mechanism"}),
    "intervention_treats_disease": ({"intervention"}, {"disease"}),
    "trial_studies_disease": ({"trial"}, {"disease"}),
    "trial_tests_intervention": ({"trial"}, {"intervention"}),
    "publication_about": ({"publication"}, {"disease", "gene", "mechanism"}),
    "person_authored": ({"person"}, {"publication"}),
    "person_studies": ({"person"}, {"disease", "gene", "mechanism"}),
    "person_affiliated_with": ({"person"}, {"organization"}),
    "grant_funds_person": ({"grant"}, {"person"}),
    "grant_studies": ({"grant"}, {"disease", "gene", "mechanism"}),
    "organization_funds_grant": ({"organization"}, {"grant"}),
    "organization_serves_disease": ({"organization"}, {"disease"}),
    "organization_maintains_asset": ({"organization"}, {"asset"}),
    "asset_covers_disease": ({"asset"}, {"disease"}),
    "asset_targets_gene": ({"asset"}, {"gene"}),
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _clean_nan(d: Any) -> Any:
    if isinstance(d, dict):
        return {k: (None if isinstance(v, float) and v != v else v) for k, v in d.items()}
    return d


class Node(BaseModel):
    _nan = model_validator(mode="before")(classmethod(lambda cls, d: _clean_nan(d)))
    id: str
    type: NodeType
    subtype: str | None = None
    label: str
    description: str | None = None
    plain_summary: str | None = None
    synonyms: list[str] = Field(default_factory=list)
    xrefs: dict[str, list[str]] = Field(default_factory=dict)
    attrs: dict[str, Any] = Field(default_factory=dict)
    url: str | None = None


class Evidence(BaseModel):
    _nan = model_validator(mode="before")(classmethod(lambda cls, d: _clean_nan(d)))
    id: str
    edge_id: str
    stance: EvidenceStance = "supports"
    source_type: SourceType
    source_name: str
    source_ref: str | None = None
    url: str | None = None
    quote: str | None = None
    method: str = "curated"
    published_at: date | None = None
    retrieved_at: str = Field(default_factory=now_iso)

    @field_validator("published_at", mode="before")
    @classmethod
    def _date(cls, v: Any) -> Any:
        if isinstance(v, str):
            v = v.strip()
            if not v:
                return None
            if len(v) == 4 and v.isdigit():
                return f"{v}-01-01"
            if len(v) == 7:
                return f"{v}-01"
            return v[:10]
        return v


class Edge(BaseModel):
    id: str
    src: str
    dst: str
    type: EdgeType
    status: EdgeStatus
    confidence: float = 0.0          # finalised by confidence.edge_confidence at graph merge
    score: float | None = None
    label: str | None = None
    attrs: dict[str, Any] = Field(default_factory=dict)
