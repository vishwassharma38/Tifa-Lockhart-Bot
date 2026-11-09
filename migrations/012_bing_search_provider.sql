-- Replace the retired search-provider schema with Bing public-HTML discovery.
--
-- Forward-only: earlier migrations remain immutable because they reproduce the
-- historical schema for existing deployments. Historical discovery rows keep
-- their original metadata provenance; their source type becomes the neutral
-- ``web_legacy`` value instead of falsely claiming a Bing origin. Persisted
-- pause and success state from the retired provider is deliberately dropped,
-- not transferred, because it is not evidence about Bing access.

alter table public.runtime_state
    add column if not exists bing_paused boolean not null default false,
    add column if not exists bing_pause_reason text,
    add column if not exists bing_paused_at timestamptz,
    add column if not exists last_bing_success_at timestamptz;

-- Preserve historical rows and their metadata while replacing the obsolete
-- provider-specific source type with a provider-neutral historical value.
update public.discoveries
set source_type = 'web_legacy'
where source_type = 'brave_html';

alter table public.discoveries
    drop constraint if exists discoveries_source_type_check;

alter table public.discoveries
    add constraint discoveries_source_type_check
    check (source_type in ('reddit', 'web_legacy', 'bing_html', 'seed', 'manual_save'));

-- Keep the historical job type in the constraint so existing job-run history
-- remains valid, while allowing the active Bing discovery job type.
alter table public.job_runs
    drop constraint if exists job_runs_job_type_check;

alter table public.job_runs
    add constraint job_runs_job_type_check
    check (
        job_type in (
            'reddit_collection',
            'bing_discovery',
            'crawl',
            'daily_digest',
            'manual_save',
            'brave_discovery'
        )
    );

alter table public.runtime_state
    drop column if exists brave_paused,
    drop column if exists brave_pause_reason,
    drop column if exists brave_paused_at,
    drop column if exists last_brave_success_at;
