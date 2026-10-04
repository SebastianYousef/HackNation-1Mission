-- =============================================================================
-- Follow-up to 20261003000002_api_v1.sql (that file is applied; not edited).
-- The JSON builders _node_brief / _node_full / _edge_json / _evidence_json were
-- declared IMMUTABLE, but they call jsonb_build_object and to_jsonb, which are
-- STABLE, and _evidence_json's output depended on the session TimeZone:
-- evidence.retrieved_at (timestamptz) was rendered in the server's zone, so the
-- same row read "...T22:06:01+02:00" or "...T20:06:01+00:00" depending on where
-- the server ran.
--   * All four are re-declared STABLE. _node_brief / _node_full / _edge_json keep
--     their bodies unchanged.
--   * _evidence_json renders retrieved_at in UTC whatever the session TimeZone.
--     The text is exactly what to_jsonb(timestamptz) gives under TimeZone = UTC
--     ("YYYY-MM-DDTHH:MI:SS[.ffffff]+00:00"; 'infinity' stays as it was), so the
--     API output is byte-identical to before for a UTC server.
-- No contract type change. Idempotent: safe to re-apply (make db-migrate runs every file).
-- =============================================================================

create or replace function _node_brief(n nodes) returns jsonb
language sql stable as $$
  select jsonb_build_object(
    'id', n.id, 'type', n.type, 'subtype', n.subtype, 'label', n.label,
    'summary', n.plain_summary)
$$;

create or replace function _node_full(n nodes) returns jsonb
language sql stable as $$
  select _node_brief(n) || jsonb_build_object(
    'description', n.description, 'synonyms', to_jsonb(n.synonyms),
    'xrefs', n.xrefs, 'attrs', n.attrs, 'url', n.url)
$$;

create or replace function _edge_json(e edges) returns jsonb
language sql stable as $$
  select jsonb_build_object(
    'id', e.id, 'src', e.src, 'dst', e.dst, 'type', e.type, 'label', e.label,
    'status', e.status, 'confidence', e.confidence, 'score', e.score,
    'support_count', e.support_count, 'contradict_count', e.contradict_count,
    'sources', to_jsonb(e.sources), 'attrs', e.attrs)
$$;

create or replace function _evidence_json(v evidence) returns jsonb
language sql stable as $$
  select jsonb_build_object(
    'id', v.id, 'stance', v.stance, 'source_type', v.source_type,
    'source_name', v.source_name, 'source_ref', v.source_ref, 'url', v.url,
    'quote', v.quote, 'method', v.method, 'published_at', v.published_at,
    'retrieved_at', case when isfinite(v.retrieved_at)
                         then to_jsonb((to_jsonb(v.retrieved_at at time zone 'UTC') #>> '{}') || '+00:00')
                         else to_jsonb(v.retrieved_at) end)
$$;
