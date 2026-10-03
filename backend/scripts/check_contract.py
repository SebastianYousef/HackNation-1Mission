#!/usr/bin/env python3
"""Check a running Atlas API against contract/atlas.ts (v1). Stdlib only.

  python backend/scripts/check_contract.py --base-url http://localhost:8000
  python backend/scripts/check_contract.py --base-url https://localhost --insecure --ids ids.txt --with-ai

Discovers ids from /meta, /search, neighborhoods and clusters (plus an optional --ids
file, one id per line), calls every GET endpoint, validates exact key sets, types and
enum values, and checks referential integrity. Exit code 1 on any mismatch.

Schema mini-language (mirrors atlas.ts by hand — keep in sync):
  "str" "num" "int" "bool" "any"    scalar; suffix "?" = nullable (key must still be present)
  {"k": schema}                       object with EXACTLY these keys; "?k" = optional key;
                                      "...": schema = extra keys allowed with that value schema
  [schema]                            array (never null)
  frozenset({...})                    string enum
"""
from __future__ import annotations

import argparse
import json
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

# ---------------------------------------------------------------------------- schema
NODE_TYPE = frozenset("disease gene variant phenotype mechanism intervention organization person publication "
                      "trial grant asset".split())
EDGE_TYPE = frozenset("""gene_associated_with_disease variant_of_gene variant_associated_with_disease
  gene_in_mechanism disease_involves_mechanism disease_has_phenotype disease_subtype_of disease_similar_to
  intervention_targets_mechanism intervention_treats_disease trial_studies_disease trial_tests_intervention
  publication_about person_authored person_studies person_affiliated_with grant_funds_person grant_studies
  organization_funds_grant organization_serves_disease organization_maintains_asset asset_covers_disease
  asset_targets_gene""".split())
STATUS = frozenset("curated literature inferred hypothesis".split())
STANCE = frozenset("supports contradicts context".split())
SOURCE_TYPE = frozenset("database publication trial_registry grant_database patient_org_site web computed".split())
PATH_KIND = frozenset("related_disease patient_group asset researcher trial intervention".split())
ANYMAP = {"...": "any"}

NODE_BRIEF = {"id": "str", "type": NODE_TYPE, "subtype": "str?", "label": "str", "summary": "str?"}
NODE_FULL = {**NODE_BRIEF, "description": "str?", "synonyms": ["str"], "xrefs": {"...": ["str"]},
             "attrs": ANYMAP, "url": "str?"}
EDGE = {"id": "str", "src": "str", "dst": "str", "type": EDGE_TYPE, "label": "str?", "status": STATUS,
        "confidence": "num", "score": "num?", "support_count": "int", "contradict_count": "int",
        "sources": ["str"], "attrs": ANYMAP}
EVIDENCE = {"id": "str", "stance": STANCE, "source_type": SOURCE_TYPE, "source_name": "str", "source_ref": "str?",
            "url": "str?", "quote": "str?", "method": "str", "published_at": "str?", "retrieved_at": "str"}
NODE_TYPE_COUNTS = {**{f"?{t}": "int" for t in NODE_TYPE}}

META = {"contract_version": "str",
        "dataset": {"?version": "str", "?slice": "str", "?built_at": "str", "...": "any"},
        "counts": {"nodes": NODE_TYPE_COUNTS, "edges": "int", "evidence": "int", "clusters": "int"},
        "sources": ["str"]}
SEARCH = [{**NODE_BRIEF, "matched_name": "str",
           "match_kind": frozenset({"id", "label", "synonym", "xref", "abbreviation"}), "score": "num"}]
NODE_RESP = {"node": NODE_FULL, "degree": NODE_TYPE_COUNTS,
             "clusters": [{"id": "str", "label": "str", "size": "int", "membership": "num"}],
             "has_action_view": "bool", "has_mechanism_view": "bool"}
NEIGHBORHOOD = {"center": "str", "nodes": [NODE_BRIEF], "edges": [EDGE], "truncated": "bool"}
EDGE_RESP = {"edge": EDGE, "source": NODE_BRIEF, "target": NODE_BRIEF, "supporting": [EVIDENCE],
             "contradicting": [EVIDENCE], "context": [EVIDENCE]}
SIMILAR = [{"disease": NODE_BRIEF, "edge_id": "str", "score": "num?", "confidence": "num", "status": STATUS,
            "components": {"?phenotype": "num", "?mechanism": "num", "?gene": "num"}, "caution": "str?",
            "shared": {"phenotypes": [NODE_BRIEF], "mechanisms": [NODE_BRIEF], "genes": [NODE_BRIEF]},
            "same_cluster": "bool"}]
PATH = {"id": "str", "kind": PATH_KIND, "title": "str", "score": "num", "from": "str", "to": "str",
        "node_ids": ["str"], "edge_ids": ["str"], "nodes": [NODE_BRIEF], "edges": [EDGE],
        "weakest_status": STATUS, "min_confidence": "num", "attrs": ANYMAP}
CLUSTER_BRIEF = {"id": "str", "label": "str", "summary": "str?", "method": "str", "size": "int", "attrs": ANYMAP}
CLUSTER_RESP = {"cluster": CLUSTER_BRIEF, "members": [{**NODE_BRIEF, "membership": "num"}],
                "top_phenotypes": [NODE_BRIEF], "top_mechanisms": [NODE_BRIEF]}

ORG_CARD = {"node": NODE_BRIEF, "for_disease": NODE_BRIEF, "edge_id": "str", "website": "str?",
            "contact_url": "str?", "country": "str?", "has_registry": "bool?"}
ASSET_CARD = {"node": NODE_BRIEF, "asset_kind": "str", "owner": "NODE_BRIEF?",
              "covers": [NODE_BRIEF], "reusability": frozenset({"direct", "adaptable", "reference_only"}),
              "what_differs": ["str"], "needs_review": ["str"], "edge_ids": ["str"]}
TRIAL_CARD = {"node": NODE_BRIEF, "nct_id": "str", "phase": "str?", "overall_status": "str?", "intervention": "str?",
              "conditions": [NODE_BRIEF],
              "relevance": frozenset({"same_disease", "related_disease", "shared_mechanism"}),
              "eligibility_note": "str?", "url": "str", "edge_ids": ["str"]}
PERSON_ROLE = frozenset({"researcher", "clinician", "industry", "funder", "investor"})
PERSON_CARD = {"node": NODE_BRIEF, "affiliation": "str?", "roles": [PERSON_ROLE], "works_on": [NODE_BRIEF],
               "recent_publications": "int", "active_grants": "int", "contact_url": "str?", "edge_ids": ["str"]}
NEXT_STEP = {"id": "str",
             "kind": frozenset("contact_group join_registry adapt_study_design contact_researcher validate_experiment "
                               "apply_funding build_missing_group contribute_evidence".split()),
             "title": "str", "rationale": "str", "target": "NODE_BRIEF?",
             "status": frozenset({"viable", "needs_review", "unsupported"}), "validation_needed": ["str"],
             "effort": frozenset({"this_week", "this_month", "this_quarter"}), "edge_ids": ["str"]}
ACTION_VIEW = {
    "disease": NODE_FULL, "headline": "str", "plain_summary": "str",
    "treatment_status": {"has_approved_treatment": "bool?", "note": "str", "edge_ids": ["str"]},
    "exact_groups": [ORG_CARD],
    "related_communities": [{"disease": NODE_BRIEF, "similarity": "num", "similarity_edge_id": "str", "why": "str",
                             "differences": ["str"], "caution": "str?", "groups": [ORG_CARD]}],
    "connections": [PATH], "assets": [ASSET_CARD], "trials": [TRIAL_CARD], "researchers": [PERSON_CARD],
    "shared_people": [{"person": PERSON_CARD, "communities": [NODE_BRIEF], "edge_ids": ["str"]}],
    "next_steps": [NEXT_STEP],
    "coverage": {"has_supported_route": "bool",
                 "sources": [{"name": "str", "checked": "bool", "query": "str?", "result_count": "int?",
                              "checked_at": "str?"}],
                 "gaps": [{"question": "str", "why_it_matters": "str", "what_would_resolve": "str"}]},
}
MECHANISM_VIEW = {
    "mechanism": NODE_FULL, "headline": "str", "plain_summary": "str", "genes": [NODE_BRIEF],
    "diseases": [{"disease": NODE_BRIEF, "score": "num", "status": STATUS, "evidence_edge_ids": ["str"],
                  "cluster": "CLUSTER_REF?", "groups": [ORG_CARD], "assets": [ASSET_CARD], "unmet_need": "str?"}],
    "clusters": [{"cluster": CLUSTER_BRIEF, "disease_ids": ["str"], "score": "num"}],
    "researchers": [PERSON_CARD], "trials": [TRIAL_CARD], "interventions": [NODE_BRIEF],
}
EXPLANATION = {"headline": "str", "steps": [{"text": "str", "edge_id": "str", "status": STATUS, "confidence": "num"}],
               "uncertainties": ["str"], "what_to_check_next": "str", "model": "str?", "cached": "bool"}
JOB_STATUS = {"job_id": "str", "state": frozenset({"queued", "running", "done", "failed"}),
              "result": "JOB_RESULT?", "error": "str?"}
API_ERROR = {"error": {"code": frozenset({"not_found", "bad_request", "rate_limited", "upstream_unavailable",
                                          "internal"}), "message": "str", "request_id": "str"}}
# nullable object references (resolved by name so "X?" works for objects too)
NAMED: dict[str, Any] = {
    "NODE_BRIEF": NODE_BRIEF, "CLUSTER_REF": {"id": "str", "label": "str"},
    "JOB_RESULT": {"leads": [{"title": "str", "url": "str", "snippet": "str",
                              "kind": frozenset({"patient_group", "registry", "study", "news", "other"})}],
                   "disclaimer": "str"},
}


def validate(value: Any, schema: Any, path: str = "$") -> list[str]:
    errs: list[str] = []
    if isinstance(schema, str):
        nullable = schema.endswith("?")
        base = schema.rstrip("?")
        if value is None:
            return [] if nullable or base == "any" else [f"{path}: null not allowed ({base})"]
        if base in NAMED:
            return validate(value, NAMED[base], path)
        ok = {"str": isinstance(value, str), "bool": isinstance(value, bool), "any": True,
              "int": isinstance(value, int) and not isinstance(value, bool),
              "num": isinstance(value, (int, float)) and not isinstance(value, bool)}[base]
        return [] if ok else [f"{path}: expected {base}, got {type(value).__name__} {str(value)[:40]!r}"]
    if isinstance(schema, frozenset):
        return [] if isinstance(value, str) and value in schema else [f"{path}: {value!r} not in enum {sorted(schema)[:6]}..."]
    if isinstance(schema, list):
        if not isinstance(value, list):
            return [f"{path}: expected array, got {type(value).__name__}"]
        for i, item in enumerate(value):
            errs += validate(item, schema[0], f"{path}[{i}]")
        return errs
    if isinstance(schema, dict):
        if not isinstance(value, dict):
            return [f"{path}: expected object, got {type(value).__name__}"]
        extra_schema = schema.get("...")
        known = {k.lstrip("?"): (k.startswith("?"), s) for k, s in schema.items() if k != "..."}
        for k, (optional, s) in known.items():
            if k in value:
                errs += validate(value[k], s, f"{path}.{k}")
            elif not optional:
                errs.append(f"{path}: missing key {k!r}")
        for k in value.keys() - known.keys():
            if extra_schema is None:
                errs.append(f"{path}: unexpected key {k!r}")
            else:
                errs += validate(value[k], extra_schema, f"{path}.{k}")
        return errs
    raise TypeError(schema)


# ---------------------------------------------------------------------------- integrity
# These run only on bodies that passed validate() (Checker.get returns None otherwise), so they may index freely.
def check_edge_obj(e: dict, path: str) -> list[str]:
    return [] if 0 <= e["confidence"] <= 1 else [f"{path}: edge {e['id']} confidence {e['confidence']} out of 0..1"]


def check_neighborhood(nb: dict, path: str) -> list[str]:
    ids = {n["id"] for n in nb["nodes"]}
    errs = [] if nb["center"] in ids else [f"{path}: center {nb['center']} not in nodes"]
    for e in nb["edges"]:
        errs += check_edge_obj(e, path)
        for end in ("src", "dst"):
            if e[end] not in ids:
                errs.append(f"{path}: edge {e['id']} {end}={e[end]} not in nodes")
    return errs


def check_path(p: dict, path: str) -> list[str]:
    errs = []
    if len(p["node_ids"]) < 2:
        errs.append(f"{path}: node_ids must have >= 2 entries")
    if len(set(p["node_ids"])) != len(p["node_ids"]):
        errs.append(f"{path}: node_ids repeat a node (must be a simple path)")
    if len(p["edge_ids"]) != len(p["node_ids"]) - 1:
        errs.append(f"{path}: len(edge_ids) != len(node_ids)-1")
    if [n["id"] for n in p["nodes"]] != p["node_ids"]:
        errs.append(f"{path}: nodes order != node_ids")
    if [e["id"] for e in p["edges"]] != p["edge_ids"]:
        errs.append(f"{path}: edges order != edge_ids")
    if p["node_ids"] and (p["node_ids"][0] != p["from"] or p["node_ids"][-1] != p["to"]):
        errs.append(f"{path}: node_ids must start at from and end at to")
    for i, e in enumerate(p["edges"][: len(p["node_ids"]) - 1]):
        if {e["src"], e["dst"]} != {p["node_ids"][i], p["node_ids"][i + 1]}:
            errs.append(f"{path}: edge {e['id']} does not connect {p['node_ids'][i]} and {p['node_ids'][i + 1]}")
    # A path is never stronger than its weakest edge. It equals the edge minimum unless an honesty cap
    # (attrs.status_cap / attrs.confidence_cap) lowers it; then it equals the cap.
    order = ["curated", "literature", "inferred", "hypothesis"]
    attrs = p.get("attrs") or {}
    if p["edges"] and p["weakest_status"] in order:
        edge_min = max((e["status"] for e in p["edges"]), key=order.index)
        cap = attrs.get("status_cap")
        want = max(edge_min, cap, key=order.index) if cap in order else edge_min
        if order.index(p["weakest_status"]) < order.index(edge_min):
            errs.append(f"{path}: weakest_status is stronger than the weakest edge status ({edge_min})")
        elif p["weakest_status"] != want:
            errs.append(f"{path}: weakest_status {p['weakest_status']} != {want}"
                        f" ({'status_cap' if cap else 'weakest edge status'})")
    # tolerance: the database stores confidence as float4
    if p["edges"] and isinstance(p["min_confidence"], (int, float)):
        edge_min = min(e["confidence"] for e in p["edges"])
        cap = attrs.get("confidence_cap")
        want = min(edge_min, cap) if isinstance(cap, (int, float)) else edge_min
        if p["min_confidence"] > edge_min + 1e-6:
            errs.append(f"{path}: min_confidence is above the minimum edge confidence ({edge_min})")
        elif abs(p["min_confidence"] - want) > 1e-6:
            errs.append(f"{path}: min_confidence {p['min_confidence']} != {want}"
                        f" ({'confidence_cap' if cap is not None else 'minimum edge confidence'})")
    for e in p["edges"]:
        errs += check_edge_obj(e, path)
    return errs


def check_edge(er: dict, path: str) -> list[str]:
    e, errs = er["edge"], []
    if er["source"]["id"] != e["src"] or er["target"]["id"] != e["dst"]:
        errs.append(f"{path}: source/target do not match edge src/dst")
    for bucket, stance in (("supporting", "supports"), ("contradicting", "contradicts"), ("context", "context")):
        errs += [f"{path}.{bucket}: evidence {v['id']} has stance {v['stance']}" for v in er[bucket]
                 if v["stance"] != stance]
    errs += check_edge_obj(e, path)
    # honest statuses (root CLAUDE.md rule 2)
    rows = er["supporting"] + er["contradicting"] + er["context"]
    if not rows:
        errs.append(f"{path}: edge has no evidence rows")
    if e["status"] == "curated" and any(v["method"].startswith("llm:") for v in rows):
        errs.append(f"{path}: curated edge has llm:* evidence (LLM output is never curated)")
    return errs


# ---------------------------------------------------------------------------- runner
class Checker:
    def __init__(self, base: str, insecure: bool, verbose: bool, allow_missing: bool = False):
        self.base, self.verbose, self.allow_missing = base.rstrip("/"), verbose, allow_missing
        self.skipped = 0
        self.ctx = ssl._create_unverified_context() if insecure else None  # noqa: S323 (dev certs)
        self.errors: list[str] = []
        self.calls = 0
        self.served_by: dict[str, int] = {}

    def request(self, method: str, path: str, body: Any = None) -> tuple[int, Any]:
        url = self.base + path
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json",
                                                                              "Accept": "application/json"})
        self.calls += 1
        try:
            with urllib.request.urlopen(req, context=self.ctx, timeout=60) as r:
                status, raw, headers = r.status, r.read(), r.headers
        except urllib.error.HTTPError as e:
            status, raw, headers = e.code, e.read(), e.headers
        for h in ("X-Request-Id", "X-Served-By"):
            if not headers.get(h):
                self.errors.append(f"{method} {path}: missing header {h}")
        sb = headers.get("X-Served-By") or "?"
        self.served_by[sb] = self.served_by.get(sb, 0) + 1
        if self.verbose:
            print(f"  {status} {method} {path} [{sb}]")
        try:
            return status, json.loads(raw) if raw else None
        except json.JSONDecodeError:
            self.errors.append(f"{method} {path}: body is not JSON")
            return status, None

    def get(self, path: str, schema: Any, expect: int = 200) -> Any:
        status, body = self.request("GET", path)
        if status == 404 and expect == 200 and self.allow_missing:
            self.skipped += 1  # partial fixture set: a 404 is fine as long as it has the ApiError shape
            self.errors += [f"GET {path} (404): {e}" for e in validate(body, API_ERROR)]
            return None
        if status != expect:
            self.errors.append(f"GET {path}: status {status} (expected {expect})")
            return None
        if status >= 400:
            schema = API_ERROR
        errs = validate(body, schema)
        self.errors += [f"GET {path}: {e}" for e in errs]
        return None if errs else body  # integrity checks only see schema-valid bodies (no crash, no lost report)


def enc(i: str) -> str:
    return urllib.parse.quote(i, safe="")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base-url", default="http://localhost:8000")
    ap.add_argument("--ids", help="file with extra node ids (one per line)")
    ap.add_argument("--queries", default="cln,batten,lysosom,disease,seiz,gene",
                    help="comma list of search queries used to discover ids")
    ap.add_argument("--max-nodes", type=int, default=60, help="max node ids to crawl")
    ap.add_argument("--max-edges", type=int, default=80, help="max edge ids to check")
    ap.add_argument("--with-ai", action="store_true", help="also POST /explain and gap-search (may cost money)")
    ap.add_argument("--insecure", action="store_true", help="skip TLS verification (self-signed dev cert)")
    ap.add_argument("--allow-missing", action="store_true",
                    help="treat 404 on discovered ids as skipped (partial fixture sets); never use against db mode")
    ap.add_argument("--allow-empty", action="store_true",
                    help="do not fail when meta reports data but discovery found none (intentionally empty DB)")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()
    c = Checker(a.base_url, a.insecure, a.verbose, a.allow_missing)

    meta = c.get("/api/v1/meta", META)
    if meta and meta.get("contract_version") != "1.0.0":
        c.errors.append(f"meta.contract_version is {meta.get('contract_version')}, checker expects 1.0.0")

    node_ids: list[str] = []
    seen_nodes: set[str] = set()

    def add_node(i: str) -> None:
        if i not in seen_nodes:
            seen_nodes.add(i)
            node_ids.append(i)

    if a.ids:
        for line in open(a.ids, encoding="utf-8"):
            if line.strip() and not line.startswith("#"):
                add_node(line.strip())
    for q in a.queries.split(","):
        for hit in c.get(f"/api/v1/search?q={enc(q)}&limit=20", SEARCH) or []:
            add_node(hit["id"])
    c.get("/api/v1/search?q=x", SEARCH)  # too-short query must still be a valid (empty) list

    clusters = c.get("/api/v1/clusters", [CLUSTER_BRIEF]) or []
    for cl in clusters:
        cr = c.get(f"/api/v1/clusters/{enc(cl['id'])}", CLUSTER_RESP)
        for m in (cr or {}).get("members", [])[:5]:
            add_node(m["id"])

    edge_ids: list[str] = []
    path_edges: list[str] = []
    hits = {"nodes": 0, "edges": 0, "paths": 0, "action_views": 0, "mechanism_views": 0}
    i = 0
    while i < len(node_ids) and i < a.max_nodes:
        nid = node_ids[i]
        i += 1
        nr = c.get(f"/api/v1/nodes/{enc(nid)}", NODE_RESP)
        if not nr:
            continue
        hits["nodes"] += 1
        if nr["node"]["id"] != nid:
            c.errors.append(f"GET /nodes/{nid}: node.id is {nr['node']['id']}")
        nb = c.get(f"/api/v1/nodes/{enc(nid)}/neighborhood?depth=1&max_nodes=30", NEIGHBORHOOD)
        if nb:
            c.errors += check_neighborhood(nb, f"neighborhood({nid})")
            if nb["center"] != nid:
                c.errors.append(f"neighborhood({nid}): center is {nb['center']}")
            edge_ids += [e["id"] for e in nb["edges"]]
            for n in nb["nodes"][:4]:
                add_node(n["id"])
        if nr["has_action_view"]:
            av = c.get(f"/api/v1/diseases/{enc(nid)}/action-view", ACTION_VIEW)
            if av:
                hits["action_views"] += 1
                for k, p in enumerate(av["connections"]):
                    c.errors += check_path(p, f"action-view({nid}).connections[{k}]")
        if nr["has_mechanism_view"]:
            hits["mechanism_views"] += bool(c.get(f"/api/v1/mechanisms/{enc(nid)}/view", MECHANISM_VIEW))
        if nr["node"]["type"] == "disease":
            for s in c.get(f"/api/v1/diseases/{enc(nid)}/similar?limit=5", SIMILAR) or []:
                edge_ids.append(s["edge_id"])
            paths = c.get(f"/api/v1/paths?from={enc(nid)}&limit=10", [PATH]) or []
            hits["paths"] += len(paths)
            for k, p in enumerate(paths):
                c.errors += check_path(p, f"paths({nid})[{k}]")
                path_edges = path_edges or p["edge_ids"]

    for eid in list(dict.fromkeys(edge_ids))[: a.max_edges]:
        er = c.get(f"/api/v1/edges/{enc(eid)}", EDGE_RESP)
        if er:
            hits["edges"] += 1
            c.errors += check_edge(er, f"edge({eid})")

    # error shape
    c.get("/api/v1/nodes/ATLAS%3A__definitely_missing__", API_ERROR, expect=404)
    c.get("/api/v1/edges/E%3A__definitely_missing__", API_ERROR, expect=404)

    if a.with_ai and path_edges:
        st, body = c.request("POST", "/api/v1/explain", {"edge_ids": path_edges[:8], "audience": "family"})
        if st == 200:
            errs = validate(body, EXPLANATION)
            c.errors += [f"POST /explain: {e}" for e in errs]
            bad = [] if errs else [s["edge_id"] for s in body["steps"] if s["edge_id"] not in path_edges]
            if bad:
                c.errors.append(f"POST /explain cites edges not in the request: {bad}")
        else:
            c.errors.append(f"POST /explain: status {st} {body}")
        st, body = c.request("POST", "/api/v1/gap-search", {"disease_id": node_ids[0]})
        if st == 202 and isinstance(body, dict) and isinstance(body.get("job_id"), str):
            job = c.get(f"/api/v1/jobs/{enc(body['job_id'])}", JOB_STATUS)
            if job and job["job_id"] != body["job_id"]:
                c.errors.append("GET /jobs: job_id mismatch")
        else:
            c.errors.append(f"POST /gap-search: status {st}")

    # coverage: an OK that checked nothing is not an OK (broken search/clusters, node_names not loaded, ...)
    counts = (meta or {}).get("counts", {})
    if not a.allow_empty and meta:
        total_nodes = sum(counts["nodes"].values())
        if total_nodes and not seen_nodes:
            c.errors.append(f"discovery found 0 node ids but meta reports {total_nodes} nodes (search/clusters broken?)")
        if counts["edges"] and not hits["edges"]:
            c.errors.append(f"checked 0 edges but meta reports {counts['edges']} (neighborhoods/similar broken?)")
        if counts["clusters"] and not clusters:
            c.errors.append(f"/clusters returned [] but meta reports {counts['clusters']} clusters")

    print(f"{c.calls} requests, {len(seen_nodes)} node ids discovered, {len(set(edge_ids))} edges, "
          f"{c.skipped} skipped (404), served by {c.served_by}")
    print("checked: " + ", ".join(f"{k}={v}" for k, v in hits.items()))
    if c.errors:
        print(f"FAIL: {len(c.errors)} contract violation(s)")
        for e in c.errors[:200]:
            print("  -", e)
        return 1
    print("OK: responses match contract v1.0.0")
    return 0


if __name__ == "__main__":
    sys.exit(main())
