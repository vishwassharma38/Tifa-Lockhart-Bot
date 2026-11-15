-- Atomically requeue every still-owned claim from an interrupted crawl batch.
-- Additive only: this function updates only rows that are still processing and
-- whose claimed_at value matches the caller's acquired claim. Completed,
-- terminalized, or reassigned discoveries are left untouched.
--
-- Rollback: drop function if exists public.requeue_crawl_claims(jsonb, text, timestamptz);

create or replace function public.requeue_crawl_claims(
    p_claims jsonb,
    p_reason text,
    p_next_attempt_at timestamptz default null
)
returns integer
language plpgsql
security definer
set search_path = ''
as $$
declare
    requeued_count integer;
begin
    if p_claims is null or pg_catalog.jsonb_typeof(p_claims) <> 'array' then
        raise exception 'p_claims must be a JSON array';
    end if;

    with claims as (
        select *
        from pg_catalog.jsonb_to_recordset(p_claims)
            as x(id uuid, claimed_at timestamptz)
        limit 300
    ),
    requeued as (
        update public.discoveries d
        set status = 'queued',
            claimed_at = null,
            last_error = pg_catalog.left(coalesce(p_reason, 'crawl_batch_interrupted'), 500),
            next_attempt_at = p_next_attempt_at
        from claims c
        where d.id = c.id
          and d.status = 'processing'
          and d.claimed_at is not distinct from c.claimed_at
        returning d.id
    )
    select pg_catalog.count(*) into requeued_count from requeued;

    return requeued_count;
end;
$$;

revoke all on function public.requeue_crawl_claims(jsonb, text, timestamptz)
    from public, anon, authenticated;
grant execute on function public.requeue_crawl_claims(jsonb, text, timestamptz)
    to service_role;
