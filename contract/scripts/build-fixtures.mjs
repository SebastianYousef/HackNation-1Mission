#!/usr/bin/env node
/**
 * Builds contract/fixtures/** from ONE in-memory mock graph so every file is
 * mutually consistent (same ids, same labels, counts that add up).
 *
 *   node contract/scripts/build-fixtures.mjs     # regenerate fixtures
 *   node contract/scripts/validate-fixtures.mjs  # type-check + integrity
 *
 * ALL DATA IS MOCK. Gene symbols, disease names and HPO-style labels are real
 * well-known names, but every identifier (MONDO:MOCK…, PMID:MOCK…, NCTMOCK…),
 * every person, quote, number and URL is a placeholder.
 */
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { createHash } from "node:crypto";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const OUT = path.resolve(HERE, "../fixtures");
const RETRIEVED = "2026-10-01T12:00:00Z";
const ex = (p) => `https://example.org/${p}`;
export const safeId = (id) => id.replaceAll(":", "_");

// ============================================================================
// Nodes
// ============================================================================
const nodes = new Map();      // id -> NodeFull
const extraNames = new Map(); // id -> [{name, kind}] (abbreviations; feeds mock search only)

function node(id, type, subtype, label, summary, o = {}) {
  if (nodes.has(id)) throw new Error(`dup node ${id}`);
  nodes.set(id, {
    id, type, subtype, label, summary,
    description: o.description ?? null,
    synonyms: o.synonyms ?? [],
    xrefs: o.xrefs ?? {},
    attrs: o.attrs ?? {},
    url: o.url === undefined ? ex(`atlas/${type}/${safeId(id)}`) : o.url,
  });
  if (o.abbr) extraNames.set(id, o.abbr.map((name) => ({ name, kind: "abbreviation" })));
  return id;
}
const brief = (id) => {
  const n = nodes.get(id);
  if (!n) throw new Error(`unknown node ${id}`);
  return { id: n.id, type: n.type, subtype: n.subtype, label: n.label, summary: n.summary };
};
const full = (id) => structuredClone(nodes.get(id));

// ---- diseases --------------------------------------------------------------
const D = {
  NCL: node("MONDO:MOCK0000", "disease", null, "Neuronal ceroid lipofuscinosis",
    "A group of inherited brain diseases (often called Batten disease) in which waste material builds up inside cells' recycling centres, the lysosomes.",
    { description: "[MOCK] Group of lysosomal storage disorders characterised by accumulation of autofluorescent ceroid lipopigment in neurons and other cells.",
      synonyms: ["Batten disease", "NCL"], abbr: ["NCL"],
      xrefs: { ORPHA: ["MOCK-ORPHA-0000"] },
      attrs: { inheritance: "mostly autosomal recessive", has_approved_treatment: true, approved_treatments: ["Cerliponase alfa (CLN2 disease only)"] } }),
  CLN5: node("MONDO:MOCK0005", "disease", null, "CLN5 disease",
    "A very rare inherited childhood brain disease that causes vision loss, seizures and loss of skills. There is no approved treatment yet.",
    { description: "[MOCK] Neuronal ceroid lipofuscinosis caused by biallelic variants in CLN5, typically with variant late-infantile onset.",
      synonyms: ["Neuronal ceroid lipofuscinosis type 5", "CLN5 Batten disease", "Variant late infantile NCL, Finnish type"],
      abbr: ["CLN5"], xrefs: { ORPHA: ["MOCK-ORPHA-0005"], OMIM: ["MOCK-OMIM-0005"] },
      attrs: { prevalence: "[MOCK] ultra-rare (<1 in 1,000,000)", onset: "late infantile to childhood", inheritance: "autosomal recessive",
               has_approved_treatment: false, approved_treatments: [] } }),
  CLN6: node("MONDO:MOCK0006", "disease", null, "CLN6 disease",
    "A rare inherited brain disease of childhood with seizures, vision loss and loss of movement and speech skills.",
    { description: "[MOCK] Neuronal ceroid lipofuscinosis caused by biallelic variants in CLN6, an endoplasmic-reticulum membrane protein.",
      synonyms: ["Neuronal ceroid lipofuscinosis type 6", "CLN6 Batten disease"], abbr: ["CLN6"],
      xrefs: { ORPHA: ["MOCK-ORPHA-0006"] },
      attrs: { prevalence: "[MOCK] ultra-rare", onset: "late infantile", inheritance: "autosomal recessive",
               has_approved_treatment: false, approved_treatments: [] } }),
  CLN3: node("MONDO:MOCK0003", "disease", null, "CLN3 disease",
    "The most common form of Batten disease. It usually starts at school age with vision loss, followed by seizures and loss of skills.",
    { description: "[MOCK] Juvenile neuronal ceroid lipofuscinosis caused by variants in CLN3, a lysosomal membrane protein.",
      synonyms: ["Juvenile Batten disease", "Juvenile neuronal ceroid lipofuscinosis", "Neuronal ceroid lipofuscinosis type 3"],
      abbr: ["CLN3", "JNCL"], xrefs: { ORPHA: ["MOCK-ORPHA-0003"] },
      attrs: { prevalence: "[MOCK] rare", onset: "juvenile (4–10 years)", inheritance: "autosomal recessive",
               has_approved_treatment: false, approved_treatments: [] } }),
  CLN2: node("MONDO:MOCK0002", "disease", null, "CLN2 disease",
    "A form of Batten disease that starts in toddlers with seizures and loss of skills. An enzyme replacement therapy is approved.",
    { description: "[MOCK] Late-infantile neuronal ceroid lipofuscinosis caused by deficiency of the soluble lysosomal enzyme tripeptidyl peptidase 1 (TPP1).",
      synonyms: ["Late infantile Batten disease", "TPP1 deficiency", "Neuronal ceroid lipofuscinosis type 2"],
      abbr: ["CLN2", "LINCL"], xrefs: { ORPHA: ["MOCK-ORPHA-0002"] },
      attrs: { prevalence: "[MOCK] rare", onset: "late infantile (2–4 years)", inheritance: "autosomal recessive",
               has_approved_treatment: true, approved_treatments: ["Cerliponase alfa"] } }),
  X: node("MONDO:MOCK0099", "disease", null, "MOCK ultra-rare NCL subtype X (fictional)",
    "A FICTIONAL, made-up ultra-rare Batten-like disease used to demo the 'no known group' case. Not a real diagnosis.",
    { description: "[MOCK] Fictional neuronal ceroid lipofuscinosis subtype reported in a handful of families; gene MOCKG1 (fictional).",
      synonyms: ["NCL subtype X (mock)"], abbr: ["CLN-X"], xrefs: {},
      attrs: { prevalence: "[MOCK] fewer than 20 reported families", onset: "childhood", inheritance: "autosomal recessive",
               has_approved_treatment: false, approved_treatments: [] } }),
};

// ---- genes -----------------------------------------------------------------
const gene = (id, sym, summary, description) =>
  node(id, "gene", "protein_coding", sym, summary, { description, synonyms: [], abbr: [], xrefs: { HGNC: [`MOCK-${sym}`] } });
const G = {
  CLN5: gene("HGNC:MOCK0005", "CLN5", "Makes a soluble protein that works inside the lysosome; its exact job is still being studied.",
    "[MOCK] CLN5 intracellular trafficking protein; soluble lysosomal glycoprotein of incompletely understood function."),
  CLN6: gene("HGNC:MOCK0006", "CLN6", "Makes a protein in the cell's endoplasmic reticulum membrane that helps deliver enzymes to lysosomes.",
    "[MOCK] CLN6 transmembrane ER protein."),
  CLN3: gene("HGNC:MOCK0003", "CLN3", "Makes a protein that sits in the lysosome's outer membrane.",
    "[MOCK] CLN3 lysosomal/endosomal transmembrane protein."),
  TPP1: gene("HGNC:MOCK0002", "TPP1", "Makes TPP1, a soluble lysosomal enzyme that breaks down small proteins. Missing in CLN2 disease.",
    "[MOCK] Tripeptidyl peptidase 1, a soluble lysosomal serine protease."),
  PPT1: gene("HGNC:MOCK0001", "PPT1", "Makes a soluble lysosomal enzyme; changes cause CLN1 disease.",
    "[MOCK] Palmitoyl-protein thioesterase 1, soluble lysosomal enzyme."),
  MFSD8: gene("HGNC:MOCK0007", "MFSD8", "Makes a lysosomal membrane transporter; changes cause CLN7 disease.",
    "[MOCK] Major facilitator superfamily domain containing 8, lysosomal membrane protein."),
  MOCKG1: gene("HGNC:MOCK0099", "MOCKG1", "A FICTIONAL gene used only in this mock dataset.",
    "[MOCK] Fictional gene for the demo subtype X."),
};

// ---- mechanisms ------------------------------------------------------------
const M = {
  LYS: node("ATLAS:mech-lysosomal-degradation", "mechanism", "biological_process", "Lysosomal degradation (ceroid lipofuscin storage)",
    "Cells break down worn-out parts inside lysosomes. In all NCLs this recycling fails and waste (lipofuscin) builds up, especially in nerve cells.",
    { description: "[MOCK] Lysosomal catabolism of proteins and lipids; impairment leads to accumulation of autofluorescent storage material.",
      synonyms: ["Lysosomal storage", "Lysosomal protein breakdown", "Autophagy-lysosome pathway"] }),
  SOL: node("ATLAS:mech-soluble-lysosomal-enzyme-deficiency", "mechanism", "variant_effect", "Soluble lysosomal enzyme deficiency",
    "A missing enzyme that normally floats free inside the lysosome. Because it is soluble, it can sometimes be replaced with a manufactured version (enzyme replacement).",
    { description: "[MOCK] Loss of function of a secreted/soluble lysosomal hydrolase amenable in principle to cross-correction.",
      synonyms: ["Soluble lysosomal protein loss", "Cross-correctable lysosomal deficiency"] }),
  MEM: node("ATLAS:mech-membrane-protein-dysfunction", "mechanism", "biological_process", "Lysosomal/ER membrane protein dysfunction",
    "A broken protein that is built into a membrane (of the lysosome or the endoplasmic reticulum). It cannot simply be replaced by an infusion.",
    { description: "[MOCK] Loss of function of an integral membrane protein of the lysosome or ER; not cross-correctable by enzyme replacement.",
      synonyms: ["Lysosomal membrane transport defect", "Membrane protein NCL"] }),
};

// ---- phenotypes ------------------------------------------------------------
const pheno = (id, label, summary, ic, synonyms = []) =>
  node(id, "phenotype", null, label, summary, { synonyms, attrs: { ic } });
const P = {
  SEIZ: pheno("HP:MOCK0001", "Seizure", "Sudden bursts of abnormal electrical activity in the brain (fits).", 3.1, ["Epileptic seizure", "Fits"]),
  VIS: pheno("HP:MOCK0002", "Visual loss", "Gradual loss of eyesight.", 4.2, ["Vision loss", "Progressive visual loss"]),
  ATAX: pheno("HP:MOCK0003", "Ataxia", "Unsteady, clumsy movement and walking.", 3.5, ["Unsteady gait"]),
  REGR: pheno("HP:MOCK0004", "Developmental regression", "Losing skills (speech, walking, learning) that a child had already gained.", 4.0, ["Loss of skills", "Cognitive decline"]),
  MYOC: pheno("HP:MOCK0005", "Myoclonus", "Brief, jerky, involuntary muscle twitches.", 4.6, ["Myoclonic jerks"]),
  ATRO: pheno("HP:MOCK0006", "Cerebral atrophy", "Shrinking of brain tissue seen on a brain scan.", 4.4, ["Brain atrophy"]),
};

// ---- intervention ----------------------------------------------------------
const CERL = node("DRUG:MOCK0001", "intervention", "enzyme_replacement", "Cerliponase alfa",
  "A manufactured TPP1 enzyme given into the brain fluid. It is approved for CLN2 disease only.",
  { description: "[MOCK] Recombinant human tripeptidyl peptidase 1 administered intracerebroventricularly.",
    synonyms: ["rhTPP1", "Brineura"], abbr: [] });

// ---- organizations ---------------------------------------------------------
const org = (id, subtype, label, summary, attrs, abbr = []) =>
  node(id, "organization", subtype, label, summary, { synonyms: abbr, abbr, attrs, url: attrs.website ?? null });
const O = {
  CLN5FAM: org("ORG:mock-cln5-families", "patient_group", "CLN5 Families Network (mock)",
    "A (fictional) parent-led group for families affected by CLN5 disease.",
    { website: ex("cln5-families"), country: "SE", contact_url: ex("cln5-families/contact"), has_registry: false, email_public: "hello@example.org" }),
  BDSRA: org("ORG:mock-bdsra", "patient_group", "Batten Disease Support, Research and Advocacy Association (BDSRA)",
    "A patient advocacy organization supporting families affected by all forms of Batten disease. [MOCK record: details are placeholders]",
    { website: ex("bdsra"), country: "US", contact_url: ex("bdsra/contact"), has_registry: null, email_public: null }, ["BDSRA"]),
  CLN6ALL: org("ORG:mock-cln6-alliance", "patient_group", "CLN6 Parents Alliance (mock)",
    "A (fictional) parent group for CLN6 disease that supports a natural history study.",
    { website: ex("cln6-alliance"), country: "GB", contact_url: ex("cln6-alliance/contact"), has_registry: false, email_public: null }),
  CLN2CON: org("ORG:mock-cln2-connection", "patient_group", "CLN2 Family Connection (mock)",
    "A (fictional) support network for families living with CLN2 disease, including families on enzyme replacement therapy.",
    { website: ex("cln2-connection"), country: "DE", contact_url: ex("cln2-connection/contact"), has_registry: false, email_public: null }),
  CONS: org("ORG:mock-ncl-registry-consortium", "academic", "International NCL Registry Consortium (mock)",
    "A (fictional) academic consortium that runs a shared patient registry for several NCL subtypes.",
    { website: ex("ncl-registry"), country: "NL", contact_url: ex("ncl-registry/contact"), has_registry: true, email_public: "registry@example.org" }),
  HOSP: org("ORG:mock-example-university-hospital", "hospital", "Example University Hospital (mock)",
    "A (fictional) hospital with a paediatric neurology team that studies NCL diseases.",
    { website: ex("example-hospital"), country: "SE", contact_url: ex("example-hospital/ncl-team"), has_registry: false, email_public: null }),
  FUND: org("ORG:mock-ncl-research-fund", "funder", "NCL Research Fund (mock)",
    "A (fictional) charity that funds research on Batten disease.",
    { website: ex("ncl-fund"), country: "US", contact_url: ex("ncl-fund/apply"), has_registry: false, email_public: null }),
};

// ---- persons ---------------------------------------------------------------
const person = (id, subtype, label, summary, attrs) =>
  node(id, "person", subtype, label, summary, { attrs, url: attrs.contact_url });
const H = {
  A: person("PERSON:mock-researcher-a", "researcher", "Dr. A. Example",
    "(Fictional) neuroscientist studying lysosomal storage in CLN5 and CLN3 disease.",
    { affiliation: "Example University Hospital (mock)", orcid: "MOCK-ORCID-0001", roles: ["researcher", "clinician"], contact_url: ex("people/a-example") }),
  B: person("PERSON:mock-researcher-b", "researcher", "Dr. B. Placeholder",
    "(Fictional) cell biologist working on lysosomal membrane proteins, especially CLN3.",
    { affiliation: "International NCL Registry Consortium (mock)", orcid: "MOCK-ORCID-0002", roles: ["researcher"], contact_url: ex("people/b-placeholder") }),
  C: person("PERSON:mock-clinician-c", "clinician", "Dr. C. Sample",
    "(Fictional) child neurologist who has run enzyme replacement trials in CLN2 disease.",
    { affiliation: "Example University Hospital (mock)", orcid: "MOCK-ORCID-0003", roles: ["clinician"], contact_url: ex("people/c-sample") }),
};

// ---- assets ----------------------------------------------------------------
const A = {
  REG: node("ASSET:mock-ncl-registry", "asset", "registry", "International NCL Patient Registry (mock)",
    "A (fictional) shared registry where families with several NCL subtypes record their child's health over time.",
    { attrs: { asset_kind: "registry", access: "on_request", owner_id: O.CONS }, url: ex("ncl-registry/registry") }),
  NHS: node("ASSET:mock-cln6-natural-history", "asset", "natural_history_study", "CLN6 Natural History Study (mock)",
    "A (fictional) study that follows children with CLN6 disease over years to learn how the disease progresses.",
    { attrs: { asset_kind: "natural_history_study", access: "on_request", owner_id: O.HOSP }, url: ex("example-hospital/cln6-nhs") }),
  SHEEP: node("ASSET:mock-cln5-sheep-model", "asset", "animal_model", "CLN5 sheep model (mock entry)",
    "A large-animal model with CLN5 changes that researchers use to test treatments. [MOCK record]",
    { attrs: { asset_kind: "animal_model", access: "restricted", owner_id: O.HOSP }, url: ex("example-hospital/cln5-sheep") }),
};

// ---- trials, grant, publications ------------------------------------------
const T = {
  T1: node("NCTMOCK0001", "trial", null, "[MOCK] Intraventricular cerliponase alfa in children with CLN2 disease",
    "A (mock) completed trial of enzyme replacement therapy in CLN2 disease.",
    { attrs: { nct_id: "NCTMOCK0001", phase: "Phase 2", overall_status: "COMPLETED", start_date: "2013-09-01", enrollment: 24 }, url: ex("trials/NCTMOCK0001") }),
  T2: node("NCTMOCK0002", "trial", null, "[MOCK] Gene transfer study in CLN6 disease",
    "A (mock) early-stage gene therapy trial for children with CLN6 disease.",
    { attrs: { nct_id: "NCTMOCK0002", phase: "Phase 1/2", overall_status: "RECRUITING", start_date: "2025-03-01", enrollment: 12 }, url: ex("trials/NCTMOCK0002") }),
};
const GR = node("GRANT:MOCK0001", "grant", null, "[MOCK] Lysosomal dysfunction across CLN subtypes",
  "A (mock) research grant comparing how lysosomes fail in different forms of Batten disease.",
  { attrs: { project_num: "MOCK-R01-0001", fiscal_year: 2026, amount_usd: 450000, agency: "NCL Research Fund (mock)" }, url: ex("grants/MOCK0001") });
const PUB = {
  P1: node("PMID:MOCK0001", "publication", null, "[MOCK] Shared lysosomal storage features in CLN5 and CLN6 disease",
    "A (mock) paper comparing storage material and symptoms in CLN5 and CLN6 disease.",
    { attrs: { pmid: "MOCK0001", year: 2024, journal: "Journal of Mock Neurology", title: "[MOCK] Shared lysosomal storage features in CLN5 and CLN6 disease" }, url: ex("pubmed/MOCK0001") }),
  P2: node("PMID:MOCK0002", "publication", null, "[MOCK] CLN3 is a lysosomal membrane protein: implications for enzyme replacement",
    "A (mock) paper explaining why therapies that work for soluble-enzyme NCLs may not work for CLN3.",
    { attrs: { pmid: "MOCK0002", year: 2025, journal: "Mock Lysosome Reports", title: "[MOCK] CLN3 is a lysosomal membrane protein: implications for enzyme replacement" }, url: ex("pubmed/MOCK0002") }),
};

// ============================================================================
// Edges + evidence
// ============================================================================
const edges = new Map();    // alias -> Edge (alias = readable id used in this script)
const realId = new Map();   // alias -> "E:" + sha1(type|src|dst)[:16]  (repo convention, see CLAUDE.md)
const evidence = new Map(); // edge id -> Evidence[]
let evN = 0;

const LABEL = {
  gene_associated_with_disease: "causes", gene_in_mechanism: "acts in", disease_involves_mechanism: "involves",
  disease_has_phenotype: "has symptom", disease_subtype_of: "is a form of", disease_similar_to: "is similar to",
  intervention_targets_mechanism: "targets", intervention_treats_disease: "treats", trial_studies_disease: "studies",
  trial_tests_intervention: "tests", publication_about: "is about", person_authored: "authored", person_studies: "studies",
  person_affiliated_with: "works at", grant_funds_person: "funds", grant_studies: "studies", organization_funds_grant: "funds",
  organization_serves_disease: "serves families with", organization_maintains_asset: "maintains",
  asset_covers_disease: "covers", asset_targets_gene: "models",
};

function ev(stance, source_type, source_name, source_ref, url, quote, method, published_at = null) {
  return { id: `ev-mock-${String(++evN).padStart(4, "0")}`, stance, source_type, source_name, source_ref, url, quote, method, published_at, retrieved_at: RETRIEVED };
}
/** default supporting evidence row, chosen from the edge status / type */
function defaultEvidence(type, status, src, dst, srcName) {
  const what = `${nodes.get(src).label} ${LABEL[type]} ${nodes.get(dst).label}`;
  if (status === "inferred")
    return ev("supports", "computed", "Atlas analytics", null, null, null, "algorithm:mock-similarity-v1");
  if (status === "hypothesis")
    return ev("supports", "computed", "Atlas analytics", null, null,
      null, "algorithm:mock-hypothesis-generator");
  switch (srcName) {
    case "PubMed": // LLM-extracted text is never curated (CLAUDE.md rule 2): curated PubMed rows are bibliographic facts
      return status === "curated"
        ? ev("supports", "publication", "PubMed", "PMID:MOCK0001", ex("pubmed/MOCK0001"), null, "curated", "2024-05-01")
        : ev("supports", "publication", "PubMed", "PMID:MOCK0001", ex("pubmed/MOCK0001"), `[MOCK] Evidence that ${what}.`, "llm:mock-extractor", "2024-05-01");
    case "ClinicalTrials.gov": {
      const t = [src, dst].find((x) => nodes.get(x).type === "trial");
      return ev("supports", "trial_registry", "ClinicalTrials.gov", t, ex(`trials/${t}`), null, "curated", "2025-01-15");
    }
    case "NCL Research Fund (mock)":
      return ev("supports", "grant_database", "NCL Research Fund (mock)", "MOCK-R01-0001", ex("grants/MOCK0001"), null, "curated", "2026-01-10");
    case "Organization website":
      return ev("supports", "patient_org_site", "Organization website", null, nodes.get(src).url ?? ex("org"),
        `[MOCK] "We support families affected by ${nodes.get(dst).label}."`, "scrape:brightdata");
    default:
      return ev("supports", "database", srcName, null, ex(`db/${srcName.toLowerCase()}`), null, "curated");
  }
}
const DEFAULT_SOURCE = {
  gene_associated_with_disease: "Orphanet", gene_in_mechanism: "GO", disease_involves_mechanism: "PubMed",
  disease_has_phenotype: "HPO", disease_subtype_of: "MONDO", intervention_targets_mechanism: "DrugBank",
  intervention_treats_disease: "FDA labels", trial_studies_disease: "ClinicalTrials.gov", trial_tests_intervention: "ClinicalTrials.gov",
  publication_about: "PubMed", person_authored: "PubMed", person_studies: "PubMed", person_affiliated_with: "ORCID",
  grant_funds_person: "NCL Research Fund (mock)", grant_studies: "NCL Research Fund (mock)", organization_funds_grant: "NCL Research Fund (mock)",
  organization_serves_disease: "Organization website", organization_maintains_asset: "Organization website",
  asset_covers_disease: "Organization website", asset_targets_gene: "PubMed",
};

function edge(id, src, dst, type, status, confidence, o = {}) {
  if (edges.has(id)) throw new Error(`dup edge ${id}`);
  if (!nodes.has(src) || !nodes.has(dst)) throw new Error(`edge ${id}: missing endpoint`);
  const rows = o.evidence ?? [defaultEvidence(type, status, src, dst, o.source ?? DEFAULT_SOURCE[type])];
  if (o.extra) rows.push(...o.extra);
  evidence.set(id, rows);
  realId.set(id, "E:" + createHash("sha1").update(`${type}|${src}|${dst}`).digest("hex").slice(0, 16));
  edges.set(id, {
    id, src, dst, type, label: o.label ?? LABEL[type], status, confidence, score: o.score ?? null,
    support_count: rows.filter((r) => r.stance === "supports").length,
    contradict_count: rows.filter((r) => r.stance === "contradicts").length,
    sources: [...new Set(rows.map((r) => r.source_name))],
    attrs: o.attrs ?? {},
  });
  return id;
}
const E = (id) => { if (!edges.has(id)) throw new Error(`unknown edge ${id}`); return structuredClone(edges.get(id)); };

// ---- gene → disease, subtype -----------------------------------------------
edge("e-gd-cln5", G.CLN5, D.CLN5, "gene_associated_with_disease", "curated", 0.98);
edge("e-gd-cln6", G.CLN6, D.CLN6, "gene_associated_with_disease", "curated", 0.98);
edge("e-gd-cln3", G.CLN3, D.CLN3, "gene_associated_with_disease", "curated", 0.98);
edge("e-gd-tpp1", G.TPP1, D.CLN2, "gene_associated_with_disease", "curated", 0.99);
edge("e-gd-mockg1", G.MOCKG1, D.X, "gene_associated_with_disease", "literature", 0.6,
  { evidence: [ev("supports", "publication", "PubMed", "PMID:MOCK0003", ex("pubmed/MOCK0003"),
    "[MOCK] Homozygous MOCKG1 variants were found in three unrelated families with an NCL-like illness.", "llm:mock-extractor", "2025-11-02")] });
for (const [k, d] of Object.entries({ cln5: D.CLN5, cln6: D.CLN6, cln3: D.CLN3, cln2: D.CLN2, x: D.X }))
  edge(`e-sub-${k}`, d, D.NCL, "disease_subtype_of", k === "x" ? "literature" : "curated", k === "x" ? 0.55 : 0.99,
    k === "x" ? { source: "PubMed" } : {});

// ---- disease → mechanism -----------------------------------------------------
edge("e-dm-cln5-lys", D.CLN5, M.LYS, "disease_involves_mechanism", "literature", 0.85,
  { evidence: [ev("supports", "publication", "PubMed", "PMID:MOCK0001", ex("pubmed/MOCK0001"),
    "[MOCK] CLN5-deficient cells accumulate autofluorescent storage material and show impaired lysosomal degradation.", "llm:mock-extractor", "2024-05-01")] });
edge("e-dm-cln6-lys", D.CLN6, M.LYS, "disease_involves_mechanism", "literature", 0.84,
  { evidence: [ev("supports", "publication", "PubMed", "PMID:MOCK0001", ex("pubmed/MOCK0001"),
    "[MOCK] As in CLN5 disease, CLN6-deficient neurons show lysosomal storage of subunit c of mitochondrial ATP synthase.", "llm:mock-extractor", "2024-05-01")] });
edge("e-dm-cln3-lys", D.CLN3, M.LYS, "disease_involves_mechanism", "literature", 0.82,
  { evidence: [ev("supports", "publication", "PubMed", "PMID:MOCK0002", ex("pubmed/MOCK0002"),
    "[MOCK] Loss of CLN3 disrupts lysosomal function and leads to storage material accumulation.", "llm:mock-extractor", "2025-02-10")] });
edge("e-dm-cln2-lys", D.CLN2, M.LYS, "disease_involves_mechanism", "curated", 0.95, { source: "Reactome" });
edge("e-dm-x-lys", D.X, M.LYS, "disease_involves_mechanism", "inferred", 0.4,
  { evidence: [ev("supports", "computed", "Atlas analytics", null, null, null, "algorithm:mock-phenotype-to-mechanism")] });
edge("e-dm-cln2-sol", D.CLN2, M.SOL, "disease_involves_mechanism", "curated", 0.97, { source: "Reactome" });
edge("e-dm-cln5-sol", D.CLN5, M.SOL, "disease_involves_mechanism", "hypothesis", 0.35, {
  evidence: [
    ev("supports", "computed", "Atlas analytics", null, null, null, "algorithm:mock-hypothesis-generator"),
    ev("context", "publication", "PubMed", "PMID:MOCK0001", ex("pubmed/MOCK0001"),
      "[MOCK] The CLN5 protein is soluble and found in the lysosomal lumen.", "llm:mock-extractor", "2024-05-01"),
    ev("contradicts", "publication", "PubMed", "PMID:MOCK0004", ex("pubmed/MOCK0004"),
      "[MOCK] Unlike TPP1, no robust enzymatic activity has been established for CLN5, so its replacement cannot be assumed to restore function.", "llm:mock-extractor", "2025-08-20"),
  ],
  attrs: { note: "Atlas hypothesis: CLN5 is soluble like TPP1, so cross-correction might apply. Untested." },
});
edge("e-dm-cln3-mem", D.CLN3, M.MEM, "disease_involves_mechanism", "curated", 0.9, { source: "UniProt" });
edge("e-dm-cln6-mem", D.CLN6, M.MEM, "disease_involves_mechanism", "curated", 0.88, { source: "UniProt" });

// ---- gene → mechanism ------------------------------------------------------
for (const [k, g] of Object.entries({ cln5: G.CLN5, cln6: G.CLN6, cln3: G.CLN3, tpp1: G.TPP1, ppt1: G.PPT1, mfsd8: G.MFSD8 }))
  edge(`e-gm-${k}-lys`, g, M.LYS, "gene_in_mechanism", "curated", 0.9);
edge("e-gm-tpp1-sol", G.TPP1, M.SOL, "gene_in_mechanism", "curated", 0.95);
edge("e-gm-ppt1-sol", G.PPT1, M.SOL, "gene_in_mechanism", "curated", 0.93);
edge("e-gm-cln3-mem", G.CLN3, M.MEM, "gene_in_mechanism", "curated", 0.92);
edge("e-gm-cln6-mem", G.CLN6, M.MEM, "gene_in_mechanism", "curated", 0.9);
edge("e-gm-mfsd8-mem", G.MFSD8, M.MEM, "gene_in_mechanism", "curated", 0.9);

// ---- disease → phenotype ---------------------------------------------------
const PH = {
  cln5: { d: D.CLN5, p: { seiz: ["very frequent", "childhood"], vis: ["very frequent", "childhood"], atax: ["frequent", null], regr: ["very frequent", null], myoc: ["frequent", null], atro: ["frequent", null] } },
  cln6: { d: D.CLN6, p: { seiz: ["very frequent", "late infantile"], vis: ["very frequent", null], atax: ["frequent", null], regr: ["very frequent", null], myoc: ["frequent", null] } },
  cln3: { d: D.CLN3, p: { vis: ["very frequent", "juvenile"], seiz: ["frequent", "juvenile"], regr: ["very frequent", null] } },
  cln2: { d: D.CLN2, p: { seiz: ["very frequent", "late infantile"], atax: ["very frequent", null], regr: ["very frequent", null], myoc: ["frequent", null], vis: ["frequent", null] } },
  x: { d: D.X, p: { seiz: ["frequent", null], vis: ["frequent", null] } },
};
const PKEY = { seiz: P.SEIZ, vis: P.VIS, atax: P.ATAX, regr: P.REGR, myoc: P.MYOC, atro: P.ATRO };
for (const [dk, { d, p }] of Object.entries(PH))
  for (const [pk, [frequency, onset]] of Object.entries(p))
    edge(`e-dp-${dk}-${pk}`, d, PKEY[pk], "disease_has_phenotype", dk === "x" ? "literature" : "curated", dk === "x" ? 0.6 : 0.95,
      { attrs: onset ? { frequency, onset } : { frequency }, ...(dk === "x" ? { source: "PubMed" } : {}) });

// ---- similarity (inferred) -------------------------------------------------
const CAUTION_CLN2 = "CLN2 disease has an approved enzyme replacement therapy (cerliponase alfa) because TPP1 is a soluble lysosomal enzyme that can be supplied from outside the cell. That success does not automatically transfer to CLN5 disease: whether CLN5 can be replaced the same way is unproven.";
const CAUTION_CLN3 = "CLN3 is a lysosomal membrane protein, not a soluble enzyme. Similar symptoms do not mean similar treatments: enzyme-replacement-style approaches are not expected to work for CLN3.";
const simEv = () => ev("supports", "computed", "Atlas analytics", null, null, null, "algorithm:mock-similarity-v1");
edge("e-sim-cln5-cln6", D.CLN5, D.CLN6, "disease_similar_to", "inferred", 0.8,
  { score: 0.86, attrs: { components: { phenotype: 0.92, mechanism: 0.8, gene: 0 } } });
edge("e-sim-cln5-cln2", D.CLN5, D.CLN2, "disease_similar_to", "inferred", 0.7, {
  score: 0.74, attrs: { components: { phenotype: 0.85, mechanism: 0.6, gene: 0 }, caution: CAUTION_CLN2 },
  evidence: [simEv(), ev("context", "database", "FDA labels", "MOCK-LABEL-0001", ex("labels/cerliponase"),
    "[MOCK] Indicated for children with CLN2 disease (TPP1 deficiency).", "curated", "2017-04-27")],
});
edge("e-sim-cln5-cln3", D.CLN5, D.CLN3, "disease_similar_to", "inferred", 0.65, {
  score: 0.71, attrs: { components: { phenotype: 0.78, mechanism: 0.55, gene: 0 }, caution: CAUTION_CLN3 },
  evidence: [simEv(), ev("contradicts", "publication", "PubMed", "PMID:MOCK0002", ex("pubmed/MOCK0002"),
    "[MOCK] Juvenile-onset CLN3 disease follows a distinct course, beginning with isolated vision loss years before seizures.", "llm:mock-extractor", "2025-02-10")],
});
edge("e-sim-x-cln5", D.X, D.CLN5, "disease_similar_to", "inferred", 0.45,
  { score: 0.52, attrs: { components: { phenotype: 0.6, mechanism: 0.4, gene: 0 },
    caution: "Subtype X is only known from a few families; similarity is based on two symptoms and an inferred mechanism." } });

// ---- intervention / trials -------------------------------------------------
edge("e-it-cerl-cln2", CERL, D.CLN2, "intervention_treats_disease", "curated", 0.99, { attrs: { approval: "approved" } });
edge("e-itm-cerl-sol", CERL, M.SOL, "intervention_targets_mechanism", "curated", 0.95);
edge("e-itm-cerl-lys", CERL, M.LYS, "intervention_targets_mechanism", "curated", 0.9);
edge("e-ts-t1-cln2", T.T1, D.CLN2, "trial_studies_disease", "curated", 0.99);
edge("e-tt-t1-cerl", T.T1, CERL, "trial_tests_intervention", "curated", 0.99);
edge("e-ts-t2-cln6", T.T2, D.CLN6, "trial_studies_disease", "curated", 0.99);

// ---- publications / people / grant ----------------------------------------
// mirrors ingest/pubmed.py: publication_about = literature (quote = title), person_authored = curated byline
const pubEv = (pub, method, quote) => { const n = nodes.get(pub); return ev("supports", "publication", "PubMed", pub, n.url, quote, method, `${n.attrs.year}-01-01`); }; // year-only, as loaded into the date column
const aboutEv = (pub) => ({ evidence: [pubEv(pub, "algorithm:pubmed_query", nodes.get(pub).attrs.title)] });
const authoredEv = (pub) => ({ evidence: [pubEv(pub, "curated", null)] });
edge("e-pa-p1-cln5", PUB.P1, D.CLN5, "publication_about", "literature", 0.5, aboutEv(PUB.P1));
edge("e-pa-p1-cln6", PUB.P1, D.CLN6, "publication_about", "literature", 0.5, aboutEv(PUB.P1));
edge("e-pa-p2-cln3", PUB.P2, D.CLN3, "publication_about", "literature", 0.5, aboutEv(PUB.P2));
edge("e-pa-p2-mem", PUB.P2, M.MEM, "publication_about", "literature", 0.5, aboutEv(PUB.P2));
edge("e-pau-a-p1", H.A, PUB.P1, "person_authored", "curated", 0.97, authoredEv(PUB.P1));
edge("e-pau-b-p2", H.B, PUB.P2, "person_authored", "curated", 0.97, authoredEv(PUB.P2));
edge("e-ps-a-cln5", H.A, D.CLN5, "person_studies", "literature", 0.85);
edge("e-ps-a-cln3", H.A, D.CLN3, "person_studies", "literature", 0.75);
edge("e-ps-b-cln3", H.B, D.CLN3, "person_studies", "literature", 0.85);
edge("e-ps-b-lys", H.B, M.LYS, "person_studies", "literature", 0.8);
edge("e-ps-c-cln2", H.C, D.CLN2, "person_studies", "literature", 0.85);
edge("e-paf-a-hosp", H.A, O.HOSP, "person_affiliated_with", "curated", 0.95);
edge("e-paf-b-cons", H.B, O.CONS, "person_affiliated_with", "curated", 0.95);
edge("e-paf-c-hosp", H.C, O.HOSP, "person_affiliated_with", "curated", 0.95);
edge("e-gf-gr-a", GR, H.A, "grant_funds_person", "curated", 0.98);
edge("e-gs-gr-lys", GR, M.LYS, "grant_studies", "curated", 0.9);
edge("e-ofg-fund-gr", O.FUND, GR, "organization_funds_grant", "curated", 0.98);

// ---- organizations & assets ------------------------------------------------
edge("e-os-cln5fam-cln5", O.CLN5FAM, D.CLN5, "organization_serves_disease", "literature", 0.95);
edge("e-os-bdsra-cln5", O.BDSRA, D.CLN5, "organization_serves_disease", "literature", 0.8);
edge("e-os-bdsra-cln6", O.BDSRA, D.CLN6, "organization_serves_disease", "literature", 0.8);
edge("e-os-bdsra-cln3", O.BDSRA, D.CLN3, "organization_serves_disease", "literature", 0.85);
edge("e-os-bdsra-cln2", O.BDSRA, D.CLN2, "organization_serves_disease", "literature", 0.8);
edge("e-os-cln6all-cln6", O.CLN6ALL, D.CLN6, "organization_serves_disease", "literature", 0.95);
edge("e-os-cln2con-cln2", O.CLN2CON, D.CLN2, "organization_serves_disease", "literature", 0.95);
edge("e-om-cons-reg", O.CONS, A.REG, "organization_maintains_asset", "literature", 0.95);
edge("e-om-hosp-nhs", O.HOSP, A.NHS, "organization_maintains_asset", "literature", 0.95);
edge("e-om-hosp-sheep", O.HOSP, A.SHEEP, "organization_maintains_asset", "literature", 0.8);
edge("e-ac-reg-cln2", A.REG, D.CLN2, "asset_covers_disease", "literature", 0.9);
edge("e-ac-reg-cln3", A.REG, D.CLN3, "asset_covers_disease", "literature", 0.9);
edge("e-ac-reg-cln6", A.REG, D.CLN6, "asset_covers_disease", "literature", 0.9);
edge("e-ac-nhs-cln6", A.NHS, D.CLN6, "asset_covers_disease", "literature", 0.92);
edge("e-ac-sheep-cln5", A.SHEEP, D.CLN5, "asset_covers_disease", "literature", 0.9);
edge("e-at-sheep-cln5g", A.SHEEP, G.CLN5, "asset_targets_gene", "literature", 0.9);

// ============================================================================
// Clusters
// ============================================================================
const clusters = [
  { id: "cluster:mock-ncl-core", label: "Childhood NCLs: vision loss + seizures + regression",
    summary: "Batten-type diseases that share the core symptom triad of vision loss, seizures and loss of skills.",
    method: "algorithm:mock-leiden-phenotype", members: [[D.CLN5, 0.94], [D.CLN6, 0.92], [D.CLN2, 0.88], [D.CLN3, 0.8], [D.X, 0.5]],
    attrs: { cohesion: 0.81, top_phenotypes: [P.VIS, P.SEIZ, P.REGR], top_mechanisms: [M.LYS] } },
  { id: "cluster:mock-ncl-soluble", label: "Soluble lysosomal protein NCLs",
    summary: "NCLs caused by loss of a soluble lysosomal protein — the group where enzyme replacement is plausible in principle.",
    method: "algorithm:mock-leiden-mechanism", members: [[D.CLN2, 0.95], [D.CLN5, 0.55]],
    attrs: { cohesion: 0.62, top_phenotypes: [P.SEIZ, P.MYOC], top_mechanisms: [M.SOL, M.LYS] } },
  { id: "cluster:mock-ncl-membrane", label: "Membrane protein NCLs",
    summary: "NCLs caused by a broken membrane protein (lysosome or ER); infused enzymes are not expected to help.",
    method: "algorithm:mock-leiden-mechanism", members: [[D.CLN3, 0.93], [D.CLN6, 0.9]],
    attrs: { cohesion: 0.7, top_phenotypes: [P.VIS, P.SEIZ], top_mechanisms: [M.MEM, M.LYS] } },
];
const clusterBrief = (c) => ({ id: c.id, label: c.label, summary: c.summary, method: c.method, size: c.members.length, attrs: structuredClone(c.attrs) });

// ============================================================================
// Derived helpers
// ============================================================================
const allEdges = () => [...edges.values()];
const touching = (id) => allEdges().filter((e) => e.src === id || e.dst === id);
const other = (e, id) => (e.src === id ? e.dst : e.src);
const find = (src, dst, type) => {
  const e = allEdges().find((x) => x.src === src && x.dst === dst && x.type === type);
  if (!e) throw new Error(`no ${type} edge ${src} -> ${dst}`);
  return e.id;
};

function orgCard(orgId, diseaseId) {
  const n = nodes.get(orgId);
  return {
    node: brief(orgId), for_disease: brief(diseaseId), edge_id: find(orgId, diseaseId, "organization_serves_disease"),
    website: n.attrs.website ?? null, contact_url: n.attrs.contact_url ?? null, country: n.attrs.country ?? null,
    has_registry: n.attrs.has_registry ?? null,
  };
}
const groupsFor = (diseaseId) =>
  allEdges().filter((e) => e.type === "organization_serves_disease" && e.dst === diseaseId)
    .sort((a, b) => b.confidence - a.confidence).map((e) => orgCard(e.src, diseaseId));

function assetCard(assetId, reusability, what_differs, needs_review) {
  const n = nodes.get(assetId);
  const cov = allEdges().filter((e) => e.src === assetId && e.type === "asset_covers_disease");
  const own = allEdges().filter((e) => e.dst === assetId && e.type === "organization_maintains_asset");
  const tg = allEdges().filter((e) => e.src === assetId && e.type === "asset_targets_gene");
  return {
    node: brief(assetId), asset_kind: n.attrs.asset_kind, owner: n.attrs.owner_id ? brief(n.attrs.owner_id) : null,
    covers: cov.map((e) => brief(e.dst)), reusability, what_differs, needs_review,
    edge_ids: [...cov, ...own, ...tg].map((e) => e.id),
  };
}
function trialCard(trialId, relevance, eligibility_note) {
  const n = nodes.get(trialId);
  const st = allEdges().filter((e) => e.src === trialId && e.type === "trial_studies_disease");
  const tt = allEdges().filter((e) => e.src === trialId && e.type === "trial_tests_intervention");
  return {
    node: brief(trialId), nct_id: n.attrs.nct_id, phase: n.attrs.phase, overall_status: n.attrs.overall_status,
    intervention: tt.length ? nodes.get(tt[0].dst).label : trialId === T.T2 ? "[MOCK] AAV9-CLN6 gene transfer" : null,
    conditions: st.map((e) => brief(e.dst)), relevance, eligibility_note, url: n.url,
    edge_ids: [...st, ...tt].map((e) => e.id),
  };
}
function personCard(personId) {
  const n = nodes.get(personId);
  const studies = allEdges().filter((e) => e.src === personId && e.type === "person_studies");
  const aff = allEdges().filter((e) => e.src === personId && e.type === "person_affiliated_with");
  return {
    node: brief(personId), affiliation: n.attrs.affiliation, roles: n.attrs.roles,
    works_on: studies.map((e) => brief(e.dst)),
    recent_publications: allEdges().filter((e) => e.src === personId && e.type === "person_authored").length,
    active_grants: allEdges().filter((e) => e.dst === personId && e.type === "grant_funds_person").length,
    contact_url: n.attrs.contact_url, edge_ids: [...studies, ...aff].map((e) => e.id),
  };
}
const RANK = ["curated", "literature", "inferred", "hypothesis"];
function mkPath(id, kind, title, score, from, edgeIds, attrs = {}) {
  const node_ids = [from];
  for (const eid of edgeIds) {
    const e = edges.get(eid); const cur = node_ids.at(-1);
    if (e.src !== cur && e.dst !== cur) throw new Error(`path ${id}: ${eid} does not touch ${cur}`);
    node_ids.push(other(e, cur));
  }
  const es = edgeIds.map(E);
  return {
    id, kind, title, score, from, to: node_ids.at(-1), node_ids, edge_ids: [...edgeIds],
    nodes: node_ids.map(brief), edges: es,
    weakest_status: es.map((e) => e.status).sort((a, b) => RANK.indexOf(b) - RANK.indexOf(a))[0],
    min_confidence: Math.min(...es.map((e) => e.confidence)), attrs,
  };
}

// ============================================================================
// Endpoint payloads
// ============================================================================
const files = {}; // relative path -> object
const put = (rel, obj) => { files[rel] = obj; };

// ---- nodes/ ----------------------------------------------------------------
const ACTION_VIEWS = new Set([D.CLN5, D.X]);
const MECH_VIEWS = new Set([M.LYS]);
for (const id of nodes.keys()) {
  const degree = {};
  for (const e of touching(id)) { const t = nodes.get(other(e, id)).type; degree[t] = (degree[t] ?? 0) + 1; }
  const cl = clusters.flatMap((c) => c.members.filter(([m]) => m === id).map(([, membership]) =>
    ({ id: c.id, label: c.label, size: c.members.length, membership }))).sort((a, b) => b.membership - a.membership);
  put(`nodes/${safeId(id)}.json`, { node: full(id), degree, clusters: cl, has_action_view: ACTION_VIEWS.has(id), has_mechanism_view: MECH_VIEWS.has(id) });
}

// ---- edges/ ----------------------------------------------------------------
for (const [id, e] of edges) {
  const rows = evidence.get(id);
  const by = (s) => rows.filter((r) => r.stance === s);
  put(`edges/${safeId(realId.get(id))}.json`, { edge: E(id), source: brief(e.src), target: brief(e.dst), supporting: by("supports"), contradicting: by("contradicts"), context: by("context") });
}

// ---- neighborhood/ (depth 1, mirrors api_neighborhood) ---------------------
const TYPE_ORDER = (a, b) => a.type.localeCompare(b.type) || a.label.localeCompare(b.label);
function neighborhood(center) {
  const set = new Set([center, ...touching(center).map((e) => other(e, center))]);
  const ns = [...set].map(brief).sort((a, b) => (a.id === center ? -1 : b.id === center ? 1 : TYPE_ORDER(a, b)));
  const es = allEdges().filter((e) => set.has(e.src) && set.has(e.dst)).sort((a, b) => b.confidence - a.confidence || a.id.localeCompare(b.id)).map((e) => E(e.id));
  return { center, nodes: ns, edges: es, truncated: false };
}
put(`neighborhood/${safeId(D.CLN5)}.json`, neighborhood(D.CLN5));
put(`neighborhood/${safeId(M.LYS)}.json`, neighborhood(M.LYS));

// ---- similar/ (mirrors api_similar_diseases) -------------------------------
function shared(a, b, type) {
  const nb = (x) => new Set(touching(x).filter((e) => e.type === type && e.status !== "hypothesis").map((e) => other(e, x)));
  const sa = nb(a), sb = nb(b);
  return [...sa].filter((x) => sb.has(x)).map((x) => nodes.get(x))
    .sort((p, q) => (q.attrs.ic ?? -1) - (p.attrs.ic ?? -1) || p.label.localeCompare(q.label)).slice(0, 8).map((n) => brief(n.id));
}
const sameCluster = (a, b) => clusters.some((c) => c.members.some(([m]) => m === a) && c.members.some(([m]) => m === b));
function similar(id) {
  return touching(id).filter((e) => e.type === "disease_similar_to").sort((a, b) => (b.score ?? 0) - (a.score ?? 0)).map((e) => {
    const o = other(e, id);
    return {
      disease: brief(o), edge_id: e.id, score: e.score, confidence: e.confidence, status: e.status,
      components: structuredClone(e.attrs.components ?? {}), caution: e.attrs.caution ?? null,
      shared: { phenotypes: shared(id, o, "disease_has_phenotype"), mechanisms: shared(id, o, "disease_involves_mechanism"), genes: shared(id, o, "gene_associated_with_disease") },
      same_cluster: sameCluster(id, o),
    };
  });
}
put(`similar/${safeId(D.CLN5)}.json`, similar(D.CLN5));

// ---- paths/ ----------------------------------------------------------------
const PATHS = [
  mkPath("path-mock-cln5-cln6", "related_disease", "CLN5 disease and CLN6 disease share lysosomal storage", 0.86, D.CLN5,
    ["e-dm-cln5-lys", "e-dm-cln6-lys"]),
  mkPath("path-mock-cln5-cln6alliance", "patient_group", "A CLN6 parent group facing the same lysosomal problem", 0.78, D.CLN5,
    ["e-dm-cln5-lys", "e-dm-cln6-lys", "e-os-cln6all-cln6"]),
  mkPath("path-mock-cln5-nhs", "asset", "A CLN6 natural history study that could be adapted for CLN5", 0.72, D.CLN5,
    ["e-sim-cln5-cln6", "e-ac-nhs-cln6"], { reusability: "adaptable" }),
  mkPath("path-mock-cln5-researcher-a", "researcher", "Dr. A. Example published on CLN5 lysosomal storage", 0.7, D.CLN5,
    ["e-pa-p1-cln5", "e-pau-a-p1"]),
  mkPath("path-mock-cln5-cerliponase", "intervention", "HYPOTHESIS: could enzyme replacement (approved for CLN2) apply to CLN5?", 0.35, D.CLN5,
    ["e-dm-cln5-sol", "e-itm-cerl-sol"], { caution: CAUTION_CLN2 }),
];
put(`paths/${safeId(D.CLN5)}.json`, [...PATHS].sort((a, b) => b.score - a.score || a.id.localeCompare(b.id)));

// ---- clusters --------------------------------------------------------------
put("clusters.json", clusters.map(clusterBrief).sort((a, b) => b.size - a.size || a.id.localeCompare(b.id)));
for (const c of clusters)
  put(`clusters/${safeId(c.id)}.json`, {
    cluster: clusterBrief(c),
    members: c.members.map(([id, membership]) => ({ ...brief(id), membership })).sort((a, b) => b.membership - a.membership || a.label.localeCompare(b.label)),
    top_phenotypes: c.attrs.top_phenotypes.map(brief), top_mechanisms: c.attrs.top_mechanisms.map(brief),
  });

// ---- action-view: CLN5 (hero, Maria) ---------------------------------------
const COVERAGE_SOURCES = (q, counts) => [
  { name: "Orphanet", checked: true, query: q, result_count: counts[0], checked_at: "2026-10-01T10:00:00Z" },
  { name: "PubMed", checked: true, query: q, result_count: counts[1], checked_at: "2026-10-01T10:00:00Z" },
  { name: "ClinicalTrials.gov", checked: true, query: q, result_count: counts[2], checked_at: "2026-10-01T10:00:00Z" },
  { name: "NIH RePORTER", checked: true, query: q, result_count: counts[3], checked_at: "2026-10-01T10:00:00Z" },
  { name: "Patient organization websites (Bright Data scrape)", checked: true, query: q, result_count: counts[4], checked_at: "2026-10-01T10:00:00Z" },
  { name: "Global Genes directory", checked: false, query: null, result_count: null, checked_at: null },
];
const sim = Object.fromEntries(similar(D.CLN5).map((s) => [s.disease.id, s]));
const step = (id, kind, title, rationale, target, status, validation_needed, effort, edge_ids) =>
  ({ id, kind, title, rationale, target: target ? brief(target) : null, status, validation_needed, effort, edge_ids });

put(`action-view/${safeId(D.CLN5)}.json`, {
  disease: full(D.CLN5),
  headline: "No approved treatment yet — but CLN5 disease shares a lysosomal pathway with 3 other Batten diseases",
  plain_summary: "CLN5 disease is one of the Batten diseases. In all of them the cell's recycling centre (the lysosome) fails and waste builds up in nerve cells. That shared problem connects your community to CLN6, CLN3 and CLN2 families, their groups, a shared registry and a CLN6 natural history study. One related disease (CLN2) has an approved therapy, but it is not known to work for CLN5.",
  treatment_status: {
    has_approved_treatment: false,
    note: "No approved treatment for CLN5 disease. The closest approved therapy, cerliponase alfa, treats CLN2 disease by replacing a soluble enzyme (TPP1); whether a similar approach could work for CLN5 is only a hypothesis.",
    edge_ids: ["e-it-cerl-cln2", "e-dm-cln5-sol"],
  },
  exact_groups: groupsFor(D.CLN5),
  related_communities: [
    { disease: brief(D.CLN6), similarity: sim[D.CLN6].score, similarity_edge_id: sim[D.CLN6].edge_id,
      why: "Both disrupt lysosomal breakdown in nerve cells and both start in early childhood with seizures and vision loss.",
      differences: ["CLN6 is a membrane protein in the endoplasmic reticulum; CLN5 is a soluble lysosomal protein", "CLN6 already has a natural history study and an early gene therapy trial (mock)"],
      caution: null, groups: groupsFor(D.CLN6) },
    { disease: brief(D.CLN2), similarity: sim[D.CLN2].score, similarity_edge_id: sim[D.CLN2].edge_id,
      why: "Both are childhood Batten diseases with seizures, ataxia and loss of skills, and both involve a soluble lysosomal protein.",
      differences: ["CLN2 is caused by a missing enzyme with a known activity (TPP1); CLN5's exact function is unclear", "CLN2 has an approved enzyme replacement therapy; CLN5 has none"],
      caution: CAUTION_CLN2, groups: groupsFor(D.CLN2) },
    { disease: brief(D.CLN3), similarity: sim[D.CLN3].score, similarity_edge_id: sim[D.CLN3].edge_id,
      why: "Both lead to lysosomal storage and vision loss, and the CLN3 community is the largest Batten community with an established registry.",
      differences: ["CLN3 usually starts later (school age) with isolated vision loss", "CLN3 is a lysosomal membrane protein, not a soluble one"],
      caution: CAUTION_CLN3, groups: groupsFor(D.CLN3) },
  ],
  connections: [...PATHS].sort((a, b) => b.score - a.score),
  assets: [
    assetCard(A.REG, "adaptable", ["Does not list CLN5 disease today", "Collects juvenile-onset (CLN3) milestones that may not fit late-infantile CLN5"],
      ["Would the registry's eligibility rules accept CLN5 families?", "Are CLN5-specific clinical scores needed?"]),
    assetCard(A.NHS, "adaptable", ["Designed for CLN6; CLN5 progression speed may differ", "Outcome scale validated only in CLN6 (mock)"],
      ["Is the CLN6 rating scale valid for CLN5 disease?"]),
    assetCard(A.SHEEP, "direct", ["Large-animal model; access is restricted"], []),
  ],
  trials: [
    trialCard(T.T2, "related_disease", "For CLN6 disease only. Useful as a template for a future CLN5 study design."),
    trialCard(T.T1, "shared_mechanism", "Completed; CLN2 only. Shown as a counterexample — its enzyme replacement approach is not known to apply to CLN5."),
  ],
  researchers: [personCard(H.A), personCard(H.B)],
  shared_people: [{ person: personCard(H.A), communities: [brief(D.CLN5), brief(D.CLN3)], edge_ids: ["e-ps-a-cln5", "e-ps-a-cln3"] }],
  next_steps: [
    step("ns-mock-cln5-1", "contact_group", "Introduce your group to the CLN6 Parents Alliance and compare notes on their natural history study",
      "CLN6 disease is the most similar disease to CLN5 in the atlas, and its parent group already supports a natural history study.",
      O.CLN6ALL, "viable", [], "this_week", ["e-sim-cln5-cln6", "e-os-cln6all-cln6", "e-ac-nhs-cln6"]),
    step("ns-mock-cln5-2", "join_registry", "Ask the International NCL Registry Consortium whether their registry accepts CLN5 families",
      "The registry already covers CLN2, CLN3 and CLN6; CLN5 is not listed yet.",
      A.REG, "needs_review", ["Registry eligibility criteria for CLN5"], "this_month", ["e-ac-reg-cln6", "e-om-cons-reg"]),
    step("ns-mock-cln5-3", "contact_researcher", "Email Dr. A. Example, who studies both CLN5 and CLN3 lysosomal storage",
      "Active in two Batten communities and funded to compare lysosomal dysfunction across CLN subtypes.",
      H.A, "viable", [], "this_week", ["e-ps-a-cln5", "e-ps-a-cln3", "e-gf-gr-a"]),
    step("ns-mock-cln5-4", "adapt_study_design", "Ask the CLN6 natural history study team whether their protocol could be adapted for CLN5",
      "A shared protocol would make CLN5 data comparable with CLN6 and could speed up future trials.",
      A.NHS, "needs_review", ["Is the CLN6 rating scale valid for CLN5?", "Ethics approval for a new cohort"], "this_quarter", ["e-ac-nhs-cln6", "e-om-hosp-nhs", "e-sim-cln5-cln6"]),
    step("ns-mock-cln5-5", "validate_experiment", "Do NOT assume CLN2's enzyme replacement works for CLN5 — ask an expert whether CLN5 could be cross-corrected",
      "This link is an atlas hypothesis with contradicting evidence: CLN5 is soluble like TPP1, but no robust enzyme activity has been shown.",
      M.SOL, "unsupported", ["Cell experiment: does added CLN5 protein reduce storage in CLN5-deficient cells?", "Expert review of the contradicting paper"], "this_quarter", ["e-dm-cln5-sol", "e-sim-cln5-cln2"]),
    step("ns-mock-cln5-6", "contribute_evidence", "Tell us about CLN5 groups, registries or studies we are missing",
      "Coverage of non-English patient groups is incomplete.",
      null, "viable", [], "this_week", []),
  ],
  coverage: {
    has_supported_route: true,
    sources: COVERAGE_SOURCES("CLN5 disease", [1, 48, 2, 3, 2]),
    gaps: [
      { question: "Does CLN5 loss affect the same step of lysosomal degradation as CLN6?",
        why_it_matters: "If yes, CLN6 study designs and outcome measures can likely be reused for CLN5.",
        what_would_resolve: "A side-by-side proteomics comparison of CLN5- and CLN6-deficient neurons." },
      { question: "Can CLN5 protein be taken up from outside the cell (cross-correction) the way TPP1 can?",
        why_it_matters: "It decides whether an enzyme-replacement strategy is even worth exploring for CLN5.",
        what_would_resolve: "An uptake-and-rescue experiment in CLN5-deficient patient cells." },
    ],
  },
});

// ---- action-view: subtype X (Devon, honest gap) -----------------------------
const simX = similar(D.X).find((s) => s.disease.id === D.CLN5);
put(`action-view/${safeId(D.X)}.json`, {
  disease: full(D.X),
  headline: "We found no patient group for this exact disease — here is the closest community and how to start one",
  plain_summary: "This (fictional) subtype is known from only a few families. The atlas has no patient group, registry or trial for it. Based on two shared symptoms and an inferred lysosomal mechanism, the closest community is CLN5 disease — but this link is weak and needs a specialist's review.",
  treatment_status: { has_approved_treatment: null, note: "Unknown: no treatment studies were found for this subtype.", edge_ids: [] },
  exact_groups: [],
  related_communities: [
    { disease: brief(D.CLN5), similarity: simX.score, similarity_edge_id: simX.edge_id,
      why: "Both cause seizures and vision loss in childhood and may involve the same lysosomal problem.",
      differences: ["Different gene (MOCKG1 vs CLN5)", "The shared mechanism is inferred, not shown in any paper"],
      caution: simX.caution, groups: groupsFor(D.CLN5) },
  ],
  connections: [],
  assets: [],
  trials: [],
  researchers: [],
  shared_people: [],
  next_steps: [
    step("ns-mock-x-1", "build_missing_group", "Start a small family network for subtype X — BDSRA-style umbrella groups may help you set it up",
      "No group serves this disease today; even a handful of connected families can start a registry later.",
      null, "viable", [], "this_month", []),
    step("ns-mock-x-2", "contact_group", "Ask the CLN5 Families Network whether families with subtype X could join their meetings",
      "CLN5 disease is the closest match in the atlas, but the link is weak.",
      O.CLN5FAM, "needs_review", ["A specialist should confirm subtype X is clinically close to CLN5"], "this_month", ["e-sim-x-cln5", "e-os-cln5fam-cln5"]),
    step("ns-mock-x-3", "contribute_evidence", "Send us any paper, clinic or group that mentions this subtype",
      "Our sources returned almost nothing; your knowledge fills the gap.",
      null, "viable", [], "this_week", []),
  ],
  coverage: {
    has_supported_route: false,
    sources: COVERAGE_SOURCES("NCL subtype X OR MOCKG1", [0, 1, 0, 0, 0]),
    gaps: [
      { question: "Is subtype X really a lysosomal (NCL) disease?",
        why_it_matters: "All the connections shown depend on this inferred mechanism.",
        what_would_resolve: "Electron microscopy of a skin or blood sample looking for storage material." },
    ],
  },
});

// ---- mechanism-view: lysosomal degradation ---------------------------------
const clusterOf = (d) => {
  const best = clusters.flatMap((c) => c.members.filter(([m]) => m === d).map(([, w]) => ({ c, w }))).sort((a, b) => b.w - a.w)[0];
  return best ? { id: best.c.id, label: best.c.label } : null;
};
const ranked = (d, score, assets, unmet_need) => {
  const eid = find(d, M.LYS, "disease_involves_mechanism");
  return { disease: brief(d), score, status: edges.get(eid).status, evidence_edge_ids: [eid], cluster: clusterOf(d), groups: groupsFor(d), assets, unmet_need };
};
put(`mechanism-view/${safeId(M.LYS)}.json`, {
  mechanism: full(M.LYS),
  headline: "One lysosomal failure, at least 6 gene names, 5 diseases in this slice",
  plain_summary: "Every Batten disease ends in the same place — waste piling up in lysosomes — but the broken part differs. Soluble-enzyme forms (CLN2, maybe CLN5) can in principle be treated by supplying the enzyme; membrane-protein forms (CLN3, CLN6) cannot.",
  genes: [G.CLN5, G.CLN6, G.CLN3, G.TPP1, G.PPT1, G.MFSD8].map(brief),
  diseases: [
    ranked(D.CLN2, 0.95, [assetCard(A.REG, "direct", [], [])], null),
    ranked(D.CLN5, 0.85, [assetCard(A.SHEEP, "direct", ["Large-animal model; access is restricted"], [])], "No approved therapy; [MOCK] fewer than 100 known patients worldwide"),
    ranked(D.CLN6, 0.84, [assetCard(A.NHS, "direct", [], [])], "No approved therapy; early gene therapy trial (mock)"),
    ranked(D.CLN3, 0.82, [assetCard(A.REG, "direct", [], [])], "No approved therapy"),
    ranked(D.X, 0.4, [], "No approved therapy; no known patient group"),
  ],
  clusters: clusters.map((c) => ({ cluster: clusterBrief(c), disease_ids: c.members.map(([m]) => m), score: c.attrs.cohesion })),
  researchers: [personCard(H.B), personCard(H.A)],
  trials: [trialCard(T.T1, "shared_mechanism", "CLN2 only."), trialCard(T.T2, "shared_mechanism", "CLN6 only.")],
  interventions: [brief(CERL)],
});

// ---- search.json (map: query -> SearchResponse; mirrors api_search scoring) --
function search(q) {
  q = q.toLowerCase().trim();
  if (q.length < 2) return [];
  const best = new Map();
  for (const n of nodes.values()) {
    const names = [{ name: n.id, kind: "id" }, { name: n.label, kind: "label" },
      ...n.synonyms.map((name) => ({ name, kind: "synonym" })), ...(extraNames.get(n.id) ?? []),
      ...Object.values(n.xrefs).flat().map((name) => ({ name, kind: "xref" }))];
    for (const { name, kind } of names) {
      const l = name.toLowerCase();
      const s = l === q ? 1.0 : l.startsWith(q) ? 0.9 : l.includes(q) ? 0.6 : 0;
      if (s > 0 && (!best.has(n.id) || best.get(n.id).score < s)) best.set(n.id, { matched_name: name, match_kind: kind, score: s });
    }
  }
  return [...best.entries()].map(([id, m]) => ({ ...brief(id), ...m }))
    .sort((a, b) => b.score - a.score || (b.type === "disease") - (a.type === "disease") || a.label.localeCompare(b.label)).slice(0, 20);
}
put("search.json", Object.fromEntries(["cln5", "batten", "cln", "lysosom", "seizure", "bdsra", "zzz"].map((q) => [q, search(q)])));

// ---- AI endpoints ----------------------------------------------------------
put("explain.json", {
  headline: "CLN5 disease and CLN6 disease break the same recycling system in nerve cells",
  steps: [
    { text: "In CLN5 disease, nerve cells cannot properly break down waste in their lysosomes, so it builds up (reported in a research paper).",
      edge_id: "e-dm-cln5-lys", status: edges.get("e-dm-cln5-lys").status, confidence: edges.get("e-dm-cln5-lys").confidence },
    { text: "The same lysosomal build-up is reported in CLN6 disease, which is why the atlas links the two communities.",
      edge_id: "e-dm-cln6-lys", status: edges.get("e-dm-cln6-lys").status, confidence: edges.get("e-dm-cln6-lys").confidence },
  ],
  uncertainties: [
    "Both links come from a single (mock) paper; they have not been independently confirmed.",
    "The broken protein is different: CLN6 sits in a membrane, CLN5 is soluble — so treatments may not transfer.",
  ],
  what_to_check_next: "Ask a CLN5/CLN6 researcher whether the two diseases block the same step of lysosomal breakdown.",
  model: "mock-model",
  cached: true,
});
const OUT_EDGES = ["e-sim-cln5-cln6", "e-dm-cln5-lys", "e-dm-cln6-lys", "e-ac-nhs-cln6"];
put("outreach-draft.json", {
  subject: "CLN5 families would love to learn from your CLN6 natural history study",
  body: "Dear CLN6 Parents Alliance,\n\nI lead the CLN5 Families Network, a small parent group for CLN5 disease, which has no approved treatment. While exploring the Rare Disease Atlas we saw that CLN5 and CLN6 disease look very similar in symptoms [1] and that both involve the same failure of lysosomal breakdown in nerve cells [2][3].\n\nWe understand you support a CLN6 natural history study [4]. Would you be open to a short call to share how it was set up, and whether parts of its design could work for CLN5 families? We know the two diseases are not identical, so we would value your honest view of what would and would not transfer.\n\nWarm regards,\nMaria\nCLN5 Families Network (mock)",
  citations: OUT_EDGES.map((eid, i) => { const r = evidence.get(eid)[0]; return { n: i + 1, edge_id: eid, source_ref: r.source_ref, url: r.url }; }),
  model: "mock-model",
});
put("gap-search-job.json", {
  job_id: "job-mock-0001",
  state: "done",
  result: {
    leads: [
      { title: "[MOCK] Forum thread: parents of children with an unnamed NCL variant", url: ex("forum/ncl-variant-thread"),
        snippet: "[MOCK] Our daughter was diagnosed with a very rare NCL subtype and we are looking for other families...", kind: "patient_group" },
      { title: "[MOCK] Hospital page: rare NCL subtypes research cohort", url: ex("hospital/rare-ncl-cohort"),
        snippet: "[MOCK] We are recruiting families with genetically unexplained NCL to a research cohort.", kind: "study" },
      { title: "[MOCK] News: families form new Batten subtype network", url: ex("news/new-batten-network"),
        snippet: "[MOCK] A handful of families have started an informal network for an ultra-rare Batten subtype.", kind: "news" },
    ],
    disclaimer: "These are UNVERIFIED web search results found automatically. They have not been checked by the atlas team or an expert — verify before contacting anyone or sharing medical information.",
  },
  error: null,
});
put("error-not-found.json", { error: { code: "not_found", message: "No node with id 'MONDO:MOCK404' in this dataset.", request_id: "req-mock-0001" } });

// ---- meta ------------------------------------------------------------------
const nodeCounts = {};
for (const n of nodes.values()) nodeCounts[n.type] = (nodeCounts[n.type] ?? 0) + 1;
const allEv = [...evidence.values()].flat();
put("meta.json", {
  contract_version: "1.0.0",
  dataset: { version: "mock-2026.10.03", slice: "MOCK: neuronal ceroid lipofuscinoses (Batten disease) within lysosomal storage diseases", built_at: "2026-10-03T12:00:00Z", mock: true },
  counts: { nodes: nodeCounts, edges: edges.size, evidence: allEv.length, clusters: clusters.length },
  sources: [...new Set(allEv.map((r) => r.source_name))].sort(),
});

// ============================================================================
// Write
// ============================================================================
for (const sub of ["nodes", "edges", "neighborhood", "similar", "paths", "clusters", "action-view", "mechanism-view"])
  fs.rmSync(path.join(OUT, sub), { recursive: true, force: true });
for (const [rel, obj] of Object.entries(files)) {
  const p = path.join(OUT, rel);
  fs.mkdirSync(path.dirname(p), { recursive: true });
  // swap readable edge aliases ("e-…") for the real "E:<sha1>" ids
  const json = JSON.stringify(obj, null, 2).replace(/"(e-[a-z0-9-]+)"/g, (m, a) => (realId.has(a) ? JSON.stringify(realId.get(a)) : m));
  fs.writeFileSync(p, json + "\n");
}
console.log(`wrote ${Object.keys(files).length} fixture files (${nodes.size} nodes, ${edges.size} edges, ${allEv.length} evidence rows) to ${OUT}`);
