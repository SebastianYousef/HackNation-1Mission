"""PubMed via NCBI E-utilities (esearch + efetch). NCBI_API_KEY optional (10 req/s vs 3 req/s).

esearch: https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi
         db=pubmed term=("<name>"[tiab] OR …) AND hasabstract  retmax=literature.pubmed_per_disease sort=relevance
efetch : https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi  db=pubmed retmode=xml id=<≤200 ids>
Fields : PMID -> PMID:<n> publication node (attrs pmid/year/journal/title), ArticleTitle -> label + quote,
         Abstract/AbstractText -> data/interim/pubmed_abstracts.jsonl (input to `extract`),
         AuthorList (first 2 + last author) -> person nodes PERSON:<last>-<first> (attrs.affiliation),
         MeshHeadingList -> attrs.mesh.
Edges  : publication_about (pub -> queried disease; literature; quote = title)
         person_authored  (person -> pub; curated bibliographic fact)
         person_studies   (person -> disease; literature; one evidence row per PMID, so confidence
                           grows with the number of independent papers)
"""
from __future__ import annotations

import logging
import xml.etree.ElementTree as ET

from ..config import env, slice_config
from ..http import get_json, get_text
from ..ids import person_id
from ..store import GraphWriter, write_jsonl
from ._common import Coverage, query_diseases, search_names

log = logging.getLogger(__name__)
EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"


def _params(**kw) -> dict:
    p = {"tool": "rare-disease-atlas", **kw}
    if env("NCBI_API_KEY"):
        p["api_key"] = env("NCBI_API_KEY")
    if env("NCBI_EMAIL"):
        p["email"] = env("NCBI_EMAIL")
    return p


def esearch(term: str, retmax: int) -> tuple[list[str], int]:
    d = get_json(f"{EUTILS}/esearch.fcgi", _params(db="pubmed", term=term, retmax=retmax, sort="relevance",
                                                     retmode="json"), ns="pubmed")
    r = d.get("esearchresult", {})
    return r.get("idlist", []), int(r.get("count", 0))


def efetch(pmids: list[str]) -> list[dict]:
    out = []
    for i in range(0, len(pmids), 200):
        batch = sorted(pmids[i:i + 200])
        xml = get_text(f"{EUTILS}/efetch.fcgi", _params(db="pubmed", retmode="xml", id=",".join(batch)), ns="pubmed")
        root = ET.fromstring(xml)
        for art in root.iter("PubmedArticle"):
            out.append(parse_article(art))
    return out


def _text(el) -> str:
    return "".join(el.itertext()).strip() if el is not None else ""


def parse_article(art) -> dict:
    mc = art.find("MedlineCitation")
    a = mc.find("Article")
    pmid = mc.findtext("PMID")
    abstract = " ".join(
        (f"{t.get('Label')}: " if t.get("Label") else "") + _text(t) for t in a.findall("Abstract/AbstractText"))
    year = a.findtext("Journal/JournalIssue/PubDate/Year") or (a.findtext("Journal/JournalIssue/PubDate/MedlineDate") or "")[:4]
    authors = []
    for au in a.findall("AuthorList/Author"):
        last = au.findtext("LastName")
        if not last:
            continue
        authors.append({"last": last, "fore": au.findtext("ForeName") or au.findtext("Initials") or "",
                        "affiliation": au.findtext("AffiliationInfo/Affiliation")})
    return {"pmid": pmid, "title": _text(a.find("ArticleTitle")), "abstract": abstract,
            "year": year if year and year.isdigit() else None, "journal": a.findtext("Journal/Title"),
            "authors": authors, "mesh": [_text(m.find("DescriptorName")) for m in mc.findall("MeshHeadingList/MeshHeading")]}


def download() -> None:
    return None


def emit() -> None:
    n_per = int(slice_config().literature.get("pubmed_per_disease", 20))
    g = GraphWriter("pubmed")
    cov = Coverage("pubmed")
    abstracts: dict[str, dict] = {}
    for d in query_diseases():
        names = search_names(d)
        term = "(" + " OR ".join(f'"{n}"[tiab]' for n in names) + ") AND hasabstract"
        try:
            ids, count = esearch(term, n_per)
        except Exception as e:  # keep going; coverage records the failure
            log.warning("pubmed esearch failed for %s: %s", d["id"], e)
            cov.add(d["id"], term, None)
            continue
        cov.add(d["id"], term, count)
        if not ids:
            continue
        for art in efetch(ids):
            pmid = art["pmid"]
            pid = f"PMID:{pmid}"
            url = f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"
            g.node(id=pid, type="publication", label=art["title"][:300] or pid, url=url,
                   attrs={"pmid": pmid, "year": int(art["year"]) if art["year"] else None,
                          "journal": art["journal"], "title": art["title"], "mesh": art["mesh"][:15]})
            ev = dict(source_type="publication", source_name="PubMed", source_ref=pid, url=url,
                      quote=art["title"], method="algorithm:pubmed_query", published_at=art["year"])
            g.edge("publication_about", pid, d["id"], status="literature", label="is about", evidence=ev)
            rec = abstracts.setdefault(pmid, {**art, "query_diseases": []})
            rec["query_diseases"].append(d["id"])
            au = art["authors"]
            picked = au[:2] + ([au[-1]] if len(au) > 2 else [])
            for pos, a in enumerate(picked):
                person = person_id(a["last"], a["fore"])
                g.node(id=person, type="person", subtype="researcher", label=f"{a['fore']} {a['last']}".strip(),
                       attrs={"affiliation": a["affiliation"], "roles": ["researcher"]})
                g.edge("person_authored", person, pid, status="curated", label="authored",
                       evidence=dict(source_type="publication", source_name="PubMed", source_ref=pid, url=url,
                                     quote=f"{a['last']} {a['fore']}" + (f" ({a['affiliation']})" if a["affiliation"] else ""),
                                     method="curated", published_at=art["year"]))
                g.edge("person_studies", person, d["id"], status="literature", label="publishes on",
                       evidence=dict(ev, method="algorithm:authorship"))
    write_jsonl("pubmed_abstracts.jsonl", abstracts.values())
    cov.save()
    g.close()
