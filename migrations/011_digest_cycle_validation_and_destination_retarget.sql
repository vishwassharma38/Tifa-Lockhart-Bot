-- Validate cycle identifiers in PostgreSQL and support claim-fenced routing updates.
-- Forward-only: migration 010 may already be applied in shared environments.

drop trigger if exists trg_guard_daily_digest_claimed_updates on public.publications;
create trigger trg_guard_daily_digest_claimed_updates
    before update of channel_id, status, attempt_count, message_id, published_at, last_error,
                     delivery_claim_token, delivery_claim_expires_at,
                     next_attempt_at, retry_schedule_count, digest_terminal
    on public.publications
    for each row
    execute function public.guard_daily_digest_claimed_updates();

create or replace function public.retarget_daily_digest_publication(
    p_publication_id uuid,
    p_claim_token uuid,
    p_channel_id bigint
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
    if p_channel_id <= 0 then
        return pg_catalog.jsonb_build_object('claim_valid', false);
    end if;

    perform pg_catalog.set_config('tifa.digest_claim_token', p_claim_token::text, true);
    update public.publications
       set channel_id = p_channel_id
     where id = p_publication_id
       and purpose = 'daily_digest'
       and status in ('pending', 'failed')
       and not digest_terminal
       and delivery_claim_token = p_claim_token
       and delivery_claim_expires_at > v_now
    returning * into v_publication;

    if not found then
        return pg_catalog.jsonb_build_object('claim_valid', false);
    end if;
    return pg_catalog.to_jsonb(v_publication)
        || pg_catalog.jsonb_build_object('claim_valid', true);
end;
$$;

revoke all on function public.retarget_daily_digest_publication(uuid, uuid, bigint)
    from public, anon, authenticated;
grant execute on function public.retarget_daily_digest_publication(uuid, uuid, bigint)
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
    v_now timestamptz;
    v_local_now timestamp without time zone;
    v_current_cycle date;
begin
    perform 1 from public.digest_state where id = 1 for update;
    v_now := pg_catalog.clock_timestamp();
    v_local_now := v_now at time zone 'Asia/Kolkata';
    v_current_cycle := v_local_now::date - case
        when v_local_now::time < time '04:00' then 1
        else 0
    end;

    if p_digest_cycle is distinct from v_current_cycle then
        return pg_catalog.jsonb_build_object(
            'cycle_stale', true, 'quota_exhausted', false
        );
    end if;

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

revoke all on function public.reserve_daily_digest_publication(uuid, bigint, date, integer)
    from public, anon, authenticated;
grant execute on function public.reserve_daily_digest_publication(uuid, bigint, date, integer)
    to service_role;

notify pgrst, 'reload schema';
