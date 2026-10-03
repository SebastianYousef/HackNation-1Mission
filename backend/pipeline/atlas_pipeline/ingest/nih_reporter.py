"""NIH RePORTER API v2 (no key; ~1 request/second).

POST https://api.reporter.nih.gov/v2/projects/search
  {"criteria": {"advanced_text_search": {"operator": "or", "search_field": "projecttitle,terms,abstracttext",
                                         "search_text": "\"<name1>\" \"<name2>\""},
                "fiscal_years": [...]},
   "include_fields": [...], "offset": 0, "limit": <n>, "sort_field": "fiscal_year", "sort_order": "desc"}
Fields : core_project_num -> grant node NIH:<core_project_num>. Center grants (U54/P01) return one row
             per subproject under the same core number; the node's label/description/amount come from the
             parent row (subproject_id null, latest fiscal year), fetched by project_nums if the text search
             only returned subprojects;
         project_title -> label; abstract_text -> description (first 1500 chars);
         fiscal_year / award_amount / agency_ic_admin.abbreviation -> attrs.fiscal_year / amount_usd / agency;
         principal_investigators[{profile_id, first_name, last_name, full_name}] -> person PERSON:<last>-<first>
             (attrs.nih_profile_id) + grant_funds_person (curated) ;
         organization.org_name/org_country -> organization ORG:<slug> (academic) + person_affiliated_with;
         agency_ic_admin -> organization ORG:nih-<ic> (funder) + organization_funds_grant.
Query  : label + synonyms, without acronyms (RePORTER matches 'INCL' in 'incl.'/'including').
Edges  : grant_studies (grant -> queried disease; literature; quote = an abstract/title sentence that
         mentions a query name as a whole phrase). A hit with no such sentence is dropped entirely, and
         so is one whose quote only matches an acronym (CLN3) while title + abstract carry no disease
         context word (yeast CLN1/CLN2/CLN3 genes are also written in upper case).
"""
from __future__ import annotations

import logging
import re

from ..config import slice_config
from ..http import post_json
from ..ids import org_id, person_id, slug
from ..store import GraphWriter
from ._common import Coverage, context_terms, is_abbrev, mentions, query_diseases, search_names

log = logging.getLogger(__name__)
API = "https://api.reporter.nih.gov/v2/projects/search"
FIELDS = ["ProjectNum", "CoreProjectNum", "ProjectTitle", "AbstractText", "FiscalYear", "AwardAmount",
          "AgencyIcAdmin", "PrincipalInvestigators", "Organization", "ProjectDetailUrl",
          "ProjectStartDate", "ProjectEndDate", "ApplId", "SubprojectId"]


def search(names: list[str], years: list[int], limit: int) -> dict:
    body = {"criteria": {"advanced_text_search": {"operator": "or", "search_field": "projecttitle,terms,abstracttext",
                                                  "search_text": " ".join(f'"{n}"' for n in names)},
                         "fiscal_years": years},
            "include_fields": FIELDS, "offset": 0, "limit": limit,
            "sort_field": "fiscal_year", "sort_order": "desc"}
    return post_json(API, body, ns="nih_reporter")


def parent_row(core: str, years: list[int]) -> dict | None:
    """The center grant's own row (subproject_id null) for a core number, latest fiscal year."""
    body = {"criteria": {"project_nums": [core], "fiscal_years": years}, "include_fields": FIELDS,
            "offset": 0, "limit": 500, "sort_field": "fiscal_year", "sort_order": "desc"}
    try:
        rows = post_json(API, body, ns="nih_reporter").get("results", [])
    except Exception as e:
        log.warning("reporter parent lookup failed for %s: %s", core, type(e).__name__)
        return None
    rows = [r for r in rows if not r.get("subproject_id") and r.get("core_project_num") == core]
    return max(rows, key=lambda r: (r.get("fiscal_year") or 0, r.get("award_amount") or 0), default=None)


def _sentence(text: str, names: list[str]) -> str | None:
    for s in re.split(r"(?<=[.!?])\s+", text or ""):
        if mentions(s, names):
            return s.strip()[:600]
    return None


def _quote(p: dict, d: dict, names: list[str]) -> str | None:
    """Abstract/title sentence naming the disease. An acronym-only match (CLN3) also needs disease context
    in title + abstract, as PubMed's build_term requires: a context_terms word, batten, lysosom(al),
    neurodegenerat(ive), or the acronym followed by 'disease'. Yeast CLN1-3 cyclin genes have none."""
    abstract, title = p.get("abstract_text") or "", p.get("project_title") or ""
    quote = _sentence(abstract, names) or _sentence(title, names)
    hit = mentions(quote, names) if quote else None
    if hit and is_abbrev(hit):
        ctx = [rf"\b{re.escape(w)}" for w in context_terms(d) + ["batten", "lysosom", "neurodegenerat"]]
        ctx.append(rf"\b{re.escape(hit)}[\s-]+disease")
        if not any(re.search(c, f"{title} {abstract}", re.I) for c in ctx):
            return None
    return quote


def _rank(p: dict) -> tuple:
    return (bool(p.get("subproject_id")), -(p.get("fiscal_year") or 0), -(p.get("award_amount") or 0))


def download() -> None:
    return None


def emit() -> None:
    lit = slice_config().literature
    n, years = int(lit.get("grants_per_disease", 25)), list(lit.get("grant_fiscal_years", [2024, 2025]))
    g = GraphWriter("nih_reporter")
    cov = Coverage("nih_reporter")
    hits: list[tuple[dict, str, dict, str]] = []  # (disease, core, row, quote)
    for d in query_diseases():
        names = search_names(d, k=3, abbreviations=False)
        try:
            res = search(names, years, n)
        except Exception as e:
            log.warning("reporter failed for %s: %s", d["id"], type(e).__name__)
            cov.add(d["id"], " OR ".join(names), None)
            continue
        cov.add(d["id"], " OR ".join(names), (res.get("meta") or {}).get("total"))
        for p in res.get("results", []):
            core = p.get("core_project_num") or p.get("project_num")
            quote = _quote(p, d, names)
            if core and quote:  # no sentence names the disease -> the text search hit something else
                hits.append((d, core, p, quote))

    # grant node facts from one representative row per core number (GraphWriter keeps the first label)
    best: dict[str, dict] = {}
    for _, core, p, _ in hits:
        if core not in best or _rank(p) < _rank(best[core]):
            best[core] = p
    for core, p in best.items():
        if p.get("subproject_id"):
            parent = parent_row(core, years)
            best[core] = parent or {**p, "project_title": f"{core} (center grant)", "abstract_text": None,
                                    "award_amount": None}
    for core, p in best.items():
        gid = f"NIH:{core}"
        ic = (p.get("agency_ic_admin") or {})
        g.node(id=gid, type="grant", label=p.get("project_title") or gid,
               description=(p.get("abstract_text") or "")[:1500] or None,
               url=p.get("project_detail_url") or f"https://reporter.nih.gov/project-details/{p.get('appl_id')}",
               attrs={"project_num": p.get("project_num"), "fiscal_year": p.get("fiscal_year"),
                      "amount_usd": p.get("award_amount"), "agency": ic.get("abbreviation"),
                      "start_date": p.get("project_start_date"), "end_date": p.get("project_end_date")})

    for d, core, p, quote in hits:
        gid = f"NIH:{core}"
        url = p.get("project_detail_url") or f"https://reporter.nih.gov/project-details/{p.get('appl_id')}"
        ic = (p.get("agency_ic_admin") or {})
        ev = dict(source_type="grant_database", source_name="NIH RePORTER", source_ref=gid, url=url, quote=quote,
                  method="algorithm:reporter_text_search", published_at=(p.get("project_start_date") or "")[:10] or None)
        g.edge("grant_studies", gid, d["id"], status="literature", label="funds research on", evidence=ev)
        if ic.get("abbreviation"):
            fid = f"ORG:nih-{slug(ic['abbreviation'])}"
            g.node(id=fid, type="organization", subtype="funder", label=ic.get("name") or ic["abbreviation"],
                   attrs={"country": "US", "website": "https://www.nih.gov"})
            g.edge("organization_funds_grant", fid, gid, status="curated", label="funds",
                   evidence=dict(ev, method="curated", quote=f"Administering IC: {ic.get('name')}"))
        org = p.get("organization") or {}
        oid = None
        if org.get("org_name"):
            oid = org_id(org["org_name"])
            g.node(id=oid, type="organization", subtype="academic", label=org["org_name"].title(),
                   attrs={"country": org.get("org_country"), "city": org.get("org_city")})
        for pi in p.get("principal_investigators") or []:
            if not pi.get("last_name"):
                continue
            pid = person_id(pi["last_name"], pi.get("first_name"))
            g.node(id=pid, type="person", subtype="researcher",
                   label=f"{(pi.get('first_name') or '').title()} {pi['last_name'].title()}".strip(),
                   attrs={"nih_profile_id": pi.get("profile_id"), "roles": ["researcher"],
                          "affiliation": (org.get("org_name") or "").title() or None})
            g.edge("grant_funds_person", gid, pid, status="curated", label="funds",
                   evidence=dict(ev, method="curated", quote=f"PI: {pi.get('full_name')}"))
            if oid:
                g.edge("person_affiliated_with", pid, oid, status="curated", label="works at",
                       evidence=dict(ev, method="curated", quote=f"{pi.get('full_name')} — {org.get('org_name')}"))
    cov.save()
    g.close()
