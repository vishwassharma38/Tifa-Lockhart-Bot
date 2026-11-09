-- Tifa Lockhart Bot - fenced digest claims, fair retry scheduling, and cycle rollover.
-- Additive and forward-only: publication history and migration 008 are preserved.

alter table public.publications
    add column if not exists delivery_claim_token uuid,
    add column if not exists delivery_claim_expires_at timestamptz,
    add column if not exists next_attempt_at timestamptz,
    add column if not exists retry_schedule_count integer not null default 0;

create index if not exists idx_publications_digest_retry_due
    on public.publications (purpose, next_attempt_at, created_at, id)
    where purpose = 'daily_digest' and status in ('pending', 'failed');

create or replace function public.guard_daily_digest_claimed_updates()
returns trigger
language plpgsql
set search_path = ''
as $$
declare
    v_claim_token text;
    v_now timestamptz := pg_catalog.clock_timestamp();
begin
    if old.purpose <> 'daily_digest' then
        return new;
    end if;

    v_claim_token := nullif(pg_catalog.current_setting('tifa.digest_claim_token', true), '');
    if v_claim_token is null then
        raise exception 'daily digest publication update requires a valid claim';
    end if;

    -- The active owner may update or release its row. A new token may take over
    -- only when the old lease is absent or expired.
    if old.delivery_claim_token::text = v_claim_token
       and old.delivery_claim_expires_at > v_now then
        return new;
    end if;

    if new.delivery_claim_token::text = v_claim_token
       and (old.delivery_claim_token is null or old.delivery_claim_expires_at <= v_now) then
        return new;
    end if;

    raise exception 'daily digest publication claim is stale or not owned';
end;
$$;

drop trigger if exists trg_guard_daily_digest_claimed_updates on public.publications;
create trigger trg_guard_daily_digest_claimed_updates
    before update of status, attempt_count, message_id, published_at, last_error,
                     delivery_claim_token, delivery_claim_expires_at,
                     next_attempt_at, retry_schedule_count
    on public.publications
    for each row
    execute function public.guard_daily_digest_claimed_updates();

create or replace function public.claim_daily_digest_publication(
    p_publication_id uuid,
    p_claim_token uuid,
    p_lease_seconds integer default 900
)
returns jsonb
language plpgsql
security invoker
set search_path = ''
as $$
declare
    v_publication public.publications%rowtype;
    v_now timestamptz := pg_catalog.clock_timestamp();
begin
    perform pg_catalog.set_config('tifa.digest_claim_token', p_claim_token::text, true);
    update public.publications
       set delivery_claim_token = p_claim_token,
           delivery_claim_expires_at = v_now + pg_catalog.make_interval(
               secs => greatest(30, least(3600, p_lease_seconds))
           )
     where id = p_publication_id
       and purpose = 'daily_digest'
       and status in ('pending', 'failed')
       and (delivery_claim_token is null or delivery_claim_expires_at <= v_now)
       and (next_attempt_at is null or next_attempt_at <= v_now)
    returning * into v_publication;

    if not found then
        return pg_catalog.jsonb_build_object('claim_acquired', false);
    end if;
    return pg_catalog.to_jsonb(v_publication)
        || pg_catalog.jsonb_build_object('claim_acquired', true);
end;
$$;

create or replace function public.renew_daily_digest_claim(
    p_publication_id uuid,
    p_claim_token uuid,
    p_lease_seconds integer default 900
)
returns boolean
language plpgsql
security invoker
set search_path = ''
as $$
declare
    v_now timestamptz := pg_catalog.clock_timestamp();
begin
    perform pg_catalog.set_config('tifa.digest_claim_token', p_claim_token::text, true);
    update public.publications
       set delivery_claim_expires_at = v_now + pg_catalog.make_interval(
           secs => greatest(30, least(3600, p_lease_seconds))
       )
     where id = p_publication_id
       and purpose = 'daily_digest'
       and delivery_claim_token = p_claim_token
       and delivery_claim_expires_at > v_now;
    return found;
end;
$$;

create or replace function public.advance_daily_digest_attempt(
    p_publication_id uuid,
    p_claim_token uuid,
    p_expected_attempt_count integer,
    p_attempt_limit integer default 3,
    p_lease_seconds integer default 900
)
returns jsonb
language plpgsql
security invoker
set search_path = ''
as $$
declare
    v_publication public.publications%rowtype;
    v_now timestamptz := pg_catalog.clock_timestamp();
begin
    perform pg_catalog.set_config('tifa.digest_claim_token', p_claim_token::text, true);
    update public.publications
       set status = 'pending',
           attempt_count = attempt_count + 1,
           last_error = null,
           next_attempt_at = null,
           delivery_claim_expires_at = v_now + pg_catalog.make_interval(
               secs => greatest(30, least(3600, p_lease_seconds))
           )
     where id = p_publication_id
       and purpose = 'daily_digest'
       and delivery_claim_token = p_claim_token
       and delivery_claim_expires_at > v_now
       and attempt_count = greatest(0, p_expected_attempt_count)
       and attempt_count < greatest(1, p_attempt_limit)
    returning * into v_publication;

    if not found then
        return pg_catalog.jsonb_build_object('attempt_advanced', false);
    end if;
    return pg_catalog.to_jsonb(v_publication)
        || pg_catalog.jsonb_build_object('attempt_advanced', true);
end;
$$;

create or replace function public.finish_daily_digest_failure(
    p_publication_id uuid,
    p_claim_token uuid,
    p_error text
)
returns boolean
language plpgsql
security invoker
set search_path = ''
as $$
declare
    v_now timestamptz := pg_catalog.clock_timestamp();
    v_ambiguous boolean := p_error like 'discord_delivery_ambiguous:%'
        or p_error like 'discord_delivery_failed%';
begin
    perform pg_catalog.set_config('tifa.digest_claim_token', p_claim_token::text, true);
    update public.publications
       set status = 'failed',
           last_error = pg_catalog.left(p_error, 500),
           digest_slot_reserved = case
               when v_ambiguous then digest_slot_reserved else false end,
           retry_schedule_count = retry_schedule_count + 1,
           next_attempt_at = v_now + pg_catalog.make_interval(
               secs => least(604800, (900 * pg_catalog.power(2,
                   least(retry_schedule_count, 9)))::integer)
           ),
           delivery_claim_token = null,
           delivery_claim_expires_at = null
     where id = p_publication_id
       and purpose = 'daily_digest'
       and delivery_claim_token = p_claim_token
       and delivery_claim_expires_at > v_now;
    return found;
end;
$$;

create or replace function public.finish_daily_digest_reconciliation(
    p_publication_id uuid,
    p_claim_token uuid,
    p_error text
)
returns boolean
language plpgsql
security invoker
set search_path = ''
as $$
declare
    v_now timestamptz := pg_catalog.clock_timestamp();
begin
    perform pg_catalog.set_config('tifa.digest_claim_token', p_claim_token::text, true);
    update public.publications
       set last_error = pg_catalog.left(
               case
                   when last_error like 'discord_delivery_ambiguous:%'
                     or last_error like 'discord_delivery_failed%'
                   then last_error || ';' || p_error
                   else p_error
               end,
               500
           ),
           retry_schedule_count = retry_schedule_count + 1,
           next_attempt_at = v_now + pg_catalog.make_interval(
               secs => least(604800, (900 * pg_catalog.power(2,
                   least(retry_schedule_count, 9)))::integer)
           ),
           delivery_claim_token = null,
           delivery_claim_expires_at = null
     where id = p_publication_id
       and purpose = 'daily_digest'
       and delivery_claim_token = p_claim_token
       and delivery_claim_expires_at > v_now;
    return found;
end;
$$;

create or replace function public.publish_daily_digest_publication(
    p_publication_id uuid,
    p_claim_token uuid,
    p_message_id bigint
)
returns boolean
language plpgsql
security invoker
set search_path = ''
as $$
declare
    v_now timestamptz := pg_catalog.clock_timestamp();
begin
    perform pg_catalog.set_config('tifa.digest_claim_token', p_claim_token::text, true);
    update public.publications
       set status = 'published',
           message_id = p_message_id,
           published_at = v_now,
           last_error = null,
           next_attempt_at = null,
           delivery_claim_token = null,
           delivery_claim_expires_at = null
     where id = p_publication_id
       and purpose = 'daily_digest'
       and delivery_claim_token = p_claim_token
       and delivery_claim_expires_at > v_now;
    return found;
end;
$$;

create or replace function public.rollover_reconciled_digest_publication(
    p_publication_id uuid,
    p_claim_token uuid,
    p_digest_cycle date,
    p_limit integer default 50
)
returns jsonb
language plpgsql
security invoker
set search_path = ''
as $$
declare
    v_publication public.publications%rowtype;
    v_reserved_count integer;
    v_now timestamptz := pg_catalog.clock_timestamp();
begin
    perform 1 from public.digest_state where id = 1 for update;
    perform pg_catalog.set_config('tifa.digest_claim_token', p_claim_token::text, true);
    select * into v_publication
      from public.publications
     where id = p_publication_id
       and purpose = 'daily_digest'
       and delivery_claim_token = p_claim_token
       and delivery_claim_expires_at > v_now
     for update;
    if not found then
        return pg_catalog.jsonb_build_object(
            'claim_valid', false, 'quota_exhausted', false
        );
    end if;

    select count(*) into v_reserved_count
      from public.publications
     where purpose = 'daily_digest'
       and digest_cycle = p_digest_cycle
       and digest_slot_reserved
       and id <> p_publication_id;

    if (v_publication.digest_cycle is distinct from p_digest_cycle
        or not v_publication.digest_slot_reserved)
       and v_reserved_count >= greatest(0, p_limit) then
        update public.publications
           set status = 'failed',
               last_error = 'delivery_reconciled_no_marker',
               digest_slot_reserved = false,
               next_attempt_at = v_now,
               retry_schedule_count = retry_schedule_count + 1,
               delivery_claim_token = null,
               delivery_claim_expires_at = null
         where id = p_publication_id;
        return pg_catalog.jsonb_build_object(
            'claim_valid', true, 'quota_exhausted', true
        );
    end if;

    update public.publications
       set status = 'failed',
           last_error = 'delivery_reconciled_no_marker',
           digest_cycle = p_digest_cycle,
           digest_slot_reserved = true,
           next_attempt_at = null,
           retry_schedule_count = 0
     where id = p_publication_id
    returning * into v_publication;
    return pg_catalog.to_jsonb(v_publication)
        || pg_catalog.jsonb_build_object(
            'claim_valid', true, 'quota_exhausted', false
        );
end;
$$;

create or replace function public.reserve_daily_digest_publication(
    p_media_item_id uuid,
    p_channel_id bigint,
    p_digest_cycle date,
    p_limit integer default 50
)
returns jsonb
language plpgsql
security invoker
set search_path = ''
as $$
declare
    v_publication public.publications%rowtype;
    v_reserved_count integer;
    v_existing boolean := false;
    v_now timestamptz := pg_catalog.clock_timestamp();
begin
    perform 1 from public.digest_state where id = 1 for update;

    select * into v_publication
      from public.publications
     where media_item_id = p_media_item_id
       and purpose = 'daily_digest'
     order by (status = 'published') desc, created_at, id
     limit 1
     for update;
    v_existing := found;

    if v_existing then
        if v_publication.status = 'published' then
            return pg_catalog.to_jsonb(v_publication)
                || pg_catalog.jsonb_build_object('quota_exhausted', false);
        end if;

        -- An active worker or an attempted uncertain delivery keeps its current
        -- ownership and reconciliation context. It is never treated as fresh.
        if (v_publication.delivery_claim_token is not null
            and v_publication.delivery_claim_expires_at > v_now)
           or (v_publication.attempt_count > 0
               and (v_publication.status = 'pending'
                    or v_publication.last_error like 'discord_delivery_ambiguous:%'
                    or v_publication.last_error like 'discord_delivery_failed%')) then
            return pg_catalog.to_jsonb(v_publication)
                || pg_catalog.jsonb_build_object(
                    'quota_exhausted', false, 'requires_reconciliation', true
                );
        end if;

        if v_publication.digest_cycle = p_digest_cycle
           and v_publication.digest_slot_reserved then
            return pg_catalog.to_jsonb(v_publication)
                || pg_catalog.jsonb_build_object('quota_exhausted', false);
        end if;

        if v_publication.next_attempt_at is not null
           and v_publication.next_attempt_at > v_now then
            return pg_catalog.to_jsonb(v_publication)
                || pg_catalog.jsonb_build_object('quota_exhausted', false);
        end if;
    end if;

    select count(*) into v_reserved_count
      from public.publications
     where purpose = 'daily_digest'
       and digest_cycle = p_digest_cycle
       and digest_slot_reserved;

    if v_reserved_count >= greatest(0, p_limit) then
        return pg_catalog.jsonb_build_object('quota_exhausted', true);
    end if;

    if v_existing then
        update public.publications
           set digest_cycle = p_digest_cycle,
               digest_slot_reserved = true
         where id = v_publication.id
         returning * into v_publication;
    else
        insert into public.publications (
            media_item_id, channel_id, purpose, status, attempt_count,
            digest_cycle, digest_slot_reserved
        ) values (
            p_media_item_id, p_channel_id, 'daily_digest', 'pending', 0,
            p_digest_cycle, true
        ) returning * into v_publication;
    end if;

    return pg_catalog.to_jsonb(v_publication)
        || pg_catalog.jsonb_build_object('quota_exhausted', false);
exception when unique_violation then
    select * into v_publication
      from public.publications
     where media_item_id = p_media_item_id
       and purpose = 'daily_digest'
     order by (status = 'published') desc, created_at, id
     limit 1;
    if found then
        return pg_catalog.to_jsonb(v_publication)
            || pg_catalog.jsonb_build_object('quota_exhausted', false);
    end if;
    raise;
end;
$$;

revoke all on function public.claim_daily_digest_publication(uuid, uuid, integer)
    from public, anon, authenticated;
revoke all on function public.renew_daily_digest_claim(uuid, uuid, integer)
    from public, anon, authenticated;
revoke all on function public.advance_daily_digest_attempt(uuid, uuid, integer, integer, integer)
    from public, anon, authenticated;
revoke all on function public.finish_daily_digest_failure(uuid, uuid, text)
    from public, anon, authenticated;
revoke all on function public.finish_daily_digest_reconciliation(uuid, uuid, text)
    from public, anon, authenticated;
revoke all on function public.publish_daily_digest_publication(uuid, uuid, bigint)
    from public, anon, authenticated;
revoke all on function public.rollover_reconciled_digest_publication(uuid, uuid, date, integer)
    from public, anon, authenticated;
revoke all on function public.reserve_daily_digest_publication(uuid, bigint, date, integer)
    from public, anon, authenticated;

grant execute on function public.claim_daily_digest_publication(uuid, uuid, integer) to service_role;
grant execute on function public.renew_daily_digest_claim(uuid, uuid, integer) to service_role;
grant execute on function public.advance_daily_digest_attempt(uuid, uuid, integer, integer, integer)
    to service_role;
grant execute on function public.finish_daily_digest_failure(uuid, uuid, text) to service_role;
grant execute on function public.finish_daily_digest_reconciliation(uuid, uuid, text)
    to service_role;
grant execute on function public.publish_daily_digest_publication(uuid, uuid, bigint)
    to service_role;
grant execute on function public.rollover_reconciled_digest_publication(uuid, uuid, date, integer)
    to service_role;
grant execute on function public.reserve_daily_digest_publication(uuid, bigint, date, integer)
    to service_role;

notify pgrst, 'reload schema';
