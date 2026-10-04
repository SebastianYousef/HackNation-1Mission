"""ClinicalTrials.gov API v2.

GET https://clinicaltrials.gov/api/v2/studies?query.cond=<disease label>&pageSize=<n>&countTotal=true&format=json
Fields (protocolSection.*):
  identificationModule.nctId -> trial node id (NCTxxxxxxxx), briefTitle -> label
  statusModule.overallStatus / startDateStruct.date -> attrs.overall_status / start_date
  designModule.phases / studyType / enrollmentInfo.count -> attrs.phase / study_type / enrollment
  conditionsModule.conditions -> trial_studies_disease. query.cond is fuzzy (it returns registries listing
      hundreds of conditions), so the queried disease is never linked just because the trial came back.
      The condition -> MONDO mapping is our string match, never `curated`:
        condition = a slice disease's id/xref or exact label   -> literature (quote = that condition)
        condition = a synonym/abbreviation (not an umbrella like "Batten disease" or a family's
                    umbrella_terms in config/slice.yaml)                -> hypothesis
        queried disease named in the title (whole phrase)      -> hypothesis (quote = title)
        otherwise                                              -> no edge
  armsInterventionsModule.interventions[{type,name}] -> intervention nodes ATLAS:int-<slug> + trial_tests_intervention
      (subtype from the name, see int_subtype; placebo/sham arms skipped)
  contactsLocationsModule.overallOfficials[{name,affiliation,role}] -> person (clinician) + person_studies, only
      to diseases linked at literature level (role placeholders like "Medical Director" skipped)
  sponsorCollaboratorsModule.leadSponsor -> attrs.sponsor
  eligibilityModule.minimumAge/maximumAge -> attrs.eligibility
"""
from __future__ import annotations

import logging
import re

from ..config import slice_config
from ..http import get_json
from ..ids import intervention_id, norm_name, person_id
from ..store import GraphWriter
from ._common import Coverage, mentions, query_diseases
from ..reconcile import NameIndex

log = logging.getLogger(__name__)
API = "https://clinicaltrials.gov/api/v2/studies"
# sponsors use these as umbrella terms; MONDO files "batten disease" as a synonym of the juvenile form only.
# Each family in config/slice.yaml adds its own (umbrella_terms: "mitochondrial disease", "epilepsy", ...).
UMBRELLA = {"batten disease", "batten s disease", "batten", "ncl"}


def umbrella() -> set[str]:
    """Normalised umbrella terms: the built-in NCL ones plus every slice family's umbrella_terms."""
    return UMBRELLA | {norm_name(t) for t in slice_config().umbrella_terms}
DEGREES = {"MD", "PHD", "DR", "PROF", "MSC", "DO", "MPH", "MBBS", "FRCP", "MS", "RN", "PHARMD"}
ROLE_WORDS = {"medical", "director", "monitor", "manager", "sponsor", "clinical", "study", "trial", "trials", "team",
              "call", "center", "central", "contact", "information", "development", "research", "program",
              "department", "officer", "operations", "affairs", "chief"}
PLACEBO = re.compile(r"\bplacebo\b|\bno intervention\b|\bsham\b", re.I)


def int_subtype(ctype: str | None, name: str) -> str:
    """Contract intervention subtype from CT.gov type + name. CT.gov's BIOLOGICAL/DRUG say nothing about the
    modality (vectors, cells and enzymes are filed under both), so only clear name cues count."""
    n = name.lower()
    if re.search(r"\bcare\b|education|training|counsel|testing|screening", n):
        return "other"
    if ctype == "GENETIC" or re.search(r"\baav|vector|gene therapy|lentivir", n):
        return "gene_therapy"
    if re.search(r"stem cell|\bcells?\b|transplant|\bhsct\b|-sc\b", n):
        return "other"
    if "oligonucleotide" in n or re.search(r"\w+rsen\b", n):
        return "antisense_oligonucleotide"
    enzyme = re.sub(r"\w*(?:release|increase|decrease|disease|database|purchase|phase)\b", " ", n)
    if ctype in ("BIOLOGICAL", "DRUG") and re.search(r"recombinant|\balfa\b|\w{3,}ase\b", enzyme):
        return "enzyme_replacement"
    return "other"


def official_name(raw: str | None, affiliation: str | None) -> tuple[str, str, str] | None:
    """(last, first, display) for a real person, None for role placeholders ('Medical Director, MD')."""
    toks = (raw or "").split(",")[0].split()  # degrees follow the first comma ("Dang Do, MD, PhD")
    while toks and (toks[-1].isupper() or "." in toks[-1]) and toks[-1].replace(".", "").upper() in DEGREES:
        toks.pop()
    if toks and toks[0].replace(".", "").upper() in ("DR", "PROF"):
        toks.pop(0)
    if len(toks) < 2 or any(t.lower().strip(".") in ROLE_WORDS for t in toks):
        return None
    if affiliation and norm_name(raw or "") == norm_name(affiliation):
        return None
    if len(toks[0]) >= 3 and toks[0].isupper() and not all(t.isupper() for t in toks[1:]):
        # surname-first ("DEHARO Jean Claude"); a 2-letter capital run is initials ("JC Smith")
        return toks[0], toks[1], " ".join(toks[1:] + [toks[0].title()])
    return toks[-1], toks[0], " ".join(toks)


def condition_match(idx: NameIndex, cond: str) -> tuple[str, str, str] | None:
    """(mondo_id, status, kind) for a CT.gov condition string, see module docstring."""
    hit = idx.exact(cond)
    if not hit:
        return None
    mid, kind, _ = hit
    if kind in ("xref", "label"):
        return mid, "literature", kind
    if norm_name(cond) in umbrella():
        return None
    return mid, "hypothesis", kind


def search(cond: str, n: int) -> tuple[list[dict], int | None]:
    d = get_json(API, {"query.cond": cond, "pageSize": min(n, 100), "countTotal": "true", "format": "json"},
                 ns="clinicaltrials")
    return d.get("studies", []), d.get("totalCount")


def download() -> None:
    return None


def _disease_names(d: dict) -> list[str]:
    names = [d["label"]] + list(d.get("synonyms") or []) + list((d.get("attrs") or {}).get("abbreviations") or [])
    um = umbrella()
    return [x for x in names if len(x.strip()) >= 4 and norm_name(x) not in um]


def emit() -> None:
    n = int(slice_config().literature.get("trials_per_disease", 50))
    idx = NameIndex.from_stage_files(types={"disease"})
    g = GraphWriter("clinicaltrials")
    cov = Coverage("clinicaltrials")
    found: dict[str, tuple[dict, list[dict]]] = {}  # nct -> (study, queried diseases that returned it)
    for d in query_diseases():
        try:
            studies, total = search(d["label"], n)
        except Exception as e:
            log.warning("ctgov failed for %s: %s", d["id"], type(e).__name__)
            cov.add(d["id"], d["label"], None)
            continue
        cov.add(d["id"], d["label"], total)
        for s in studies:
            nct = s.get("protocolSection", {}).get("identificationModule", {}).get("nctId")
            if nct:
                found.setdefault(nct, (s, []))[1].append(d)

    for nct, (s, queried) in found.items():
        ps = s.get("protocolSection", {})
        idm, st, de = ps.get("identificationModule", {}), ps.get("statusModule", {}), ps.get("designModule", {})
        url = f"https://clinicaltrials.gov/study/{nct}"
        conds = ps.get("conditionsModule", {}).get("conditions", []) or []
        ints = ps.get("armsInterventionsModule", {}).get("interventions", []) or []
        elig = ps.get("eligibilityModule", {})
        g.node(id=nct, type="trial", subtype=(de.get("studyType") or "").lower() or None,
               label=idm.get("briefTitle") or nct, description=idm.get("officialTitle"), url=url,
               attrs={"nct_id": nct, "phase": ",".join(de.get("phases", []) or []) or None,
                      "overall_status": st.get("overallStatus"),
                      "start_date": (st.get("startDateStruct") or {}).get("date"),
                      "enrollment": (de.get("enrollmentInfo") or {}).get("count"),
                      "study_type": de.get("studyType"), "conditions": conds,
                      "interventions": [i.get("name") for i in ints],
                      "sponsor": (ps.get("sponsorCollaboratorsModule", {}).get("leadSponsor") or {}).get("name"),
                      "eligibility": {k: elig.get(k) for k in ("minimumAge", "maximumAge", "sex") if elig.get(k)}})
        ev = dict(source_type="trial_registry", source_name="ClinicalTrials.gov", source_ref=nct, url=url,
                  method="curated", published_at=(st.get("studyFirstPostDateStruct") or {}).get("date"))

        # disease links: {mondo: (status, kind, [quoted condition strings])}
        links: dict[str, tuple[str, str, list[str]]] = {}
        for c in conds:
            m = condition_match(idx, c)
            if not m:
                continue
            mid, status, kind = m
            cur = links.get(mid)
            if cur is None or (status == "literature" and cur[0] != "literature"):
                links[mid] = (status, kind, [c])
            elif cur[0] == status:
                cur[2].append(c)
        for mid, (status, kind, cs) in links.items():
            g.edge("trial_studies_disease", nct, mid, status=status, label="studies",
                   evidence=dict(ev, quote="Condition: " + "; ".join(cs),
                                 method=f"algorithm:ctgov_condition_match:{kind}"))
        title = idm.get("officialTitle") or idm.get("briefTitle") or ""
        for d in queried:
            if d["id"] not in links and mentions(title, _disease_names(d)):
                g.edge("trial_studies_disease", nct, d["id"], status="hypothesis", label="studies",
                       evidence=dict(ev, quote=title, method="algorithm:ctgov_title_match"))

        for i in ints:
            name = (i.get("name") or "").strip()
            if not name or PLACEBO.search(name):
                continue
            iid = intervention_id(name)
            g.node(id=iid, type="intervention", subtype=int_subtype(i.get("type"), name), label=name,
                   attrs={"ctgov_type": i.get("type")})
            g.edge("trial_tests_intervention", nct, iid, status="curated", label="tests",
                   evidence=dict(ev, quote=f"{i.get('type')}: {name}"))

        lit = [mid for mid, (status, _, _) in links.items() if status == "literature"]
        for o in ps.get("contactsLocationsModule", {}).get("overallOfficials", []) or []:
            pn = official_name(o.get("name"), o.get("affiliation"))
            if not pn or not lit:
                continue
            last, first, display = pn
            pid = person_id(last, first)
            g.node(id=pid, type="person", subtype="clinician", label=display,
                   attrs={"affiliation": o.get("affiliation"), "roles": ["clinician"]})
            for mid in lit:
                g.edge("person_studies", pid, mid, status="literature", label="leads trial on",
                       evidence=dict(ev, method="algorithm:ctgov_official",
                                     quote=f"{o.get('role')}: {o.get('name')} ({o.get('affiliation')}) — "
                                           f"{nct}, condition: {'; '.join(links[mid][2])}"))
    cov.save()
    g.close()
