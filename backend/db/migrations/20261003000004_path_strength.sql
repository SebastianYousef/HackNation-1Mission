-- =============================================================================
-- One rule for a path's strength, computed once in the pipeline.
-- analytics/paths.py (path_strength) takes the weakest edge status and the lowest
-- edge confidence, lowered by the path's honesty caps (attrs.status_cap /
-- attrs.confidence_cap), and the loader stores them here. GET /paths now serves
-- the stored values, so it matches the action/mechanism views' connections.
-- Rows loaded before this migration have NULLs and fall back to the edge minimum.
-- Idempotent: safe to re-apply (make db-migrate runs every file).
-- =============================================================================

alter table paths add column if not exists weakest_status text
  check (weakest_status in ('curated','literature','inferred','hypothesis'));
alter table paths add column if not exists min_confidence real;

-- ---------- GET /api/v1/paths ----------
create or replace function api_paths(p_from text, p_to text default null, p_kind text default null, p_limit int default 10)
returns jsonb language sql stable as $$
  select case when not exists (select 1 from nodes where id = p_from) then null else (
    select coalesce(jsonb_agg(jsonb_build_object(
             'id', p.id, 'kind', p.kind, 'title', p.title, 'score', p.score,
             'from', p.from_id, 'to', p.to_id,
             'node_ids', to_jsonb(p.node_ids), 'edge_ids', to_jsonb(p.edge_ids),
             'nodes', (select jsonb_agg(_node_brief(n) order by array_position(p.node_ids, n.id))
                         from nodes n where n.id = any(p.node_ids)),
             'edges', (select jsonb_agg(_edge_json(e) order by array_position(p.edge_ids, e.id))
                         from edges e where e.id = any(p.edge_ids)),
             'weakest_status', coalesce(p.weakest_status,
                                (select e.status from edges e where e.id = any(p.edge_ids)
                                  order by array_position(array['curated','literature','inferred','hypothesis'], e.status) desc
                                  limit 1)),
             'min_confidence', coalesce(p.min_confidence,
                                (select min(e.confidence) from edges e where e.id = any(p.edge_ids))),
             'attrs', p.attrs)
           order by p.score desc, p.id), '[]'::jsonb)
    from (select * from paths
           where from_id = p_from
             and (p_to is null or to_id = p_to)
             and (p_kind is null or kind = p_kind)
           order by score desc, id
           limit least(greatest(coalesce(p_limit, 10), 1), 50)) p) end
$$;
