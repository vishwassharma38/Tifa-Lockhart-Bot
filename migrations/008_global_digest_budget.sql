-- Tifa Lockhart Bot - persistent global daily digest budget.
-- Existing publication history is retained and remains eligible for lookup.

alter table public.digest_state
    add column if not exists digest_cycle date;

alter table public.publications
    add column if not exists digest_cycle date,
    add column if not exists digest_slot_reserved boolean not null default false;

-- Backfill one canonical row per media item. Every publication row remains;
-- additional historical channel rows remain available with a NULL cycle.
with ranked as (
    select id,
           row_number() over (
               partition by media_item_id
               order by (status = 'published') desc, created_at, id
           ) as item_rank
      from public.publications
     where purpose = 'daily_digest'
       and digest_cycle is null
       and not exists (
           select 1 from public.publications as claimed
            where claimed.media_item_id = publications.media_item_id
              and claimed.purpose = 'daily_digest'
              and claimed.digest_cycle is not null
       )
)
update public.publications as p
   set digest_cycle = (
           coalesce(p.published_at, p.created_at) at time zone 'Asia/Kolkata'
       )::date,
       digest_slot_reserved = (
           p.status = 'published'
           or p.status = 'pending'
           or p.last_error like 'discord_delivery_ambiguous:%'
           or p.last_error like 'discord_delivery_failed%'
       )
  from ranked
 where ranked.id = p.id
   and ranked.item_rank = 1;

update public.digest_state
   set digest_cycle = (
       coalesce(last_window_end, last_completed_at) at time zone 'Asia/Kolkata'
   )::date
 where id = 1
   and digest_cycle is null
   and coalesce(last_window_end, last_completed_at) is not null;

-- New digest reservations are globally unique per canonical media item. Legacy
-- rows keep a NULL digest_cycle so historical per-channel records are preserved.
create unique index if not exists uq_publications_digest_item_global
    on public.publications (media_item_id)
    where purpose = 'daily_digest' and digest_cycle is not null;

create index if not exists idx_publications_digest_cycle_reserved
    on public.publications (digest_cycle, media_item_id)
    where purpose = 'daily_digest' and digest_slot_reserved;

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
begin
    -- Serialize reservation decisions across every worker through the existing
    -- digest singleton. Concurrent callers cannot overbook the global budget.
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
            return to_jsonb(v_publication) || jsonb_build_object('quota_exhausted', false);
        end if;

        if v_publication.digest_cycle is not null
           and v_publication.digest_cycle <> p_digest_cycle
           and v_publication.digest_slot_reserved then
            -- Preserve the original destination and cycle for retry/reconcile.
            return to_jsonb(v_publication) || jsonb_build_object('quota_exhausted', false);
        end if;
        if v_publication.digest_cycle = p_digest_cycle
           and v_publication.digest_slot_reserved then
            return to_jsonb(v_publication) || jsonb_build_object('quota_exhausted', false);
        end if;
    end if;

    select count(*) into v_reserved_count
      from public.publications
     where purpose = 'daily_digest'
       and digest_cycle = p_digest_cycle
       and digest_slot_reserved;

    if v_reserved_count >= greatest(0, p_limit) then
        return jsonb_build_object('quota_exhausted', true);
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

    return to_jsonb(v_publication) || jsonb_build_object('quota_exhausted', false);
exception when unique_violation then
    -- The item was claimed concurrently; return its canonical digest record.
    select * into v_publication
      from public.publications
     where media_item_id = p_media_item_id
       and purpose = 'daily_digest'
     order by (status = 'published') desc, created_at, id
     limit 1;
    if found then
        return to_jsonb(v_publication) || jsonb_build_object('quota_exhausted', false);
    end if;
    raise;
end;
$$;

revoke all on function public.reserve_daily_digest_publication(uuid, bigint, date, integer)
    from public, anon, authenticated;
grant execute on function public.reserve_daily_digest_publication(uuid, bigint, date, integer)
    to service_role;
