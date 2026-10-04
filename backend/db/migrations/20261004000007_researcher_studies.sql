-- =============================================================================
-- Researcher-published studies (contract v1.1.0, additive).
-- Research teams publish a study through One Mission without an account. The API
-- returns a private edit token once and stores only its sha256. Studies are shown
-- with verification "researcher_submitted" until a registry or a person checks them.
-- counts holds anonymous totals only (views, screenings, potential matches, contact
-- clicks): no answers, no personal data. Applications never reach this database.
-- Idempotent: safe to re-apply (make db-migrate runs every file).
-- =============================================================================

create table if not exists researcher_studies (
  id          text primary key,
  token_hash  text not null,
  data        jsonb not null,
  published   boolean not null default true,
  history     jsonb not null default '[]'::jsonb,
  counts      jsonb not null default '{}'::jsonb,
  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now()
);
create index if not exists researcher_studies_conditions_idx on researcher_studies using gin ((data->'condition_ids'));
create index if not exists researcher_studies_published_idx on researcher_studies (published, updated_at desc);

-- ---------- GET /api/v1/meta: contract 1.1.0 (researcher studies) ----------
create or replace function api_meta() returns jsonb
language sql stable as $$
  select jsonb_build_object(
    'contract_version', '1.1.0',
    'dataset', coalesce((select value from dataset_meta where key = 'dataset'), '{}'::jsonb),
    'counts', jsonb_build_object(
      'nodes', coalesce((select jsonb_object_agg(type, c) from (select type, count(*) c from nodes group by type) t), '{}'::jsonb),
      'edges', (select count(*) from edges),
      'evidence', (select count(*) from evidence),
      'clusters', (select count(*) from clusters)),
    'sources', coalesce((select jsonb_agg(distinct source_name) from evidence), '[]'::jsonb))
$$;
