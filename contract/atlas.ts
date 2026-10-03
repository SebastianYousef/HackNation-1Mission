/**
 * Rare Disease Atlas — API CONTRACT v1.0.0
 *
 * THE BRIDGE between frontend and backend. Single source of truth for every
 * request and response shape of the REST API served at  {API_BASE}/api/v1/...
 *
 *  - Backend MUST return exactly these shapes (checked by backend/scripts/check_contract.py).
 *  - Frontend MUST only rely on these shapes (copied verbatim to src/contract/atlas.ts).
 *  - contract/fixtures/*.json are valid example responses for every endpoint.
 *  - Changing this file = contract change. Follow contract/README.md (both sides approve).
 *
 * Conventions: ids are CURIE strings ("MONDO:0016295", "HP:0001250", "NCT01234567") and
 * MUST be URL-encoded in paths (encodeURIComponent). Nullable fields are `| null`, never
 * omitted. Arrays are never null (empty array instead). Dates are ISO-8601 strings.
 */

export const CONTRACT_VERSION = "1.0.0";

// ============================================================================
// Enums
// ============================================================================

export type NodeType =
  | "disease" | "gene" | "variant" | "phenotype" | "mechanism" | "intervention"
  | "organization" | "person" | "publication" | "trial" | "grant" | "asset";

/**
 * Node subtypes (open-ended strings, these are the ones the UI should style):
 *  organization: patient_group | foundation | company | funder | investor | academic | hospital
 *  asset:        registry | natural_history_study | animal_model | cell_model | biomarker | biobank | trial_design
 *  mechanism:    pathway | biological_process | variant_effect
 *  intervention: small_molecule | enzyme_replacement | gene_therapy | antisense_oligonucleotide | other
 *  person:       researcher | clinician | industry | funder | investor
 */
export type NodeSubtype = string;

export type EdgeType =
  | "gene_associated_with_disease"   // gene → disease
  | "variant_of_gene"                // variant → gene
  | "variant_associated_with_disease"// variant → disease
  | "gene_in_mechanism"              // gene → mechanism
  | "disease_involves_mechanism"     // disease → mechanism
  | "disease_has_phenotype"          // disease → phenotype   attrs: {frequency?, onset?}
  | "disease_subtype_of"             // disease → disease (MONDO hierarchy)
  | "disease_similar_to"             // disease → disease     inferred; score 0..1; attrs: {components, caution?}
  | "intervention_targets_mechanism" // intervention → mechanism
  | "intervention_treats_disease"    // intervention → disease attrs: {approval?: "approved"|"investigational"}
  | "trial_studies_disease"          // trial → disease
  | "trial_tests_intervention"       // trial → intervention
  | "publication_about"              // publication → disease|gene|mechanism
  | "person_authored"                // person → publication
  | "person_studies"                 // person → disease|gene|mechanism
  | "person_affiliated_with"         // person → organization
  | "grant_funds_person"             // grant → person
  | "grant_studies"                  // grant → disease|gene|mechanism
  | "organization_funds_grant"       // organization → grant
  | "organization_serves_disease"    // organization → disease
  | "organization_maintains_asset"   // organization → asset
  | "asset_covers_disease"           // asset → disease
  | "asset_targets_gene";            // asset → gene

/**
 * How we know an edge. The UI MUST make this visible on every edge.
 *  curated    — asserted by a curated database (HPO, Orphanet, ClinVar, MONDO…)   = data
 *  literature — stated in a paper/registry/org site; we hold the quote            = reported claim
 *  inferred   — computed by our analytics (similarity, clustering)                = derived
 *  hypothesis — proposed by the atlas; must be tested before anyone acts on it    = hypothesis
 */
export type EdgeStatus = "curated" | "literature" | "inferred" | "hypothesis";

export type EvidenceStance = "supports" | "contradicts" | "context";
export type SourceType =
  | "database" | "publication" | "trial_registry" | "grant_database"
  | "patient_org_site" | "web" | "computed";

export type Audience = "family" | "expert";

// ============================================================================
// Core graph objects
// ============================================================================

export interface NodeBrief {
  id: string;
  type: NodeType;
  subtype: NodeSubtype | null;
  label: string;
  /** 1–2 sentence plain-language summary a family can read. */
  summary: string | null;
}

export interface NodeFull extends NodeBrief {
  description: string | null;           // technical definition
  synonyms: string[];
  xrefs: Record<string, string[]>;      // {"OMIM": ["256731"], "ORPHA": ["228346"]}
  /**
   * Type-specific attributes. Known keys (all optional):
   *  disease:      prevalence, onset, inheritance, has_approved_treatment (bool), approved_treatments (string[])
   *  phenotype:    ic (information content; higher = more specific/informative symptom)
   *  organization: website, country, contact_url, has_registry (bool), email_public
   *  person:       affiliation, orcid, roles (string[]), contact_url
   *  trial:        nct_id, phase, overall_status, start_date, enrollment
   *  grant:        project_num, fiscal_year, amount_usd, agency
   *  asset:        asset_kind, access ("open"|"on_request"|"restricted"), owner_id
   *  publication:  pmid, year, journal, title
   */
  attrs: Record<string, unknown>;
  url: string | null;
}

export interface Edge {
  id: string;
  src: string;          // node id
  dst: string;          // node id
  type: EdgeType;
  label: string | null; // short human predicate: "causes", "shares pathway with"
  status: EdgeStatus;
  confidence: number;   // 0..1
  score: number | null; // type-specific strength (similarity etc.)
  support_count: number;
  contradict_count: number;
  sources: string[];    // e.g. ["HPO","PubMed"]
  attrs: Record<string, unknown>;
}

export interface Evidence {
  id: string;
  stance: EvidenceStance;
  source_type: SourceType;
  source_name: string;        // "HPO" | "ClinVar" | "PubMed" | "ClinicalTrials.gov" | "NIH RePORTER" | "NORD" | "Atlas analytics" | …
  source_ref: string | null;  // "PMID:123" | "NCT0123" | "ORPHA:228349"
  url: string | null;
  quote: string | null;       // verbatim supporting text
  method: string;             // "curated" | "llm:<model>" | "algorithm:<name>" | "scrape:brightdata"
  published_at: string | null;
  retrieved_at: string;
}

// ============================================================================
// Endpoint responses
// ============================================================================

/** GET /api/v1/meta */
export interface MetaResponse {
  contract_version: string;
  dataset: { version?: string; slice?: string; built_at?: string; [k: string]: unknown };
  counts: { nodes: Partial<Record<NodeType, number>>; edges: number; evidence: number; clusters: number };
  sources: string[];
}

/** GET /api/v1/search?q=&types=disease,gene&limit=20   (q min length 2; limit max 50) */
export interface SearchHit extends NodeBrief {
  matched_name: string;                 // which name/synonym matched (show "matched: Batten disease")
  match_kind: "id" | "label" | "synonym" | "xref" | "abbreviation";
  score: number;                        // 0..1
}
export type SearchResponse = SearchHit[];

/** GET /api/v1/nodes/{id} */
export interface NodeResponse {
  node: NodeFull;
  degree: Partial<Record<NodeType, number>>;   // neighbour counts by type
  clusters: { id: string; label: string; size: number; membership: number }[];
  has_action_view: boolean;                    // GET /diseases/{id}/action-view will return 200
  has_mechanism_view: boolean;                 // GET /mechanisms/{id}/view will return 200
}

/**
 * GET /api/v1/nodes/{id}/neighborhood
 *   ?depth=1..3 (default 1) &edge_types=a,b &statuses=curated,literature
 *   &min_confidence=0..1 (default 0) &max_nodes=2..300 (default 60)
 * Every edge in `edges` has both endpoints in `nodes`.
 */
export interface NeighborhoodResponse {
  center: string;
  nodes: NodeBrief[];
  edges: Edge[];
  truncated: boolean;     // true = more neighbours exist than max_nodes; show "showing top N"
}

/** GET /api/v1/edges/{id} — everything needed for the "why is this connected?" panel */
export interface EdgeResponse {
  edge: Edge;
  source: NodeBrief;
  target: NodeBrief;
  supporting: Evidence[];
  contradicting: Evidence[];
  context: Evidence[];
}

/** GET /api/v1/diseases/{id}/similar?limit=10 */
export interface SimilarDisease {
  disease: NodeBrief;
  edge_id: string;                       // the disease_similar_to edge (open with /edges/{id})
  score: number | null;
  confidence: number;
  status: EdgeStatus;
  components: { phenotype?: number; mechanism?: number; gene?: number };
  /** Set when diseases look alike but differ mechanistically — a counterexample. Show prominently. */
  caution: string | null;
  /** Linked to both diseases by curated/literature/inferred edges (hypothesis edges excluded). */
  shared: { phenotypes: NodeBrief[]; mechanisms: NodeBrief[]; genes: NodeBrief[] };
  same_cluster: boolean;
}
export type SimilarResponse = SimilarDisease[];

/** GET /api/v1/paths?from=&to=&kind=&limit=   — precomputed, explainable connections */
export type PathKind = "related_disease" | "patient_group" | "asset" | "researcher" | "trial" | "intervention";
export interface Path {
  id: string;
  kind: PathKind;
  title: string;
  score: number;
  from: string;
  to: string;
  node_ids: string[];             // ordered, ≥ 2 entries, no repeats (simple path)
  /** ordered, ≥ 1 entry; edge_ids[i] connects node_ids[i] and node_ids[i+1] in EITHER direction
   *  (edge.src/dst keep their semantic direction; a path may traverse an edge backwards). */
  edge_ids: string[];
  nodes: NodeBrief[];             // same order as node_ids
  edges: Edge[];                  // same order as edge_ids
  weakest_status: EdgeStatus;     // a path is only as strong as its weakest edge
  min_confidence: number;
  attrs: Record<string, unknown>;
}
export type PathsResponse = Path[];

/** GET /api/v1/clusters */
export interface ClusterBrief {
  id: string;
  label: string;
  summary: string | null;
  method: string;
  size: number;
  attrs: Record<string, unknown>;   // {cohesion?, top_phenotypes?: string[], top_mechanisms?: string[]}
}
export type ClustersResponse = ClusterBrief[];

/** GET /api/v1/clusters/{id} */
export interface ClusterResponse {
  cluster: ClusterBrief;
  members: (NodeBrief & { membership: number })[];
  top_phenotypes: NodeBrief[];
  top_mechanisms: NodeBrief[];
}

// ---------------------------------------------------------------------------
// Composite screens (precomputed by the pipeline, served as-is)
// ---------------------------------------------------------------------------

export interface OrgCard {
  node: NodeBrief;
  for_disease: NodeBrief;          // which disease this group serves
  edge_id: string;                 // organization_serves_disease edge
  website: string | null;
  contact_url: string | null;
  country: string | null;
  has_registry: boolean | null;
}

export interface RelatedCommunity {
  disease: NodeBrief;
  similarity: number;
  similarity_edge_id: string;
  why: string;                     // one plain sentence: "Both disrupt lysosomal protein breakdown and share early vision loss"
  differences: string[];           // what differs — must be shown next to `why`
  caution: string | null;
  groups: OrgCard[];
}

export type Reusability = "direct" | "adaptable" | "reference_only";
export interface AssetCard {
  node: NodeBrief;
  asset_kind: string;              // registry | natural_history_study | animal_model | …
  owner: NodeBrief | null;
  covers: NodeBrief[];             // diseases it covers today
  reusability: Reusability;
  what_differs: string[];
  needs_review: string[];          // biological/eligibility questions for an expert
  edge_ids: string[];
}

export interface TrialCard {
  node: NodeBrief;
  nct_id: string;
  phase: string | null;
  overall_status: string | null;   // RECRUITING | COMPLETED | …
  intervention: string | null;
  conditions: NodeBrief[];
  relevance: "same_disease" | "related_disease" | "shared_mechanism";
  eligibility_note: string | null;
  url: string;
  edge_ids: string[];
}

export type PersonRole = "researcher" | "clinician" | "industry" | "funder" | "investor";
export interface PersonCard {
  node: NodeBrief;
  affiliation: string | null;
  roles: PersonRole[];
  works_on: NodeBrief[];
  recent_publications: number;
  active_grants: number;
  contact_url: string | null;
  edge_ids: string[];
}

/** Network overlap: one person active in several "unrelated" communities. */
export interface SharedPerson {
  person: PersonCard;
  communities: NodeBrief[];        // diseases
  edge_ids: string[];
}

export type NextStepKind =
  | "contact_group" | "join_registry" | "adapt_study_design" | "contact_researcher"
  | "validate_experiment" | "apply_funding" | "build_missing_group" | "contribute_evidence";
export type NextStepStatus = "viable" | "needs_review" | "unsupported";
export interface NextStep {
  id: string;
  kind: NextStepKind;
  title: string;                   // imperative, doable this week: "Ask BDSRA whether their registry accepts CLN5 families"
  rationale: string;
  target: NodeBrief | null;
  status: NextStepStatus;
  validation_needed: string[];
  effort: "this_week" | "this_month" | "this_quarter";
  edge_ids: string[];              // the evidence behind this step
}

export interface CoverageSource {
  name: string;                    // "Orphanet", "PubMed", "ClinicalTrials.gov", "NORD", …
  checked: boolean;
  query: string | null;
  result_count: number | null;
  checked_at: string | null;
}
export interface Gap {
  question: string;                // "Does CLN5 loss affect the same step of lysosomal degradation as CLN6?"
  why_it_matters: string;
  what_would_resolve: string;      // the next experiment / data that would answer it
}
export interface Coverage {
  has_supported_route: boolean;    // false → UI shows the honest "no supported lead" state
  sources: CoverageSource[];
  gaps: Gap[];
}

/** GET /api/v1/diseases/{id}/action-view — Maria's & Devon's screen. 404 if not precomputed. */
export interface ActionView {
  disease: NodeFull;
  headline: string;                // "No approved treatment yet — but your disease shares a pathway with 3 others"
  plain_summary: string;
  treatment_status: { has_approved_treatment: boolean | null; note: string; edge_ids: string[] };
  exact_groups: OrgCard[];         // groups for exactly this disease (may be empty!)
  related_communities: RelatedCommunity[];
  connections: Path[];
  assets: AssetCard[];
  trials: TrialCard[];
  researchers: PersonCard[];
  shared_people: SharedPerson[];
  next_steps: NextStep[];
  coverage: Coverage;
}

/** GET /api/v1/mechanisms/{id}/view — Priya's & Dr. Osei's screen (id = mechanism or intervention). */
export interface RankedDisease {
  disease: NodeBrief;
  score: number;
  status: EdgeStatus;
  evidence_edge_ids: string[];
  cluster: { id: string; label: string } | null;
  groups: OrgCard[];
  assets: AssetCard[];
  unmet_need: string | null;       // "No approved therapy; ~X patients worldwide"
}
export interface MechanismView {
  mechanism: NodeFull;
  headline: string;
  plain_summary: string;
  genes: NodeBrief[];              // every gene name this mechanism hides under
  diseases: RankedDisease[];       // ranked candidates
  clusters: { cluster: ClusterBrief; disease_ids: string[]; score: number }[];
  researchers: PersonCard[];
  trials: TrialCard[];
  interventions: NodeBrief[];
}

// ---------------------------------------------------------------------------
// AI endpoints (live, may take 2–15 s; responses are cached server-side)
// ---------------------------------------------------------------------------

/** POST /api/v1/explain   body: ExplainRequest */
export interface ExplainRequest {
  edge_ids: string[];              // ordered path, 1..8 edges
  audience: Audience;
}
export interface ExplanationStep {
  text: string;                    // one plain sentence
  edge_id: string;                 // every sentence cites exactly one edge
  status: EdgeStatus;
  confidence: number;
}
export interface Explanation {
  headline: string;
  steps: ExplanationStep[];
  uncertainties: string[];         // what is NOT known / contradicted
  what_to_check_next: string;
  model: string | null;
  cached: boolean;
}

/** POST /api/v1/outreach-draft   — a sourced message Maria can send to a partner */
export interface OutreachRequest {
  disease_id: string;              // the sender's disease
  target_id: string;               // organization or person
  edge_ids: string[];              // evidence to cite
}
export interface OutreachDraft {
  subject: string;
  body: string;                    // plain text, citations as [1], [2]
  citations: { n: number; edge_id: string; source_ref: string | null; url: string | null }[];
  model: string | null;
}

/** POST /api/v1/gap-search → 202 {job_id}; poll GET /api/v1/jobs/{job_id} every 2 s. */
export interface GapSearchRequest { disease_id: string }
export interface JobAccepted { job_id: string }
export interface WebLead {
  title: string;
  url: string;
  snippet: string;
  kind: "patient_group" | "registry" | "study" | "news" | "other";
}
export interface JobStatus {
  job_id: string;
  state: "queued" | "running" | "done" | "failed";
  /** Leads are UNVERIFIED web results — the UI must label them as such. */
  result: { leads: WebLead[]; disclaimer: string } | null;
  error: string | null;
}

/** POST /api/v1/submissions → 201 {id}. Patient groups contribute missing evidence (reviewed later). */
export interface SubmissionRequest {
  node_id: string | null;
  kind: "missing_group" | "missing_asset" | "correction" | "new_evidence" | "other";
  url: string | null;
  note: string;                    // 1..2000 chars
  contact: string | null;
}
export interface SubmissionCreated { id: string }

// ============================================================================
// Errors — every non-2xx response has this body
// ============================================================================
export interface ApiError {
  error: {
    code: "not_found" | "bad_request" | "rate_limited" | "upstream_unavailable" | "internal";
    message: string;
    request_id: string;
  };
}

// ============================================================================
// The client interface the frontend implements twice (HTTP + mock).
// ============================================================================
export interface AtlasApi {
  meta(): Promise<MetaResponse>;
  search(q: string, opts?: { types?: NodeType[]; limit?: number }): Promise<SearchResponse>;
  node(id: string): Promise<NodeResponse>;
  neighborhood(id: string, opts?: {
    depth?: number; edgeTypes?: EdgeType[]; statuses?: EdgeStatus[];
    minConfidence?: number; maxNodes?: number;
  }): Promise<NeighborhoodResponse>;
  edge(id: string): Promise<EdgeResponse>;
  similar(diseaseId: string, opts?: { limit?: number }): Promise<SimilarResponse>;
  paths(from: string, opts?: { to?: string; kind?: PathKind; limit?: number }): Promise<PathsResponse>;
  clusters(): Promise<ClustersResponse>;
  cluster(id: string): Promise<ClusterResponse>;
  actionView(diseaseId: string): Promise<ActionView>;
  mechanismView(id: string): Promise<MechanismView>;
  explain(req: ExplainRequest): Promise<Explanation>;
  outreachDraft(req: OutreachRequest): Promise<OutreachDraft>;
  startGapSearch(req: GapSearchRequest): Promise<JobAccepted>;
  job(jobId: string): Promise<JobStatus>;
  submit(req: SubmissionRequest): Promise<SubmissionCreated>;
}
