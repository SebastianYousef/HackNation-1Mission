#!/usr/bin/env node
/**
 * Validates contract/fixtures/** against contract/atlas.ts.
 *
 *   node contract/scripts/validate-fixtures.mjs            # types + integrity
 *   node contract/scripts/validate-fixtures.mjs --no-types # integrity only (offline)
 *
 * 1. TYPE CHECK: every fixture is inlined into a generated .ts file as
 *    `const f_N: <ResponseType> = <json>;` and compiled with `tsc --strict`
 *    (via `npx -p typescript`). Explicit annotation on an object literal makes
 *    tsc reject wrong enum strings, missing fields AND excess properties.
 * 2. INTEGRITY: every referenced edge id / node id has its own fixture file,
 *    embedded NodeBriefs/Edges equal the canonical files, paths connect,
 *    neighborhoods are closed, counts add up.
 * Exit code 0 = clean.
 */
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { execFileSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { isDeepStrictEqual } from "node:util";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const CONTRACT = path.resolve(HERE, "..");
const FIX = path.join(CONTRACT, "fixtures");
const safeId = (id) => id.replaceAll(":", "_");
const errors = [];
const err = (file, msg) => errors.push(`${file}: ${msg}`);

// ---------------------------------------------------------------------------
// Load
// ---------------------------------------------------------------------------
const TYPE_FOR = [
  [/^meta\.json$/, "MetaResponse"],
  [/^search\.json$/, "Record<string, SearchResponse>"],
  [/^nodes\/[^/]+\.json$/, "NodeResponse"],
  [/^neighborhood\/[^/]+\.json$/, "NeighborhoodResponse"],
  [/^edges\/[^/]+\.json$/, "EdgeResponse"],
  [/^similar\/[^/]+\.json$/, "SimilarResponse"],
  [/^paths\/[^/]+\.json$/, "PathsResponse"],
  [/^clusters\.json$/, "ClustersResponse"],
  [/^clusters\/[^/]+\.json$/, "ClusterResponse"],
  [/^action-view\/[^/]+\.json$/, "ActionView"],
  [/^mechanism-view\/[^/]+\.json$/, "MechanismView"],
  [/^explain\.json$/, "Explanation"],
  [/^outreach-draft\.json$/, "OutreachDraft"],
  [/^gap-search-job\.json$/, "JobStatus"],
  [/^error-[^/]+\.json$/, "ApiError"],
];
const fixtures = {}; // rel -> parsed
(function walk(dir) {
  for (const ent of fs.readdirSync(dir, { withFileTypes: true })) {
    const p = path.join(dir, ent.name);
    if (ent.isDirectory()) walk(p);
    else if (ent.name.endsWith(".json")) {
      const rel = path.relative(FIX, p).split(path.sep).join("/");
      try { fixtures[rel] = JSON.parse(fs.readFileSync(p, "utf8")); } catch (e) { err(rel, `invalid JSON: ${e.message}`); }
    }
  }
})(FIX);
const rels = Object.keys(fixtures).sort();
const typeOf = (rel) => TYPE_FOR.find(([re]) => re.test(rel))?.[1];
for (const rel of rels) if (!typeOf(rel)) err(rel, "no response type mapped for this file name");

const byDir = (d) => rels.filter((r) => r.startsWith(`${d}/`));
const nodesById = new Map(byDir("nodes").map((r) => [fixtures[r].node?.id, fixtures[r]]));
const edgesById = new Map(byDir("edges").map((r) => [fixtures[r].edge?.id, fixtures[r]]));

// ---------------------------------------------------------------------------
// Integrity
// ---------------------------------------------------------------------------
const NODE_TYPES = new Set(["disease", "gene", "variant", "phenotype", "mechanism", "intervention", "organization", "person", "publication", "trial", "grant", "asset"]);
const RANK = ["curated", "literature", "inferred", "hypothesis"];
const needEdge = (rel, id, where) => { if (!edgesById.has(id)) err(rel, `${where}: edge '${id}' has no edges/${safeId(id)}.json`); };
const needNode = (rel, id, where) => { if (!nodesById.has(id)) err(rel, `${where}: node '${id}' has no nodes/${safeId(id)}.json`); };
const connects = (eid, a, b) => { const e = edgesById.get(eid)?.edge; return !!e && ((e.src === a && e.dst === b) || (e.src === b && e.dst === a)); };

// file name == safe id
for (const r of byDir("nodes")) if (r !== `nodes/${safeId(fixtures[r].node.id)}.json`) err(r, `file name does not match node id ${fixtures[r].node.id}`);
for (const r of byDir("edges")) if (r !== `edges/${safeId(fixtures[r].edge.id)}.json`) err(r, `file name does not match edge id ${fixtures[r].edge.id}`);

// generic walk over every fixture
function walkObj(rel, v, where) {
  if (Array.isArray(v)) return v.forEach((x, i) => walkObj(rel, x, `${where}[${i}]`));
  if (!v || typeof v !== "object") return;
  for (const k of ["edge_id", "similarity_edge_id"]) if (typeof v[k] === "string") needEdge(rel, v[k], `${where}.${k}`);
  for (const k of ["edge_ids", "evidence_edge_ids"]) if (Array.isArray(v[k])) v[k].forEach((id) => needEdge(rel, id, `${where}.${k}`));
  if (Array.isArray(v.node_ids)) v.node_ids.forEach((id) => needNode(rel, id, `${where}.node_ids`));
  if (Array.isArray(v.disease_ids)) v.disease_ids.forEach((id) => needNode(rel, id, `${where}.disease_ids`));
  // NodeBrief-shaped object: must match the canonical node
  if (typeof v.id === "string" && NODE_TYPES.has(v.type) && "label" in v && "summary" in v && "subtype" in v) {
    const canon = nodesById.get(v.id)?.node;
    if (!canon) needNode(rel, v.id, where);
    else for (const k of ["type", "subtype", "label", "summary"]) if (canon[k] !== v[k]) err(rel, `${where}: ${k} of ${v.id} differs from nodes/ file`);
    if ("description" in v && canon && !isDeepStrictEqual(v, canon)) err(rel, `${where}: NodeFull ${v.id} differs from nodes/ file`);
  }
  // Edge-shaped object: must equal the canonical edge
  if (typeof v.id === "string" && "src" in v && "dst" in v && "support_count" in v) {
    const canon = edgesById.get(v.id)?.edge;
    if (!canon) needEdge(rel, v.id, where);
    else if (!isDeepStrictEqual(v, canon)) err(rel, `${where}: edge ${v.id} differs from edges/ file`);
  }
  // OrgCard: edge links org and disease
  if (v.for_disease && v.node && typeof v.edge_id === "string") {
    const e = edgesById.get(v.edge_id)?.edge;
    if (e && !(e.type === "organization_serves_disease" && e.src === v.node.id && e.dst === v.for_disease.id))
      err(rel, `${where}: OrgCard edge ${v.edge_id} is not ${v.node.id} serves ${v.for_disease.id}`);
  }
  for (const [k, x] of Object.entries(v)) walkObj(rel, x, `${where}.${k}`);
}
for (const rel of rels) walkObj(rel, fixtures[rel], "$");

// edges/
const evIds = new Set();
let evCount = 0;
for (const r of byDir("edges")) {
  const f = fixtures[r], e = f.edge;
  if (f.source?.id !== e.src || f.target?.id !== e.dst) err(r, "source/target do not match edge src/dst");
  needNode(r, e.src, "edge.src"); needNode(r, e.dst, "edge.dst");
  if (e.support_count !== f.supporting.length) err(r, `support_count ${e.support_count} != ${f.supporting.length} supporting rows`);
  if (e.contradict_count !== f.contradicting.length) err(r, `contradict_count ${e.contradict_count} != ${f.contradicting.length} contradicting rows`);
  for (const [list, stance] of [[f.supporting, "supports"], [f.contradicting, "contradicts"], [f.context, "context"]])
    for (const v of list) {
      if (v.stance !== stance) err(r, `evidence ${v.id} in wrong list (${v.stance})`);
      if (evIds.has(v.id)) err(r, `duplicate evidence id ${v.id}`);
      evIds.add(v.id); evCount++;
    }
  if (e.confidence < 0 || e.confidence > 1) err(r, "confidence out of 0..1");
  // honest statuses (root CLAUDE.md rule 2)
  const rows = [...f.supporting, ...f.contradicting, ...f.context];
  if (!rows.length) err(r, "edge has no evidence rows");
  if (e.status === "curated" && rows.some((v) => v.method.startsWith("llm:"))) err(r, "curated edge has llm:* evidence (LLM output is never curated)");
  if (e.status === "literature" && !f.supporting.some((v) => v.quote)) err(r, "literature edge has no supporting row with a quote");
}

// paths
function checkPath(rel, p, where) {
  const n = p.node_ids.length;
  if (n < 2) err(rel, `${where}: node_ids must have >= 2 entries`);
  if (new Set(p.node_ids).size !== n) err(rel, `${where}: node_ids repeat a node (must be a simple path)`);
  if (p.edge_ids.length !== n - 1) err(rel, `${where}: edge_ids length must be node_ids length - 1`);
  if (p.node_ids[0] !== p.from || p.node_ids[n - 1] !== p.to) err(rel, `${where}: from/to do not match node_ids ends`);
  if (!isDeepStrictEqual(p.nodes.map((x) => x.id), p.node_ids)) err(rel, `${where}: nodes order != node_ids`);
  if (!isDeepStrictEqual(p.edges.map((x) => x.id), p.edge_ids)) err(rel, `${where}: edges order != edge_ids`);
  p.edge_ids.forEach((eid, i) => { if (!connects(eid, p.node_ids[i], p.node_ids[i + 1])) err(rel, `${where}: ${eid} does not connect ${p.node_ids[i]} and ${p.node_ids[i + 1]}`); });
  // A path is never stronger than its weakest edge. It equals the edge minimum unless an honesty cap
  // (attrs.status_cap / attrs.confidence_cap, migration 0004) lowers it; then it equals the cap.
  // Same rule as check_contract.py check_path (and analytics/paths.py path_strength).
  if (!p.edges.length) return;
  const attrs = p.attrs ?? {};
  const edgeSt = p.edges.map((e) => e.status).sort((a, b) => RANK.indexOf(b) - RANK.indexOf(a))[0];
  const sCap = RANK.includes(attrs.status_cap) ? attrs.status_cap : null;
  const wantSt = sCap && RANK.indexOf(sCap) > RANK.indexOf(edgeSt) ? sCap : edgeSt;
  if (RANK.indexOf(p.weakest_status) < RANK.indexOf(edgeSt)) err(rel, `${where}: weakest_status is stronger than the weakest edge status (${edgeSt})`);
  else if (p.weakest_status !== wantSt) err(rel, `${where}: weakest_status should be ${wantSt} (${sCap ? "status_cap" : "weakest edge status"})`);
  // tolerance: the database stores confidence as float4 (exported snapshots)
  const edgeConf = Math.min(...p.edges.map((e) => e.confidence));
  const cCap = typeof attrs.confidence_cap === "number" ? attrs.confidence_cap : null;
  const wantConf = cCap !== null ? Math.min(edgeConf, cCap) : edgeConf;
  if (p.min_confidence > edgeConf + 1e-6) err(rel, `${where}: min_confidence is above the minimum edge confidence (${edgeConf})`);
  else if (Math.abs(p.min_confidence - wantConf) > 1e-6) err(rel, `${where}: min_confidence should be ${wantConf} (${cCap !== null ? "confidence_cap" : "minimum edge confidence"})`);
}
for (const r of byDir("paths")) fixtures[r].forEach((p, i) => checkPath(r, p, `$[${i}]`));
for (const r of byDir("action-view")) fixtures[r].connections.forEach((p, i) => checkPath(r, p, `$.connections[${i}]`));

// neighborhoods
for (const r of byDir("neighborhood")) {
  const f = fixtures[r], ids = new Set(f.nodes.map((n) => n.id));
  if (r !== `neighborhood/${safeId(f.center)}.json`) err(r, "file name does not match center");
  if (!ids.has(f.center)) err(r, "center not in nodes");
  for (const e of f.edges) if (!ids.has(e.src) || !ids.has(e.dst)) err(r, `edge ${e.id} endpoint missing from nodes`);
}

// similar
for (const r of byDir("similar")) {
  const fileId = [...nodesById.keys()].find((k) => r === `similar/${safeId(k)}.json`);
  if (!fileId) err(r, "file name matches no node");
  for (const s of fixtures[r]) {
    const e = edgesById.get(s.edge_id)?.edge;
    if (e && (e.type !== "disease_similar_to" || !connects(s.edge_id, fileId, s.disease.id))) err(r, `${s.edge_id} is not a disease_similar_to edge between ${fileId} and ${s.disease.id}`);
    if (e && (s.score !== e.score || s.confidence !== e.confidence || s.status !== e.status)) err(r, `${s.edge_id}: score/confidence/status differ from edge`);
  }
}

// action / mechanism views vs node flags
for (const [dir, flag, key] of [["action-view", "has_action_view", "disease"], ["mechanism-view", "has_mechanism_view", "mechanism"]]) {
  const have = new Set(byDir(dir).map((r) => fixtures[r][key].id));
  for (const r of byDir(dir)) if (r !== `${dir}/${safeId(fixtures[r][key].id)}.json`) err(r, `file name does not match ${key}.id`);
  for (const [id, f] of nodesById) if (f[flag] !== have.has(id)) err(`nodes/${safeId(id)}.json`, `${flag}=${f[flag]} but ${dir} file ${have.has(id) ? "exists" : "missing"}`);
}

// clusters
const clusterIds = new Set((fixtures["clusters.json"] ?? []).map((c) => c.id));
for (const c of fixtures["clusters.json"] ?? []) {
  const r = `clusters/${safeId(c.id)}.json`, f = fixtures[r];
  if (!f) { err("clusters.json", `missing ${r}`); continue; }
  if (!isDeepStrictEqual(f.cluster, c)) err(r, "cluster brief differs from clusters.json");
  if (f.members.length !== c.size) err(r, "size != members.length");
}
for (const [id, f] of nodesById) for (const c of f.clusters) if (!clusterIds.has(c.id)) err(`nodes/${safeId(id)}.json`, `unknown cluster ${c.id}`);
for (const r of byDir("mechanism-view")) {
  for (const c of fixtures[r].clusters) if (!clusterIds.has(c.cluster.id)) err(r, `unknown cluster ${c.cluster.id}`);
  for (const d of fixtures[r].diseases) if (d.cluster && !clusterIds.has(d.cluster.id)) err(r, `unknown cluster ${d.cluster.id}`);
}

// meta counts
const meta = fixtures["meta.json"];
if (meta) {
  if (meta.counts.edges !== edgesById.size) err("meta.json", `counts.edges ${meta.counts.edges} != ${edgesById.size} edge files`);
  if (meta.counts.evidence !== evCount) err("meta.json", `counts.evidence ${meta.counts.evidence} != ${evCount}`);
  if (meta.counts.clusters !== clusterIds.size) err("meta.json", "counts.clusters mismatch");
  const total = Object.values(meta.counts.nodes).reduce((a, b) => a + b, 0);
  if (total !== nodesById.size) err("meta.json", `counts.nodes total ${total} != ${nodesById.size} node files`);
}

// explain / outreach citations
for (const c of fixtures["outreach-draft.json"]?.citations ?? []) {
  const e = edgesById.get(c.edge_id);
  if (e && ![...e.supporting, ...e.contradicting, ...e.context].some((v) => v.source_ref === c.source_ref && v.url === c.url))
    err("outreach-draft.json", `citation [${c.n}] source_ref/url not found in evidence of ${c.edge_id}`);
}
for (const s of fixtures["explain.json"]?.steps ?? []) {
  const e = edgesById.get(s.edge_id)?.edge;
  if (e && (e.status !== s.status || e.confidence !== s.confidence)) err("explain.json", `step ${s.edge_id} status/confidence differs from edge`);
}

// ---------------------------------------------------------------------------
// Type check
// ---------------------------------------------------------------------------
let typeResult = "skipped (--no-types)";
if (!process.argv.includes("--no-types")) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "atlas-fixtures-"));
  const atlasImport = path.join(CONTRACT, "atlas").split(path.sep).join("/");
  const names = new Set(TYPE_FOR.map(([, t]) => t.replace(/^Record<string, (\w+)>$/, "$1")));
  let src = `import type { ${[...names].join(", ")} } from ${JSON.stringify(atlasImport)};\n\n`;
  const lineOf = [];
  rels.forEach((rel, i) => {
    const t = typeOf(rel); if (!t) return;
    lineOf.push([src.split("\n").length, rel]);
    src += `// ${rel}\nexport const f_${i}: ${t} = ${JSON.stringify(fixtures[rel], null, 1)};\n\n`;
  });
  const file = path.join(dir, "fixtures.check.ts");
  fs.writeFileSync(file, src);
  try {
    // shell on Windows: npx is npx.cmd there, which execFileSync cannot spawn directly
    execFileSync("npx", ["-y", "-p", "typescript@5", "tsc", "--noEmit", "--strict", "--target", "es2022", "--module", "esnext",
      "--moduleResolution", "bundler", "--skipLibCheck", "--allowImportingTsExtensions", file],
      { stdio: "pipe", encoding: "utf8", shell: process.platform === "win32" });
    typeResult = "ok";
    fs.rmSync(dir, { recursive: true, force: true });
  } catch (e) {
    const out = `${e.stdout ?? ""}${e.stderr ?? ""}`;
    const lines = out.split("\n").filter(Boolean);
    // fail closed: npx missing (ENOENT), killed, etc. must not pass the gate silently
    if (!lines.length) errors.push(`[tsc] type check did not run (${e.code ?? e.signal ?? "no output"}): ${e.message}. Install node/npx, or pass --no-types to skip it explicitly.`);
    for (const line of lines) {
      const m = line.match(/fixtures\.check\.ts\((\d+),/);
      const rel = m ? [...lineOf].reverse().find(([l]) => l <= +m[1])?.[1] : null;
      errors.push(`[tsc]${rel ? ` ${rel}:` : ""} ${line}`);
    }
    typeResult = `FAILED (generated file kept at ${file})`;
  }
}

console.log(`fixtures: ${rels.length} files (${nodesById.size} nodes, ${edgesById.size} edges, ${evCount} evidence rows)`);
console.log(`type check: ${typeResult}`);
if (errors.length) {
  console.error(`\n${errors.length} problem(s):\n  ${errors.join("\n  ")}`);
  // exitCode, not process.exit(): exit() can drop buffered output when stderr is a pipe
  process.exitCode = 1;
} else {
  console.log("integrity: ok\nALL FIXTURES VALID");
}
