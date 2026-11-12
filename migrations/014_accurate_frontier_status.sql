-- Align the frontier status counters with the same domain cooldown and depth
-- eligibility checks used by claim_crawl_discoveries.
drop function if exists public.crawl_frontier_summary();

create function public.crawl_frontier_summary(p_max_depth integer default 6)
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
            select pg_catalog.count(*)
            from public.discoveries d
            left join public.crawl_domain_state ds
                on ds.registrable_domain = d.registrable_domain
            where d.status = 'queued'
              and d.depth <= greatest(0, least(p_max_depth, 6))
              and (d.next_attempt_at is null or d.next_attempt_at <= pg_catalog.now())
              and (ds.next_eligible_at is null or ds.next_eligible_at <= pg_catalog.now())
        ),
        'delayed_urls', (
            select pg_catalog.count(*)
            from public.discoveries d
            left join public.crawl_domain_state ds
                on ds.registrable_domain = d.registrable_domain
            where d.status = 'queued'
              and d.depth <= greatest(0, least(p_max_depth, 6))
              and (
                  d.next_attempt_at > pg_catalog.now()
                  or ds.next_eligible_at > pg_catalog.now()
              )
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
                pg_catalog.extract(epoch from pg_catalog.now() - min(discovered_at)), 0
            )::bigint
            from public.discoveries where status = 'queued'
        )
    );
$$;

revoke all on function public.crawl_frontier_summary(integer) from public, anon, authenticated;
grant execute on function public.crawl_frontier_summary(integer) to service_role;
