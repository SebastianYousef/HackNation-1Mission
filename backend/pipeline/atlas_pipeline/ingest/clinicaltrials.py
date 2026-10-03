"""ClinicalTrials.gov API v2.

GET https://clinicaltrials.gov/api/v2/studies?query.cond=<disease label>&pageSize=<n>&countTotal=true&format=json
Fields (protocolSection.*):
  identificationModule.nctId -> trial node id (NCTxxxxxxxx), briefTitle -> label
  statusModule.overallStatus / startDateStruct.date -> attrs.overall_status / start_date
  designModule.phases / studyType / enrollmentInfo.count -> attrs.phase / study_type / enrollment
  conditionsModule.conditions -> trial_studies_disease (curated if a condition string matches a slice
      disease name exactly/by synonym, else literature linked to the queried disease)
  armsInterventionsModule.interventions[{type,name}] -> intervention nodes ATLAS:int-<slug> + trial_tests_intervention
  contactsLocationsModule.overallOfficials[{name,affiliation,role}] -> person (clinician) + person_studies
  sponsorCollaboratorsModule.leadSponsor -> attrs.sponsor
  eligibilityModule.minimumAge/maximumAge -> attrs.eligibility
"""
from __future__ import annotations

import logging

from ..config import slice_config
from ..http import get_json
from ..ids import intervention_id, person_id
from ..store import GraphWriter
from ._common import Coverage, query_diseases
from ..reconcile import NameIndex

log = logging.getLogger(__name__)
API = "https://clinicaltrials.gov/api/v2/studies"
INT_SUBTYPE = {"DRUG": "small_molecule", "BIOLOGICAL": "enzyme_replacement", "GENETIC": "gene_therapy"}


def search(cond: str, n: int) -> tuple[list[dict], int | None]:
    d = get_json(API, {"query.cond": cond, "pageSize": min(n, 100), "countTotal": "true", "format": "json"},
                 ns="clinicaltrials")
    return d.get("studies", []), d.get("totalCount")


def download() -> None:
    return None


def emit() -> None:
    n = int(slice_config().literature.get("trials_per_disease", 50))
    idx = NameIndex.from_stage_files(types={"disease"})
    g = GraphWriter("clinicaltrials")
    cov = Coverage("clinicaltrials")
    for d in query_diseases():
        try:
            studies, total = search(d["label"], n)
        except Exception as e:
            log.warning("ctgov failed for %s: %s", d["id"], e)
            cov.add(d["id"], d["label"], None)
            continue
        cov.add(d["id"], d["label"], total)
        for s in studies:
            ps = s.get("protocolSection", {})
            idm, st, de = ps.get("identificationModule", {}), ps.get("statusModule", {}), ps.get("designModule", {})
            nct = idm.get("nctId")
            if not nct:
                continue
            url = f"https://clinicaltrials.gov/study/{nct}"
            conds = ps.get("conditionsModule", {}).get("conditions", [])
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
                      quote="Conditions: " + "; ".join(conds), method="curated",
                      published_at=(st.get("studyFirstPostDateStruct") or {}).get("date"))
            matched = set()
            for c in conds:
                hit = idx.exact(c)
                if hit:
                    matched.add(hit[0])
            for m in matched:
                g.edge("trial_studies_disease", nct, m, status="curated", label="studies", evidence=ev)
            if d["id"] not in matched:
                g.edge("trial_studies_disease", nct, d["id"], status="literature", label="studies",
                       evidence=dict(ev, method="algorithm:ctgov_condition_query"))
            for i in ints:
                name = (i.get("name") or "").strip()
                if not name or name.lower() in ("placebo", "no intervention"):
                    continue
                iid = intervention_id(name)
                g.node(id=iid, type="intervention", subtype=INT_SUBTYPE.get(i.get("type", ""), "other"), label=name,
                       attrs={"ctgov_type": i.get("type")})
                g.edge("trial_tests_intervention", nct, iid, status="curated", label="tests",
                       evidence=dict(ev, quote=f"{i.get('type')}: {name}"))
            for o in ps.get("contactsLocationsModule", {}).get("overallOfficials", []) or []:
                nm = (o.get("name") or "").replace(",", " ").split()
                nm = [t for t in nm if not t.rstrip(".").upper() in ("MD", "PHD", "DR", "PROF", "MSC", "DO")]
                if len(nm) < 2:
                    continue
                pid = person_id(nm[-1], nm[0])
                g.node(id=pid, type="person", subtype="clinician", label=" ".join(nm),
                       attrs={"affiliation": o.get("affiliation"), "roles": ["clinician"]})
                g.edge("person_studies", pid, d["id"], status="literature", label="leads trial on",
                       evidence=dict(ev, quote=f"{o.get('role')}: {o.get('name')} ({o.get('affiliation')})"))
    cov.save()
    g.close()
