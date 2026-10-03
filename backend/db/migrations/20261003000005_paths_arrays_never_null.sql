-- =============================================================================
-- Follow-up to 20261003000004_path_strength.sql (that file is applied; not edited).
-- api_paths keeps the one path-strength rule from 0004: it serves the stored
-- paths.weakest_status / min_confidence (computed by analytics/paths.py
-- path_strength, honesty caps included). Two gaps closed here:
--   * 'nodes' / 'edges' are never null (contract: arrays are never null). A path
--     whose ids no longer resolve gives [] instead of null.
--   * rows loaded before 0004 (NULL weakest_status / min_confidence) fall back to
--     the same rule as path_strength: the weakest edge, lowered (never raised) by
--     attrs.status_cap / attrs.confidence_cap. An unknown status_cap or a
--     non-numeric confidence_cap is ignored.
-- No contract type change. Idempotent: safe to re-apply (make db-migrate runs every file).
-- =============================================================================

-- ---------- GET /api/v1/paths ----------
create or replace function api_paths(p_from text, p_to text default null, p_kind text default null, p_limit int default 10)
returns jsonb language sql stable as $$
  select case when not exists (select 1 from nodes where id = p_from) then null else (
    select coalesce(jsonb_agg(jsonb_build_object(
             'id', p.id, 'kind', p.kind, 'title', p.title, 'score', p.score,
             'from', p.from_id, 'to', p.to_id,
             'node_ids', to_jsonb(p.node_ids), 'edge_ids', to_jsonb(p.edge_ids),
             'nodes', coalesce((select jsonb_agg(_node_brief(n) order by array_position(p.node_ids, n.id))
                                  from nodes n where n.id = any(p.node_ids)), '[]'::jsonb),
             'edges', coalesce((select jsonb_agg(_edge_json(e) order by array_position(p.edge_ids, e.id))
                                  from edges e where e.id = any(p.edge_ids)), '[]'::jsonb),
             'weakest_status', coalesce(p.weakest_status, s.weakest_status),
             'min_confidence', coalesce(p.min_confidence, s.min_confidence),
             'attrs', p.attrs)
           order by p.score desc, p.id), '[]'::jsonb)
    from (select * from paths
           where from_id = p_from
             and (p_to is null or to_id = p_to)
             and (p_kind is null or kind = p_kind)
           order by score desc, id
           limit least(greatest(coalesce(p_limit, 10), 1), 50)) p
    cross join lateral (
      -- fallback for pre-0004 rows only; greatest / least skip nulls, so no cap = edge minimum
      select r.ranks[greatest(
               (select max(array_position(r.ranks, e.status)) from edges e where e.id = any(p.edge_ids)),
               array_position(r.ranks, p.attrs->>'status_cap'))] as weakest_status,
             least(
               (select min(e.confidence) from edges e where e.id = any(p.edge_ids)),
               case when jsonb_typeof(p.attrs->'confidence_cap') = 'number'
                    then (p.attrs->>'confidence_cap')::real end) as min_confidence
        from (select array['curated','literature','inferred','hypothesis'] as ranks) r
    ) s) end
$$;
