-- Persistent, registrable-domain-aware crawler scheduling and origin policy.
-- Additive only: historical discovery and provenance rows are retained.

alter table public.discoveries
    add column if not exists hostname text,
    add column if not exists registrable_domain text,
    add column if not exists crawl_priority smallint not null default 0,
    add column if not exists claimed_at timestamptz;

alter table public.discoveries
    drop constraint if exists discoveries_crawl_priority_check;
alter table public.discoveries
    add constraint discoveries_crawl_priority_check
    check (crawl_priority between 0 and 100);

alter table public.discovery_edges
    add column if not exists original_url text,
    add column if not exists link_text text,
    add column if not exists source_domain text,
    add column if not exists destination_domain text,
    add column if not exists admission_score smallint,
    add column if not exists admission_reason text,
    add column if not exists discovery_mechanism text;

alter table public.discovery_edges
    drop constraint if exists discovery_edges_admission_score_check;
alter table public.discovery_edges
    add constraint discovery_edges_admission_score_check
    check (admission_score is null or admission_score between 0 and 100);

create table if not exists public.crawl_domain_state (
    registrable_domain text primary key,
    hostnames text[] not null default '{}',
    first_discovered_at timestamptz not null default now(),
    first_crawled_at timestamptz,
    last_claimed_at timestamptz,
    last_crawled_at timestamptz,
    last_http_status integer,
    pages_crawled bigint not null default 0,
    urls_discovered bigint not null default 0,
    external_domains_discovered bigint not null default 0,
    relevant_pages bigint not null default 0,
    relevant_media_items bigint not null default 0,
    success_count bigint not null default 0,
    failure_count bigint not null default 0,
    consecutive_failure_count integer not null default 0,
    http_status_counts jsonb not null default '{}'::jsonb,
    robots_policy_state text not null default 'unknown',
    cooldown_until timestamptz,
    next_eligible_at timestamptz,
    average_inspection_ms double precision,
    domain_health real not null default 1.0,
    domain_relevance_score real not null default 0.0,
    exploration_priority smallint not null default 100,
    operational_state text not null default 'new',
    state_reason text,
    sitemap_checked_at timestamptz,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    constraint crawl_domain_state_counts_check check (
        pages_crawled >= 0 and urls_discovered >= 0
        and external_domains_discovered >= 0 and relevant_pages >= 0
        and relevant_media_items >= 0 and success_count >= 0 and failure_count >= 0
        and consecutive_failure_count >= 0
    ),
    constraint crawl_domain_state_priority_check check (exploration_priority between 0 and 100),
    constraint crawl_domain_state_health_check check (domain_health between 0 and 1),
    constraint crawl_domain_state_status_check check (
        operational_state in ('new', 'active', 'cooldown', 'restricted', 'degraded', 'unavailable')
    )
);

create index if not exists idx_crawl_domain_state_due
    on public.crawl_domain_state (next_eligible_at, first_crawled_at, last_claimed_at);
create index if not exists idx_crawl_domain_state_exploration
    on public.crawl_domain_state (first_crawled_at, exploration_priority desc, last_crawled_at);

-- Robots policy is origin-scoped: scheme, hostname, and port can each serve a
-- different file. Bodies are capped by the application before persistence.
create table if not exists public.crawl_origin_state (
    origin text primary key,
    hostname text not null,
    registrable_domain text not null references public.crawl_domain_state(registrable_domain),
    robots_body text,
    robots_status integer,
    robots_category text not null default 'unknown',
    robots_checked_at timestamptz,
    robots_expires_at timestamptz,
    robots_crawl_delay double precision,
    next_eligible_at timestamptz,
    last_request_at timestamptz,
    request_count bigint not null default 0,
    success_count bigint not null default 0,
    failure_count bigint not null default 0,
    average_response_ms double precision,
    updated_at timestamptz not null default now()
);

create index if not exists idx_crawl_origin_domain
    on public.crawl_origin_state (registrable_domain, next_eligible_at);

alter table public.job_runs
    add column if not exists stats_json jsonb not null default '{}'::jsonb,
    add column if not exists domains_visited_count integer not null default 0,
    add column if not exists new_domains_discovered_count integer not null default 0,
    add column if not exists external_links_admitted_count integer not null default 0,
    add column if not exists internal_links_admitted_count integer not null default 0,
    add column if not exists skipped_url_count integer not null default 0,
    add column if not exists resource_budget_limited boolean not null default false;

alter table public.job_runs
    drop constraint if exists job_runs_counts_check;
alter table public.job_runs
    add constraint job_runs_counts_check check (
        processed_count >= 0 and stored_count >= 0 and skipped_count >= 0
        and discovered_count >= 0 and examined_count >= 0
        and duplicate_count >= 0 and failed_count >= 0
        and domains_visited_count >= 0 and new_domains_discovered_count >= 0
        and external_links_admitted_count >= 0 and internal_links_admitted_count >= 0
        and skipped_url_count >= 0
    );

create index if not exists idx_discoveries_frontier_round
    on public.discoveries (
        status, registrable_domain, crawl_priority desc, discovered_at, id
    ) where status = 'queued';
create index if not exists idx_discoveries_frontier_due
    on public.discoveries (status, next_attempt_at, depth, registrable_domain)
    where status = 'queued';

create or replace function public.register_crawl_discovery_domain()
returns trigger
language plpgsql
set search_path = ''
as $$
declare
    parent_domain text;
    inserted_domain integer;
begin
    if new.registrable_domain is null or new.registrable_domain = '' then
        return new;
    end if;

    insert into public.crawl_domain_state (
        registrable_domain, hostnames, first_discovered_at, urls_discovered
    ) values (
        new.registrable_domain,
        case when new.hostname is null then '{}'::text[] else array[new.hostname] end,
        new.discovered_at,
        1
    )
    on conflict (registrable_domain) do nothing;
    get diagnostics inserted_domain = row_count;
    if inserted_domain = 0 then
        update public.crawl_domain_state ds set
            hostnames = (
                select coalesce(array_agg(distinct host), '{}'::text[])
                from unnest(
                    ds.hostnames || case
                        when new.hostname is null then '{}'::text[]
                        else array[new.hostname]
                    end
                ) as host
            ),
            first_discovered_at = least(ds.first_discovered_at, new.discovered_at),
            urls_discovered = ds.urls_discovered + 1,
            updated_at = pg_catalog.now()
        where ds.registrable_domain = new.registrable_domain;
    end if;

    if new.parent_discovery_id is not null then
        select d.registrable_domain into parent_domain
        from public.discoveries d where d.id = new.parent_discovery_id;
        parent_domain := coalesce(
            parent_domain,
            new.metadata_json ->> 'source_registrable_domain'
        );
        if parent_domain is not null
           and parent_domain <> new.registrable_domain
           and inserted_domain = 1 then
            update public.crawl_domain_state
            set external_domains_discovered = external_domains_discovered + 1,
                updated_at = pg_catalog.now()
            where registrable_domain = parent_domain;
        end if;
    end if;
    return new;
end;
$$;

drop trigger if exists trg_discoveries_register_crawl_domain on public.discoveries;
create trigger trg_discoveries_register_crawl_domain
    after insert on public.discoveries
    for each row execute function public.register_crawl_discovery_domain();

create or replace function public.track_crawl_discovery_outcome()
returns trigger
language plpgsql
set search_path = ''
as $$
declare
    did_fetch boolean;
    status_key text;
    elapsed_ms double precision;
    relevance integer;
begin
    if old.status <> 'processing' or new.status = 'processing' or old.claimed_at is null then
        return new;
    end if;
    if new.fetched_at is not distinct from old.fetched_at
       and new.http_status is not distinct from old.http_status
       and new.retry_count is not distinct from old.retry_count then
        return new;
    end if;
    -- Requeues caused by a local domain budget or pause did not contact the site.
    if new.fetched_at is null then
        return new;
    end if;

    did_fetch := new.fetched_at is not null and new.http_status between 200 and 299;
    status_key := coalesce(new.http_status::text, new.status);
    begin
        elapsed_ms := nullif(new.metadata_json ->> 'inspection_latency_ms', '')::double precision;
    exception when others then
        elapsed_ms := null;
    end;
    begin
        relevance := nullif(
            new.metadata_json #>> '{classification,score}', ''
        )::integer;
    exception when others then
        relevance := 0;
    end;

    update public.crawl_domain_state ds set
        pages_crawled = ds.pages_crawled + 1,
        first_crawled_at = case
            when did_fetch then coalesce(ds.first_crawled_at, new.fetched_at)
            else ds.first_crawled_at
        end,
        last_crawled_at = pg_catalog.now(),
        last_http_status = new.http_status,
        success_count = ds.success_count + case when did_fetch then 1 else 0 end,
        failure_count = ds.failure_count + case when did_fetch then 0 else 1 end,
        consecutive_failure_count = case when did_fetch then 0 else ds.consecutive_failure_count + 1 end,
        relevant_pages = ds.relevant_pages + case when coalesce(relevance, 0) > 0 then 1 else 0 end,
        http_status_counts = pg_catalog.jsonb_set(
            ds.http_status_counts,
            array[status_key],
            pg_catalog.to_jsonb(coalesce((ds.http_status_counts ->> status_key)::bigint, 0) + 1),
            true
        ),
        average_inspection_ms = case
            when elapsed_ms is null then ds.average_inspection_ms
            when ds.pages_crawled = 0 or ds.average_inspection_ms is null then elapsed_ms
            else ((ds.average_inspection_ms * ds.pages_crawled) + elapsed_ms)
                / (ds.pages_crawled + 1)
        end,
        domain_health = greatest(0, least(1,
            ds.domain_health + case when did_fetch then 0.02 else -0.05 end
        )),
        domain_relevance_score = greatest(0, least(100,
            ds.domain_relevance_score + case when coalesce(relevance, 0) > 0 then 1 else 0 end
        )),
        exploration_priority = greatest(0, least(100,
            ds.exploration_priority + case
                when coalesce(relevance, 0) > 0 then 5
                when did_fetch then -2
                else -5
            end
        )),
        operational_state = case
            when ds.next_eligible_at > pg_catalog.now() then 'cooldown'
            when did_fetch then 'active'
            when new.status = 'blocked' then 'restricted'
            else 'degraded'
        end,
        state_reason = new.last_error,
        updated_at = pg_catalog.now()
    where ds.registrable_domain = new.registrable_domain;
    return new;
end;
$$;

drop trigger if exists trg_discoveries_track_crawl_outcome on public.discoveries;
create trigger trg_discoveries_track_crawl_outcome
    after update of status on public.discoveries
    for each row execute function public.track_crawl_discovery_outcome();

create or replace function public.claim_crawl_discoveries(
    p_limit integer,
    p_max_depth integer,
    p_per_domain_limit integer default 2,
    p_new_domain_share numeric default 0.8
)
returns setof public.discoveries
language sql
security definer
set search_path = ''
as $$
    with ranked as materialized (
        select
            d.id,
            coalesce(nullif(d.registrable_domain, ''), nullif(d.domain, ''), d.normalized_url)
                as domain_key,
            row_number() over (
                partition by coalesce(nullif(d.registrable_domain, ''), nullif(d.domain, ''), d.normalized_url)
                order by d.crawl_priority desc, d.discovered_at asc, d.id asc
            ) as domain_round,
            coalesce(ds.first_crawled_at is null, true) as unexplored,
            dense_rank() over (
                order by
                    coalesce(ds.first_crawled_at is null, true) desc,
                    ds.last_claimed_at asc nulls first,
                    ds.first_discovered_at asc nulls first,
                    coalesce(nullif(d.registrable_domain, ''), nullif(d.domain, ''), d.normalized_url)
            ) as cold_domain_rank,
            ds.last_claimed_at,
            coalesce(ds.exploration_priority, 100) as exploration_priority,
            d.crawl_priority,
            d.discovered_at
        from public.discoveries d
        left join public.crawl_domain_state ds
            on ds.registrable_domain = d.registrable_domain
        where d.status = 'queued'
          and d.depth <= greatest(0, least(p_max_depth, 6))
          and (d.next_attempt_at is null or d.next_attempt_at <= pg_catalog.now())
          and (ds.next_eligible_at is null or ds.next_eligible_at <= pg_catalog.now())
    ),
    selected as materialized (
        select d.id, ranked.domain_key, ranked.domain_round
        from public.discoveries d
        join ranked on ranked.id = d.id
        where ranked.domain_round <= greatest(1, least(p_per_domain_limit, 10))
        order by
            ranked.domain_round asc,
            case
                when ranked.unexplored and ranked.cold_domain_rank <= greatest(
                    1,
                    pg_catalog.floor(
                        greatest(1, least(p_limit, 300))
                        * greatest(0.5, least(p_new_domain_share, 0.95))
                    )::integer
                ) then 0
                when not ranked.unexplored then 1
                else 2
            end asc,
            ranked.cold_domain_rank asc,
            ranked.last_claimed_at asc nulls first,
            ranked.exploration_priority desc,
            ranked.crawl_priority desc,
            ranked.discovered_at asc,
            d.id asc
        limit greatest(0, least(p_limit, 300))
        for update of d skip locked
    ),
    claimed as (
        update public.discoveries d
        set status = 'processing',
            claimed_at = pg_catalog.now(),
            next_attempt_at = null
        from selected s
        where d.id = s.id and d.status = 'queued'
        returning d.*
    ),
    domain_claims as (
        update public.crawl_domain_state ds
        set last_claimed_at = pg_catalog.now(), updated_at = pg_catalog.now()
        where ds.registrable_domain in (
            select c.registrable_domain from claimed c where c.registrable_domain is not null
        )
        returning ds.registrable_domain
    )
    select c.*
    from claimed c
    join selected s on s.id = c.id
    cross join (select pg_catalog.count(*) from domain_claims) applied
    order by
        s.domain_round asc,
        c.crawl_priority desc,
        c.discovered_at asc,
        c.id asc;
$$;

create or replace function public.recover_expired_crawl_claims(p_lease_seconds integer default 900)
returns integer
language plpgsql
security definer
set search_path = ''
as $$
declare
    recovered integer;
begin
    update public.discoveries
    set status = 'queued',
        claimed_at = null,
        last_error = 'interrupted_by_restart',
        next_attempt_at = pg_catalog.now()
    where status = 'processing'
      and (claimed_at is null or claimed_at < pg_catalog.now()
           - pg_catalog.make_interval(secs => greatest(60, least(p_lease_seconds, 86400))));
    get diagnostics recovered = row_count;
    return recovered;
end;
$$;

create or replace function public.backfill_crawl_domain_identities(p_items jsonb)
returns integer
language plpgsql
security definer
set search_path = ''
as $$
declare
    updated_count integer;
begin
    with items as (
        select * from pg_catalog.jsonb_to_recordset(coalesce(p_items, '[]'::jsonb))
            as x(id uuid, hostname text, registrable_domain text)
    ),
    updated as (
        update public.discoveries d
        set hostname = i.hostname, registrable_domain = i.registrable_domain
        from items i
        where d.id = i.id and d.registrable_domain is null
        returning d.id, d.hostname, d.registrable_domain, d.discovered_at,
                  d.fetched_at, d.http_status
    ),
    domain_batch as (
        select
            d.registrable_domain,
            pg_catalog.array_agg(distinct d.hostname) as hostnames,
            min(d.discovered_at) as first_discovered_at,
            pg_catalog.count(*) as urls_discovered,
            min(d.fetched_at) filter (
                where d.fetched_at is not null and d.http_status between 200 and 299
            ) as first_crawled_at,
            max(d.fetched_at) as last_crawled_at,
            pg_catalog.count(d.fetched_at) as pages_crawled,
            pg_catalog.count(*) filter (
                where d.fetched_at is not null and d.http_status between 200 and 299
            ) as success_count,
            pg_catalog.count(*) filter (
                where d.fetched_at is not null
                  and (d.http_status is null or d.http_status not between 200 and 299)
            ) as failure_count
        from updated d
        group by d.registrable_domain
    ),
    upserted as (
        insert into public.crawl_domain_state (
            registrable_domain, hostnames, first_discovered_at, urls_discovered,
            first_crawled_at, last_crawled_at, pages_crawled, success_count, failure_count
        )
        select
            registrable_domain, hostnames, first_discovered_at, urls_discovered,
            first_crawled_at, last_crawled_at, pages_crawled, success_count, failure_count
        from domain_batch
        on conflict (registrable_domain) do update set
            hostnames = (
                select coalesce(array_agg(distinct host), '{}'::text[])
                from unnest(public.crawl_domain_state.hostnames || excluded.hostnames) as host
            ),
            first_discovered_at = least(
                public.crawl_domain_state.first_discovered_at, excluded.first_discovered_at
            ),
            urls_discovered = public.crawl_domain_state.urls_discovered + excluded.urls_discovered,
            first_crawled_at = least(
                public.crawl_domain_state.first_crawled_at, excluded.first_crawled_at
            ),
            last_crawled_at = greatest(
                public.crawl_domain_state.last_crawled_at, excluded.last_crawled_at
            ),
            pages_crawled = public.crawl_domain_state.pages_crawled + excluded.pages_crawled,
            success_count = public.crawl_domain_state.success_count + excluded.success_count,
            failure_count = public.crawl_domain_state.failure_count + excluded.failure_count,
            updated_at = pg_catalog.now()
        returning registrable_domain
    )
    select pg_catalog.count(*) into updated_count
    from updated
    cross join (select pg_catalog.count(*) from upserted) applied;
    return updated_count;
end;
$$;

create or replace function public.pause_crawl_domain(
    p_registrable_domain text,
    p_next_eligible_at timestamptz,
    p_reason text
)
returns void
language sql
security definer
set search_path = ''
as $$
    update public.crawl_domain_state
    set next_eligible_at = greatest(
            coalesce(next_eligible_at, '-infinity'::timestamptz), p_next_eligible_at
        ),
        cooldown_until = greatest(
            coalesce(cooldown_until, '-infinity'::timestamptz), p_next_eligible_at
        ),
        operational_state = 'cooldown',
        state_reason = left(p_reason, 120),
        updated_at = pg_catalog.now()
    where registrable_domain = p_registrable_domain;
$$;

create or replace function public.crawl_frontier_summary()
returns jsonb
language sql
security definer
set search_path = ''
as $$
    select pg_catalog.jsonb_build_object(
        'domains_discovered', (select pg_catalog.count(*) from public.crawl_domain_state),
        'domains_unexplored', (
            select pg_catalog.count(*) from public.crawl_domain_state
            where first_crawled_at is null
        ),
        'domains_explored', (
            select pg_catalog.count(*) from public.crawl_domain_state
            where first_crawled_at is not null
        ),
        'domains_cooling_down', (
            select pg_catalog.count(*) from public.crawl_domain_state
            where next_eligible_at > pg_catalog.now()
        ),
        'queued_domains', (
            select pg_catalog.count(distinct coalesce(registrable_domain, domain))
            from public.discoveries where status = 'queued'
        ),
        'domains_visited_recently', (
            select pg_catalog.count(*) from public.crawl_domain_state
            where last_crawled_at >= pg_catalog.now() - interval '7 days'
        ),
        'queued_urls', (
            select pg_catalog.count(*) from public.discoveries
            where status = 'queued'
        ),
        'due_urls', (
            select pg_catalog.count(*) from public.discoveries
            where status = 'queued'
              and (next_attempt_at is null or next_attempt_at <= pg_catalog.now())
        ),
        'delayed_urls', (
            select pg_catalog.count(*) from public.discoveries
            where status = 'queued' and next_attempt_at > pg_catalog.now()
        ),
        'processing_urls', (
            select pg_catalog.count(*) from public.discoveries
            where status = 'processing'
        ),
        'retry_backlog', (
            select pg_catalog.count(*) from public.discoveries
            where status = 'queued' and retry_count > 0
        ),
        'oldest_queued_seconds', (
            select coalesce(
                pg_catalog.extract(epoch from pg_catalog.now() - min(discovered_at)),
                0
            )::bigint
            from public.discoveries where status = 'queued'
        )
    );
$$;

create or replace function public.cache_crawl_robots_policy()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
    update public.crawl_domain_state
    set robots_policy_state = case
            when new.robots_category = 'ok' and new.robots_status between 200 and 299 then 'parsed'
            when new.robots_status in (404, 410) then 'missing_allow'
            when new.robots_status in (401, 403, 429) then 'restricted'
            when new.robots_category = 'unavailable' then 'unavailable'
            else 'cached'
        end,
        updated_at = pg_catalog.now()
    where registrable_domain = new.registrable_domain;
    new.updated_at := pg_catalog.now();
    return new;
end;
$$;

drop trigger if exists trg_crawl_origin_robots_state on public.crawl_origin_state;
create trigger trg_crawl_origin_robots_state
    before insert or update of robots_category, robots_status on public.crawl_origin_state
    for each row execute function public.cache_crawl_robots_policy();

alter table public.crawl_domain_state enable row level security;
alter table public.crawl_origin_state enable row level security;
grant select, insert, update, delete on public.crawl_domain_state to service_role;
grant select, insert, update, delete on public.crawl_origin_state to service_role;

revoke all on function public.claim_crawl_discoveries(integer, integer, integer, numeric)
    from public, anon, authenticated;
grant execute on function public.claim_crawl_discoveries(integer, integer, integer, numeric) to service_role;
revoke all on function public.recover_expired_crawl_claims(integer) from public, anon, authenticated;
grant execute on function public.recover_expired_crawl_claims(integer) to service_role;
revoke all on function public.backfill_crawl_domain_identities(jsonb) from public, anon, authenticated;
grant execute on function public.backfill_crawl_domain_identities(jsonb) to service_role;
revoke all on function public.pause_crawl_domain(text, timestamptz, text) from public, anon, authenticated;
grant execute on function public.pause_crawl_domain(text, timestamptz, text) to service_role;
revoke all on function public.crawl_frontier_summary() from public, anon, authenticated;
grant execute on function public.crawl_frontier_summary() to service_role;
