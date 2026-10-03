-- =============================================================================
-- Rare Disease Atlas — storage schema (BACKEND-OWNED)
--
-- Nobody but our API servers talks to this database. The browser calls the
-- REST API (contract/API.md); the API servers call the api_* SQL functions in
-- 20261003000002_api_v1.sql. Change tables freely as long as the api_*
-- outputs keep their documented shape.
-- =============================================================================

create extension if not exists pg_trgm;
create extension if not exists vector;

-- -----------------------------------------------------------------------------
-- Nodes: one row per real-world entity, keyed by a stable CURIE.
--   disease      MONDO:0016295        gene         HGNC:2073
--   variant      CLINVAR:12345        phenotype    HP:0001250
--   mechanism    REACT:R-HSA-..., GO:..., or ATLAS:mech-<slug> (curated)
--   intervention CHEBI:..., ATLAS:int-<slug>
--   organization ORG:<slug>           person       PERSON:<orcid|slug>
--   publication  PMID:12345678        trial        NCT01234567
--   grant        NIH:<project_num>    asset        ASSET:<slug>
-- -----------------------------------------------------------------------------
create table if not exists nodes (
  id            text primary key,
  type          text not null check (type in (
                  'disease','gene','variant','phenotype','mechanism','intervention',
                  'organization','person','publication','trial','grant','asset')),
  subtype       text,                       -- see contract/API.md "Node subtypes"
  label         text not null,              -- preferred display name
  description   text,                       -- technical definition (source wording)
  plain_summary text,                       -- 1–2 sentences a family can read
  synonyms      text[] not null default '{}',
  xrefs         jsonb  not null default '{}',   -- {"OMIM":["..."],"ORPHA":["..."]}
  attrs         jsonb  not null default '{}',   -- type-specific, see contract
  url           text,                       -- canonical external page
  embedding     vector(1536),               -- name+definition embedding (reconcile/search)
  updated_at    timestamptz not null default now()
);
create index if not exists nodes_type_idx on nodes(type);

-- Every searchable name for a node (label, synonyms, xrefs, abbreviations, the id itself).
create table if not exists node_names (
  node_id text not null references nodes(id) on delete cascade,
  name    text not null,
  kind    text not null check (kind in ('id','label','synonym','xref','abbreviation')),
  primary key (node_id, name)
);
create index if not exists node_names_trgm_idx on node_names using gin (lower(name) gin_trgm_ops);

-- -----------------------------------------------------------------------------
-- Edges: one row per (type, src, dst). Directed as documented per type; the
-- API treats every edge as traversable both ways.
--   status:  curated    asserted by a curated database (HPO, Orphanet, ClinVar…)
--            literature extracted from papers/registries with a quote
--            inferred   computed by our analytics (similarity, clustering)
--            hypothesis proposed by the atlas; must be tested before acting
-- -----------------------------------------------------------------------------
create table if not exists edges (
  id               text primary key,        -- 'E:' || first 16 hex of sha1(type|src|dst)
  src              text not null references nodes(id) on delete cascade,
  dst              text not null references nodes(id) on delete cascade,
  type             text not null check (type in (
                     'gene_associated_with_disease','variant_of_gene','variant_associated_with_disease',
                     'gene_in_mechanism','disease_involves_mechanism','disease_has_phenotype',
                     'disease_subtype_of','disease_similar_to',
                     'intervention_targets_mechanism','intervention_treats_disease',
                     'trial_studies_disease','trial_tests_intervention',
                     'publication_about','person_authored','person_studies','person_affiliated_with',
                     'grant_funds_person','grant_studies','organization_funds_grant',
                     'organization_serves_disease','organization_maintains_asset',
                     'asset_covers_disease','asset_targets_gene')),
  status           text not null check (status in ('curated','literature','inferred','hypothesis')),
  confidence       real not null check (confidence >= 0 and confidence <= 1),
  score            real,                    -- type-specific strength (e.g. similarity 0..1)
  label            text,                    -- short human predicate, e.g. "causes", "shares pathway with"
  attrs            jsonb not null default '{}',
  support_count    int  not null default 0, -- maintained by loader from evidence rows
  contradict_count int  not null default 0,
  sources          text[] not null default '{}',  -- distinct evidence.source_name values
  updated_at       timestamptz not null default now(),
  unique (type, src, dst)
);
create index if not exists edges_src_idx on edges(src);
create index if not exists edges_dst_idx on edges(dst);
create index if not exists edges_type_idx on edges(type);

-- Evidence: why we believe (or doubt) an edge. Every edge has >= 1 row.
create table if not exists evidence (
  id           text primary key,
  edge_id      text not null references edges(id) on delete cascade,
  stance       text not null check (stance in ('supports','contradicts','context')),
  source_type  text not null check (source_type in (
                 'database','publication','trial_registry','grant_database',
                 'patient_org_site','web','computed')),
  source_name  text not null,               -- 'HPO','Orphanet','ClinVar','PubMed','ClinicalTrials.gov','NIH RePORTER','NORD','Atlas analytics',…
  source_ref   text,                        -- 'PMID:123', 'NCT0123', 'ORPHA:228349', …
  url          text,
  quote        text,                        -- verbatim supporting sentence / record excerpt
  method       text not null,               -- 'curated' | 'llm:<model>' | 'algorithm:<name>' | 'scrape:brightdata'
  published_at date,
  retrieved_at timestamptz not null default now()
);
create index if not exists evidence_edge_idx on evidence(edge_id);

-- -----------------------------------------------------------------------------
-- Clusters (mechanism/phenotype communities found by graph analytics)
-- -----------------------------------------------------------------------------
create table if not exists clusters (
  id         text primary key,              -- 'CL:<slug>'
  label      text not null,
  summary    text,                          -- plain-language description
  method     text not null,                 -- e.g. 'leiden:phenotype+mechanism:v1'
  size       int not null default 0,
  attrs      jsonb not null default '{}',   -- {"top_phenotypes":[ids],"top_mechanisms":[ids],"cohesion":0.7}
  updated_at timestamptz not null default now()
);

create table if not exists cluster_members (
  cluster_id text not null references clusters(id) on delete cascade,
  node_id    text not null references nodes(id) on delete cascade,
  membership real not null default 1,       -- 0..1 strength of membership
  primary key (cluster_id, node_id)
);
create index if not exists cluster_members_node_idx on cluster_members(node_id);

-- -----------------------------------------------------------------------------
-- Precomputed connections (paths) the UI can walk step by step.
-- -----------------------------------------------------------------------------
create table if not exists paths (
  id         text primary key,              -- 'P:<hash>'
  from_id    text not null references nodes(id) on delete cascade,
  to_id      text not null references nodes(id) on delete cascade,
  kind       text not null check (kind in ('related_disease','patient_group','asset','researcher','trial','intervention')),
  title      text not null,                 -- "CLN5 disease → lysosomal protein degradation → CLN6 disease"
  node_ids   text[] not null,               -- ordered, starts with from_id, ends with to_id
  edge_ids   text[] not null,               -- ordered, length = node_ids - 1
  score      real not null,                 -- higher = better route
  attrs      jsonb not null default '{}',
  updated_at timestamptz not null default now()
);
create index if not exists paths_from_idx on paths(from_id);
create index if not exists paths_to_idx on paths(to_id);

-- -----------------------------------------------------------------------------
-- Precomputed composite screens. payload shape is defined in contract/atlas.ts
--   kind 'action'    key = disease id      -> ActionView
--   kind 'mechanism' key = mechanism or intervention id -> MechanismView
-- -----------------------------------------------------------------------------
create table if not exists views (
  kind     text not null check (kind in ('action','mechanism')),
  key      text not null,
  payload  jsonb not null,
  built_at timestamptz not null default now(),
  primary key (kind, key)
);

-- Cached LLM explanations (written by the explain-path edge function or pipeline).
create table if not exists explanations (
  cache_key  text primary key,              -- sha1(audience|edge_ids joined by ',')
  audience   text not null check (audience in ('family','expert')),
  edge_ids   text[] not null,
  payload    jsonb not null,                -- Explanation (contract/atlas.ts)
  model      text,
  created_at timestamptz not null default now()
);

-- Evidence contributed by patient groups (stretch goal). Written only by the
-- contribute-evidence edge function (service role). Never shown as fact.
create table if not exists submissions (
  id         uuid primary key default gen_random_uuid(),
  node_id    text references nodes(id) on delete set null,
  kind       text not null check (kind in ('missing_group','missing_asset','correction','new_evidence','other')),
  url        text,
  note       text not null,
  contact    text,
  status     text not null default 'pending' check (status in ('pending','accepted','rejected')),
  created_at timestamptz not null default now()
);

-- Dataset metadata, e.g. ('dataset', {"version":"2026-10-04a","slice":"Lysosomal storage diseases"}).
create table if not exists dataset_meta (
  key   text primary key,
  value jsonb not null
);

-- -----------------------------------------------------------------------------
-- Row level security: ON for every table with NO policies. Supabase's public
-- anon/authenticated roles can read nothing; only our API servers and the
-- pipeline (connecting as the database owner via DATABASE_URL) can.
-- -----------------------------------------------------------------------------
do $$
declare t text;
begin
  foreach t in array array['nodes','node_names','edges','evidence','clusters','cluster_members',
                           'paths','views','explanations','dataset_meta','submissions'] loop
    execute format('alter table %I enable row level security', t);
  end loop;
end $$;
