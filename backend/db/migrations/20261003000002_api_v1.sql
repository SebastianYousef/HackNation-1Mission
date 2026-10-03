-- =============================================================================
-- Rare Disease Atlas — data-access API v1 (CONTRACT-BOUND)
--
-- The REST API servers (backend/api) map each endpoint 1:1 to one of these
-- functions and return the jsonb unchanged. Output shapes are defined in
-- contract/atlas.ts; changing them is a contract change (see contract/README.md).
-- Every function returns NULL (-> HTTP 404) when the requested id is unknown.
-- =============================================================================

-- ---------- JSON builders (the only place node/edge/evidence shapes are made) ----------

create or replace function _node_brief(n nodes) returns jsonb
language sql immutable as $$
  select jsonb_build_object(
    'id', n.id, 'type', n.type, 'subtype', n.subtype, 'label', n.label,
    'summary', n.plain_summary)
$$;

create or replace function _node_full(n nodes) returns jsonb
language sql immutable as $$
  select _node_brief(n) || jsonb_build_object(
    'description', n.description, 'synonyms', to_jsonb(n.synonyms),
    'xrefs', n.xrefs, 'attrs', n.attrs, 'url', n.url)
$$;

create or replace function _edge_json(e edges) returns jsonb
language sql immutable as $$
  select jsonb_build_object(
    'id', e.id, 'src', e.src, 'dst', e.dst, 'type', e.type, 'label', e.label,
    'status', e.status, 'confidence', e.confidence, 'score', e.score,
    'support_count', e.support_count, 'contradict_count', e.contradict_count,
    'sources', to_jsonb(e.sources), 'attrs', e.attrs)
$$;

create or replace function _evidence_json(v evidence) returns jsonb
language sql immutable as $$
  select jsonb_build_object(
    'id', v.id, 'stance', v.stance, 'source_type', v.source_type,
    'source_name', v.source_name, 'source_ref', v.source_ref, 'url', v.url,
    'quote', v.quote, 'method', v.method, 'published_at', v.published_at,
    'retrieved_at', v.retrieved_at)
$$;

-- Nodes linked to both a and b through edges of type p_type (either direction),
-- most informative first (phenotypes carry attrs.ic = information content).
-- Hypothesis edges never count as "shared" — only curated/literature/inferred.
create or replace function _shared(a text, b text, p_type text, p_limit int)
returns jsonb language sql stable as $$
  with na as (select case when e.src = a then e.dst else e.src end as nid
                from edges e where e.type = p_type and e.status <> 'hypothesis' and (e.src = a or e.dst = a)),
       nb as (select case when e.src = b then e.dst else e.src end as nid
                from edges e where e.type = p_type and e.status <> 'hypothesis' and (e.src = b or e.dst = b)),
       s  as (select nid from na intersect select nid from nb)
  select coalesce(jsonb_agg(_node_brief(x) order by (x.attrs->>'ic')::real desc nulls last, x.label), '[]'::jsonb)
  from (select n.* from s join nodes n on n.id = s.nid
         order by (n.attrs->>'ic')::real desc nulls last, n.label limit p_limit) x
$$;

-- ---------- GET /api/v1/meta ----------
create or replace function api_meta() returns jsonb
language sql stable as $$
  select jsonb_build_object(
    'contract_version', '1.0.0',
    'dataset', coalesce((select value from dataset_meta where key = 'dataset'), '{}'::jsonb),
    'counts', jsonb_build_object(
      'nodes', coalesce((select jsonb_object_agg(type, c) from (select type, count(*) c from nodes group by type) t), '{}'::jsonb),
      'edges', (select count(*) from edges),
      'evidence', (select count(*) from evidence),
      'clusters', (select count(*) from clusters)),
    'sources', coalesce((select jsonb_agg(distinct source_name) from evidence), '[]'::jsonb))
$$;

-- ---------- GET /api/v1/search ----------
create or replace function api_search(p_q text, p_types text[] default null, p_limit int default 20)
returns jsonb language sql stable as $$
  with q as (select lower(trim(coalesce(p_q, ''))) as q),
  m as (
    select nn.node_id, nn.name, nn.kind,
           greatest(
             similarity(lower(nn.name), q.q),
             case when lower(nn.name) = q.q then 1.0
                  when lower(nn.name) like q.q || '%' then 0.9
                  when lower(nn.name) like '%' || q.q || '%' then 0.6
                  else 0 end)::real as s
    from node_names nn, q
    where length(q.q) >= 2
      and (lower(nn.name) % q.q or lower(nn.name) like '%' || q.q || '%')
  ),
  best as (
    select distinct on (m.node_id) m.node_id, m.name, m.kind, m.s
    from m order by m.node_id, m.s desc
  ),
  ranked as (
    select n, b.name, b.kind, b.s
    from best b join nodes n on n.id = b.node_id
    where p_types is null or n.type = any(p_types)
    order by b.s desc, (n.type = 'disease') desc, n.label
    limit least(greatest(coalesce(p_limit, 20), 1), 50)
  )
  select coalesce(jsonb_agg(_node_brief(r.n) || jsonb_build_object(
           'matched_name', r.name, 'match_kind', r.kind, 'score', r.s)
         order by r.s desc, ((r.n).type = 'disease') desc, (r.n).label), '[]'::jsonb)
  from ranked r
$$;

-- ---------- GET /api/v1/nodes/{id} ----------
create or replace function api_node(p_id text) returns jsonb
language sql stable as $$
  select jsonb_build_object(
    'node', _node_full(n),
    'degree', coalesce((
      select jsonb_object_agg(t, c) from (
        select o.type as t, count(*) as c
        from edges e join nodes o on o.id = case when e.src = n.id then e.dst else e.src end
        where e.src = n.id or e.dst = n.id group by o.type) d), '{}'::jsonb),
    'clusters', coalesce((
      select jsonb_agg(jsonb_build_object('id', c.id, 'label', c.label, 'size', c.size,
                                          'membership', cm.membership) order by cm.membership desc)
      from cluster_members cm join clusters c on c.id = cm.cluster_id
      where cm.node_id = n.id), '[]'::jsonb),
    'has_action_view', exists(select 1 from views v where v.kind = 'action' and v.key = n.id),
    'has_mechanism_view', exists(select 1 from views v where v.kind = 'mechanism' and v.key = n.id))
  from nodes n where n.id = p_id
$$;

-- ---------- GET /api/v1/nodes/{id}/neighborhood ----------
-- Breadth-first expansion; at each hop keeps the highest-confidence neighbours
-- until p_max_nodes is reached (truncated = true when something was dropped).
create or replace function api_neighborhood(
  p_id text, p_depth int default 1, p_edge_types text[] default null,
  p_statuses text[] default null, p_min_confidence real default 0, p_max_nodes int default 60)
returns jsonb language plpgsql stable as $$
declare
  v_nodes     text[] := array[p_id];
  v_frontier  text[] := array[p_id];
  v_next      text[];
  v_count     int;
  v_room      int;
  v_truncated boolean := false;
  v_max       int := least(greatest(coalesce(p_max_nodes, 60), 2), 300);
begin
  if not exists (select 1 from nodes where id = p_id) then return null; end if;

  for i in 1..least(greatest(coalesce(p_depth, 1), 1), 3) loop
    with cand as (
      select z.nid, max(z.conf) as mc from (
        select case when e.src = any(v_frontier) then e.dst else e.src end as nid, e.confidence as conf
        from edges e
        where (e.src = any(v_frontier) or e.dst = any(v_frontier))
          and (p_edge_types is null or e.type = any(p_edge_types))
          and (p_statuses   is null or e.status = any(p_statuses))
          and e.confidence >= coalesce(p_min_confidence, 0)) z
      where not (z.nid = any(v_nodes))
      group by z.nid)
    select array_agg(nid order by mc desc, nid), count(*) into v_next, v_count from cand;

    exit when coalesce(v_count, 0) = 0;
    v_room := v_max - cardinality(v_nodes);
    if v_count > v_room then
      v_truncated := true;
      v_next := v_next[1:greatest(v_room, 0)];
    end if;
    exit when cardinality(v_next) is null or cardinality(v_next) = 0;
    v_nodes := v_nodes || v_next;
    v_frontier := v_next;
  end loop;

  return jsonb_build_object(
    'center', p_id,
    'nodes', (select coalesce(jsonb_agg(_node_brief(n) order by n.id = p_id desc, n.type, n.label), '[]'::jsonb)
                from nodes n where n.id = any(v_nodes)),
    'edges', (select coalesce(jsonb_agg(_edge_json(e) order by e.confidence desc, e.id), '[]'::jsonb)
                from edges e
               where e.src = any(v_nodes) and e.dst = any(v_nodes)
                 and (p_edge_types is null or e.type = any(p_edge_types))
                 and (p_statuses   is null or e.status = any(p_statuses))
                 and e.confidence >= coalesce(p_min_confidence, 0)),
    'truncated', v_truncated);
end $$;

-- ---------- GET /api/v1/edges/{id} ----------
create or replace function api_edge(p_id text) returns jsonb
language sql stable as $$
  select jsonb_build_object(
    'edge', _edge_json(e),
    'source', _node_brief(s),
    'target', _node_brief(t),
    'supporting',   coalesce((select jsonb_agg(_evidence_json(v) order by v.published_at desc nulls last, v.id)
                                from evidence v where v.edge_id = e.id and v.stance = 'supports'), '[]'::jsonb),
    'contradicting', coalesce((select jsonb_agg(_evidence_json(v) order by v.published_at desc nulls last, v.id)
                                from evidence v where v.edge_id = e.id and v.stance = 'contradicts'), '[]'::jsonb),
    'context',      coalesce((select jsonb_agg(_evidence_json(v) order by v.published_at desc nulls last, v.id)
                                from evidence v where v.edge_id = e.id and v.stance = 'context'), '[]'::jsonb))
  from edges e join nodes s on s.id = e.src join nodes t on t.id = e.dst
  where e.id = p_id
$$;

-- ---------- GET /api/v1/diseases/{id}/similar ----------
create or replace function api_similar_diseases(p_id text, p_limit int default 10)
returns jsonb language sql stable as $$
  select case when not exists (select 1 from nodes where id = p_id and type = 'disease') then null else (
    with sim as (
      select e.*, case when e.src = p_id then e.dst else e.src end as other
      from edges e
      where e.type = 'disease_similar_to' and (e.src = p_id or e.dst = p_id)
      order by e.score desc nulls last, e.id
      limit least(greatest(coalesce(p_limit, 10), 1), 50))
    select coalesce(jsonb_agg(jsonb_build_object(
             'disease', _node_brief(o),
             'edge_id', s.id,
             'score', s.score,
             'confidence', s.confidence,
             'status', s.status,
             'components', coalesce(s.attrs->'components', '{}'::jsonb),
             'caution', s.attrs->>'caution',
             'shared', jsonb_build_object(
               'phenotypes', _shared(p_id, o.id, 'disease_has_phenotype', 8),
               'mechanisms', _shared(p_id, o.id, 'disease_involves_mechanism', 8),
               'genes',      _shared(p_id, o.id, 'gene_associated_with_disease', 8)),
             'same_cluster', exists(select 1 from cluster_members a join cluster_members b using (cluster_id)
                                     where a.node_id = p_id and b.node_id = o.id))
           order by s.score desc nulls last, s.id), '[]'::jsonb)
    from sim s join nodes o on o.id = s.other) end
$$;

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
             'weakest_status', (select e.status from edges e where e.id = any(p.edge_ids)
                                 order by array_position(array['curated','literature','inferred','hypothesis'], e.status) desc
                                 limit 1),
             'min_confidence', (select min(e.confidence) from edges e where e.id = any(p.edge_ids)),
             'attrs', p.attrs)
           order by p.score desc, p.id), '[]'::jsonb)
    from (select * from paths
           where from_id = p_from
             and (p_to is null or to_id = p_to)
             and (p_kind is null or kind = p_kind)
           order by score desc, id
           limit least(greatest(coalesce(p_limit, 10), 1), 50)) p) end
$$;

-- ---------- GET /api/v1/clusters ----------
create or replace function api_clusters() returns jsonb
language sql stable as $$
  select coalesce(jsonb_agg(jsonb_build_object(
           'id', c.id, 'label', c.label, 'summary', c.summary, 'method', c.method,
           'size', c.size, 'attrs', c.attrs) order by c.size desc, c.id), '[]'::jsonb)
  from clusters c
$$;

-- ---------- GET /api/v1/clusters/{id} ----------
create or replace function api_cluster(p_id text) returns jsonb
language sql stable as $$
  select jsonb_build_object(
    'cluster', jsonb_build_object('id', c.id, 'label', c.label, 'summary', c.summary,
                                  'method', c.method, 'size', c.size, 'attrs', c.attrs),
    'members', coalesce((select jsonb_agg(_node_brief(n) || jsonb_build_object('membership', cm.membership)
                                          order by cm.membership desc, n.label)
                           from cluster_members cm join nodes n on n.id = cm.node_id
                          where cm.cluster_id = c.id), '[]'::jsonb),
    'top_phenotypes', coalesce((select jsonb_agg(_node_brief(n) order by array_position(
                                   array(select jsonb_array_elements_text(c.attrs->'top_phenotypes')), n.id))
                                  from nodes n
                                 where n.id in (select jsonb_array_elements_text(coalesce(c.attrs->'top_phenotypes', '[]'::jsonb)))), '[]'::jsonb),
    'top_mechanisms', coalesce((select jsonb_agg(_node_brief(n) order by array_position(
                                   array(select jsonb_array_elements_text(c.attrs->'top_mechanisms')), n.id))
                                  from nodes n
                                 where n.id in (select jsonb_array_elements_text(coalesce(c.attrs->'top_mechanisms', '[]'::jsonb)))), '[]'::jsonb))
  from clusters c where c.id = p_id
$$;

-- ---------- GET /api/v1/diseases/{id}/action-view  and  /api/v1/mechanisms/{id}/view ----------
create or replace function api_action_view(p_id text) returns jsonb
language sql stable as $$
  select payload from views where kind = 'action' and key = p_id
$$;

create or replace function api_mechanism_view(p_id text) returns jsonb
language sql stable as $$
  select payload from views where kind = 'mechanism' and key = p_id
$$;
