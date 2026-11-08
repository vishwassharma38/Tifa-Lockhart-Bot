-- Digest terminal exhaustion, cycle-safe claim rollover, and retry claim filtering.
-- Forward-only because migration 009 may already exist in shared environments.

alter table public.publications
    add column if not exists digest_terminal boolean not null default false;

create index if not exists idx_publications_digest_retry_claimable
    on public.publications (next_attempt_at, created_at, id)
    where purpose = 'daily_digest'
      and status in ('pending', 'failed')
      and not digest_terminal;

create or replace function public.guard_daily_digest_claimed_updates()
returns trigger
language plpgsql
set search_path = ''
as $$
declare
    v_claim_token text;
    v_now timestamptz := pg_catalog.clock_timestamp();
    v_cycle_rollover boolean;
begin
    if old.purpose <> 'daily_digest' then
        return new;
    end if;

    v_claim_token := nullif(pg_catalog.current_setting('tifa.digest_claim_token', true), '');
    v_cycle_rollover := pg_catalog.current_setting('tifa.digest_claim_rollover', true) = 'true';
    if v_claim_token is null then
        raise exception 'daily digest publication update requires a valid claim';
    end if;

    if old.delivery_claim_token::text = v_claim_token
       and old.delivery_claim_expires_at > v_now then
        return new;
    end if;

    if new.delivery_claim_token::text = v_claim_token
       and (old.delivery_claim_token is null
            or old.delivery_claim_expires_at is null
            or old.delivery_claim_expires_at <= v_now) then
        return new;
    end if;

    -- Only the quota reservation RPC may revoke a live, zero-attempt claim
    -- while transferring or releasing its prior-cycle slot.
    if v_cycle_rollover
       and old.attempt_count = 0
       and new.attempt_count = 0
       and new.digest_terminal = old.digest_terminal
       and new.delivery_claim_token is null
       and new.delivery_claim_expires_at is null
       and (new.digest_cycle is distinct from old.digest_cycle
            or (old.digest_slot_reserved and not new.digest_slot_reserved)
            or old.delivery_claim_token is not null) then
        return new;
    end if;

    raise exception 'daily digest publication claim is stale or not owned';
end;
$$;

drop trigger if exists trg_guard_daily_digest_claimed_updates on public.publications;
create trigger trg_guard_daily_digest_claimed_updates
    before update of status, attempt_count, message_id, published_at, last_error,
                     delivery_claim_token, delivery_claim_expires_at,
                     next_attempt_at, retry_schedule_count, digest_terminal
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
       and not digest_terminal
       and status in ('pending', 'failed')
       and (delivery_claim_token is null
            or delivery_claim_expires_at is null
            or delivery_claim_expires_at <= v_now)
       and (next_attempt_at is null or next_attempt_at <= v_now)
    returning * into v_publication;

    if not found then
        return pg_catalog.jsonb_build_object('claim_acquired', false);
    end if;
    return pg_catalog.to_jsonb(v_publication)
        || pg_catalog.jsonb_build_object('claim_acquired', true);
end;
$$;

create or replace function public.advance_daily_digest_attempt_for_cycle(
    p_publication_id uuid,
    p_claim_token uuid,
    p_expected_attempt_count integer,
    p_digest_cycle date,
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
    v_local_now timestamp without time zone;
    v_current_cycle date;
begin
    perform pg_catalog.set_config('tifa.digest_claim_token', p_claim_token::text, true);
    v_local_now := v_now at time zone 'Asia/Kolkata';
    v_current_cycle := v_local_now::date - case
        when v_local_now::time < time '04:00' then 1
        else 0
    end;

    if p_digest_cycle is distinct from v_current_cycle then
        return pg_catalog.jsonb_build_object(
            'attempt_advanced', false, 'cycle_stale', true
        );
    end if;

    if exists (
        select 1
          from public.publications
         where id = p_publication_id
           and delivery_claim_token = p_claim_token
           and delivery_claim_expires_at > v_now
           and digest_cycle is distinct from p_digest_cycle
    ) then
        return pg_catalog.jsonb_build_object(
            'attempt_advanced', false, 'cycle_stale', true
        );
    end if;

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
       and not digest_terminal
       and digest_cycle = p_digest_cycle
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

revoke all on function public.advance_daily_digest_attempt(uuid, uuid, integer, integer, integer)
    from public, anon, authenticated, service_role;
revoke all on function public.advance_daily_digest_attempt_for_cycle(
    uuid, uuid, integer, date, integer, integer
) from public, anon, authenticated;
grant execute on function public.advance_daily_digest_attempt_for_cycle(
    uuid, uuid, integer, date, integer, integer
) to service_role;

create or replace function public.terminalize_daily_digest_publication(
    p_publication_id uuid,
    p_claim_token uuid,
    p_error text,
    p_attempt_limit integer,
    p_conclusive_no_delivery boolean
)
returns boolean
language plpgsql
security invoker
set search_path = ''
as $$
declare
    v_now timestamptz := pg_catalog.clock_timestamp();
begin
    if not p_conclusive_no_delivery then
        return false;
    end if;

    perform pg_catalog.set_config('tifa.digest_claim_token', p_claim_token::text, true);
    update public.publications
       set status = 'failed',
           last_error = pg_catalog.left(p_error, 500),
           digest_terminal = true,
           digest_slot_reserved = false,
           next_attempt_at = null,
           delivery_claim_token = null,
           delivery_claim_expires_at = null
     where id = p_publication_id
       and purpose = 'daily_digest'
       and not digest_terminal
       and delivery_claim_token = p_claim_token
       and delivery_claim_expires_at > v_now
       and attempt_count >= greatest(1, p_attempt_limit);
    return found;
end;
$$;

revoke all on function public.terminalize_daily_digest_publication(uuid, uuid, text, integer, boolean)
    from public, anon, authenticated;
grant execute on function public.terminalize_daily_digest_publication(uuid, uuid, text, integer, boolean)
    to service_role;

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

    if v_existing and v_publication.digest_terminal then
        return pg_catalog.to_jsonb(v_publication)
            || pg_catalog.jsonb_build_object('quota_exhausted', false, 'terminal', true);
    end if;

    if v_existing and v_publication.status = 'published' then
        return pg_catalog.to_jsonb(v_publication)
            || pg_catalog.jsonb_build_object('quota_exhausted', false);
    end if;

    if v_existing
       and v_publication.attempt_count = 0
       and v_publication.digest_cycle is distinct from p_digest_cycle then
        select count(*) into v_reserved_count
          from public.publications
         where purpose = 'daily_digest'
           and digest_cycle = p_digest_cycle
           and digest_slot_reserved
           and id <> v_publication.id;

        if v_reserved_count >= greatest(0, p_limit) then
            if v_publication.delivery_claim_token is not null
               or v_publication.digest_slot_reserved then
                perform pg_catalog.set_config(
                    'tifa.digest_claim_token',
                    coalesce(v_publication.delivery_claim_token::text, 'cycle-rollover'),
                    true
                );
                perform pg_catalog.set_config('tifa.digest_claim_rollover', 'true', true);
                update public.publications
                   set digest_slot_reserved = false,
                       delivery_claim_token = null,
                       delivery_claim_expires_at = null
                 where id = v_publication.id;
            end if;
            return pg_catalog.jsonb_build_object('quota_exhausted', true);
        end if;

        perform pg_catalog.set_config(
            'tifa.digest_claim_token',
            coalesce(v_publication.delivery_claim_token::text, 'cycle-rollover'),
            true
        );
        perform pg_catalog.set_config('tifa.digest_claim_rollover', 'true', true);
        update public.publications
           set digest_cycle = p_digest_cycle,
               digest_slot_reserved = true,
               delivery_claim_token = null,
               delivery_claim_expires_at = null,
               next_attempt_at = null
         where id = v_publication.id
        returning * into v_publication;
        return pg_catalog.to_jsonb(v_publication)
            || pg_catalog.jsonb_build_object('quota_exhausted', false);
    end if;

    if v_existing then
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
revoke all on function public.reserve_daily_digest_publication(uuid, bigint, date, integer)
    from public, anon, authenticated;
grant execute on function public.claim_daily_digest_publication(uuid, uuid, integer) to service_role;
grant execute on function public.reserve_daily_digest_publication(uuid, bigint, date, integer)
    to service_role;

notify pgrst, 'reload schema';
