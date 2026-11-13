-- Record discovery relationships atomically in bounded batches.
--
-- The old Python path performed a SELECT before every INSERT. This function
-- lets PostgreSQL resolve both existing unique indexes with ON CONFLICT DO
-- NOTHING and returns one row per requested relationship, whether it was newly
-- inserted or already present. Existing provenance is never updated.
create or replace function public.record_discovery_edges(p_edges jsonb)
returns jsonb
language plpgsql
security invoker
set search_path = ''
as $$
declare
    v_input_count integer;
    v_edge jsonb;
    v_parent_discovery_id uuid;
    v_child_discovery_id uuid;
    v_child_normalized_url text;
    v_discovered_from_url text;
    v_depth integer;
    v_row public.discovery_edges%rowtype;
    v_edges jsonb := '[]'::jsonb;
    v_inserted_count integer := 0;
begin
    if p_edges is null or pg_catalog.jsonb_typeof(p_edges) <> 'array' then
        raise exception 'record_discovery_edges requires a JSON array';
    end if;

    v_input_count := pg_catalog.jsonb_array_length(p_edges);
    if v_input_count > 200 then
        raise exception 'record_discovery_edges accepts at most 200 edges per call';
    end if;

    for v_edge in
        select value from pg_catalog.jsonb_array_elements(p_edges)
    loop
        if pg_catalog.jsonb_typeof(v_edge) <> 'object' then
            raise exception 'record_discovery_edges entries must be JSON objects';
        end if;

        v_child_normalized_url := nullif(v_edge ->> 'child_normalized_url', '');
        v_discovered_from_url := nullif(v_edge ->> 'discovered_from_url', '');
        if v_child_normalized_url is null then
            raise exception 'record_discovery_edges requires child_normalized_url';
        end if;
        if v_discovered_from_url is null then
            raise exception 'record_discovery_edges requires discovered_from_url';
        end if;

        v_parent_discovery_id := nullif(v_edge ->> 'parent_discovery_id', '')::uuid;
        v_child_discovery_id := nullif(v_edge ->> 'child_discovery_id', '')::uuid;
        v_depth := coalesce(nullif(v_edge ->> 'depth', '')::integer, 0);

        -- ON CONFLICT without a target covers both partial unique indexes:
        -- uq_discovery_edges_parent_child and uq_discovery_edges_root_child.
        insert into public.discovery_edges (
            parent_discovery_id,
            child_discovery_id,
            child_normalized_url,
            discovered_from_url,
            depth,
            original_url,
            link_text,
            source_domain,
            destination_domain,
            admission_score,
            admission_reason,
            discovery_mechanism
        )
        values (
            v_parent_discovery_id,
            v_child_discovery_id,
            v_child_normalized_url,
            v_discovered_from_url,
            v_depth,
            nullif(v_edge ->> 'original_url', ''),
            nullif(v_edge ->> 'link_text', ''),
            nullif(v_edge ->> 'source_domain', ''),
            nullif(v_edge ->> 'destination_domain', ''),
            nullif(v_edge ->> 'admission_score', '')::smallint,
            nullif(v_edge ->> 'admission_reason', ''),
            nullif(v_edge ->> 'discovery_mechanism', '')
        )
        on conflict do nothing
        returning * into v_row;

        if found then
            v_inserted_count := v_inserted_count + 1;
        else
            -- Return the relationship that caused the conflict so callers keep
            -- their existing row/ID contract. A new statement sees any row that
            -- a concurrent transaction committed while our insert waited.
            select *
            into v_row
            from public.discovery_edges as e
            where e.child_normalized_url = v_child_normalized_url
              and (
                  (v_parent_discovery_id is null and e.parent_discovery_id is null)
                  or e.parent_discovery_id = v_parent_discovery_id
              )
            order by e.discovered_at, e.id
            limit 1;

            if not found then
                raise exception
                    'record_discovery_edges could not resolve a conflicting relationship';
            end if;
        end if;

        v_edges := v_edges || pg_catalog.jsonb_build_array(pg_catalog.to_jsonb(v_row));
    end loop;

    return pg_catalog.jsonb_build_object(
        'inserted_count', v_inserted_count,
        'edges', v_edges
    );
end;
$$;

revoke all on function public.record_discovery_edges(jsonb) from public, anon, authenticated;
grant execute on function public.record_discovery_edges(jsonb) to service_role;
grant select, insert on table public.discovery_edges to service_role;
