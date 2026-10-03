-- =============================================================================
-- 1. Submissions survive dataset reloads.
--    submissions.node_id referenced nodes(id), so the loader's TRUNCATE ... CASCADE
--    emptied submissions on every load (TRUNCATE ignores ON DELETE SET NULL), and a
--    DELETE-based load would null every link. node_id is now a plain CURIE: ids are
--    stable across loads and the API checks the node exists before inserting.
-- 2. api_search: LIKE metacharacters in q are escaped ('%%' / '__' matched every
--    name), and ranking is match quality first (exact > prefix > substring > fuzzy),
--    then node type (disease > gene > organization > mechanism > intervention >
--    other > publication/trial/grant/person). score stays in 0..1 and follows the
--    returned order.
-- Idempotent: safe to re-apply (make db-migrate runs every file).
-- =============================================================================

alter table submissions drop constraint if exists submissions_node_id_fkey;
create index if not exists submissions_node_idx on submissions(node_id);

-- ---------- GET /api/v1/search ----------
-- score: exact 1.0, prefix 0.9, substring 0.7, fuzzy 0.6 × trigram similarity, each minus
-- 0.01 per type rank (max 0.06), so tiers never overlap.
create or replace function api_search(p_q text, p_types text[] default null, p_limit int default 20)
returns jsonb language sql stable as $$
  with q as (
    select x.q, replace(replace(replace(x.q, '\', '\\'), '%', '\%'), '_', '\_') as pat
    from (select lower(trim(coalesce(p_q, ''))) as q) x
  ),
  m as (
    select nn.node_id, nn.name, nn.kind,
           case when lower(nn.name) = q.q then 3
                when lower(nn.name) like q.pat || '%' escape '\' then 2
                when lower(nn.name) like '%' || q.pat || '%' escape '\' then 1
                else 0 end as tier,
           similarity(lower(nn.name), q.q) as sim
    from node_names nn, q
    where length(q.q) >= 2
      and (lower(nn.name) % q.q or lower(nn.name) like '%' || q.pat || '%' escape '\')
  ),
  best as (
    select distinct on (m.node_id) m.node_id, m.name, m.kind, m.tier, m.sim
    from m order by m.node_id, m.tier desc, m.sim desc, length(m.name), m.name
  ),
  ranked as (
    select n, b.name, b.kind, b.sim,
           round(greatest(0, (case b.tier when 3 then 1.0 when 2 then 0.9 when 1 then 0.7
                                           else 0.6 * b.sim end)
                 - 0.01 * case n.type when 'disease' then 0 when 'gene' then 1 when 'organization' then 2
                                      when 'mechanism' then 3 when 'intervention' then 4
                                      when 'publication' then 6 when 'trial' then 6 when 'grant' then 6
                                      when 'person' then 6 else 5 end)::numeric, 3)::float8 as s
    from best b join nodes n on n.id = b.node_id
    where p_types is null or n.type = any(p_types)
    order by s desc, b.sim desc, n.label
    limit least(greatest(coalesce(p_limit, 20), 1), 50)
  )
  select coalesce(jsonb_agg(_node_brief(r.n) || jsonb_build_object(
           'matched_name', r.name, 'match_kind', r.kind, 'score', r.s)
         order by r.s desc, r.sim desc, (r.n).label), '[]'::jsonb)
  from ranked r
$$;
