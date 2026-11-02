-- Tifa Lockhart Bot - Phase 1 initial schema.
-- Apply this migration once to a Supabase PostgreSQL project.
-- This file only creates schema objects. It does not delete or rewrite data.

create extension if not exists pgcrypto;

-- Discoveries: one canonical row per normalized URL or source candidate.
create table if not exists public.discoveries (
    id uuid primary key default gen_random_uuid(),
    url text not null,
    normalized_url text not null,
    source_type text not null,
    source_platform_id text,
    title text,
    description text,
    creator_name text,
    creator_profile_url text,
    domain text,
    parent_discovery_id uuid references public.discoveries(id) on delete set null,
    depth smallint not null default 0,
    status text not null default 'queued',
    http_status integer,
    retry_count integer not null default 0,
    last_error text,
    discovered_at timestamptz not null default now(),
    fetched_at timestamptz,
    verified_at timestamptz,
    reviewed_at timestamptz,
    reviewed_by text,
    review_note text,
    metadata_json jsonb not null default '{}'::jsonb,
    constraint discoveries_normalized_url_key unique (normalized_url),
    constraint discoveries_source_type_check
        check (source_type in ('reddit', 'brave_html', 'seed', 'manual_save')),
    constraint discoveries_depth_check check (depth between 0 and 6),
    constraint discoveries_status_check
        check (status in ('queued', 'processing', 'verified', 'dumpster', 'rejected', 'blocked', 'failed', 'oversize')),
    constraint discoveries_http_status_check
        check (http_status is null or http_status between 100 and 599),
    constraint discoveries_retry_count_check check (retry_count >= 0)
);

create index if not exists idx_discoveries_status
    on public.discoveries (status);
create index if not exists idx_discoveries_domain
    on public.discoveries (domain);
create index if not exists idx_discoveries_source_type
    on public.discoveries (source_type);
create index if not exists idx_discoveries_parent
    on public.discoveries (parent_discovery_id);
create index if not exists idx_discoveries_discovered_at
    on public.discoveries (discovered_at desc);

-- Discovery edges: one row per observed parent-to-child URL relationship.
create table if not exists public.discovery_edges (
    id uuid primary key default gen_random_uuid(),
    parent_discovery_id uuid references public.discoveries(id) on delete restrict,
    child_discovery_id uuid references public.discoveries(id) on delete set null,
    child_normalized_url text not null,
    discovered_from_url text not null,
    depth smallint not null default 0,
    discovered_at timestamptz not null default now(),
    constraint discovery_edges_depth_check check (depth between 0 and 6)
);

create unique index if not exists uq_discovery_edges_parent_child
    on public.discovery_edges (parent_discovery_id, child_normalized_url)
    where parent_discovery_id is not null;
create unique index if not exists uq_discovery_edges_root_child
    on public.discovery_edges (child_normalized_url)
    where parent_discovery_id is null;
create index if not exists idx_discovery_edges_child
    on public.discovery_edges (child_discovery_id);
create index if not exists idx_discovery_edges_child_url
    on public.discovery_edges (child_normalized_url);

-- Media items: one row per unique physical file, never the binary itself.
create table if not exists public.media_items (
    id uuid primary key default gen_random_uuid(),
    sha256 text not null,
    perceptual_hash text,
    filename text not null,
    extension text not null,
    mime_type text not null,
    media_type text not null,
    category text not null default 'other',
    size_bytes bigint not null default 0,
    width integer,
    height integer,
    duration_seconds double precision,
    -- Historical column names are retained so the Phase 1 schema and media
    -- identity stay stable. They store the private B2 bucket and object key.
    r2_bucket text,
    r2_object_key text,
    verification_status text not null default 'verified',
    favorite boolean not null default false,
    download_status text not null default 'stored',
    created_at timestamptz not null default now(),
    downloaded_at timestamptz,
    constraint media_items_sha256_key unique (sha256),
    constraint media_items_sha256_format_check check (sha256 ~ '^[0-9a-f]{64}$'),
    constraint media_items_media_type_check
        check (media_type in ('image', 'animation', 'video')),
    constraint media_items_category_check
        check (category in ('fanart', 'cosplay_photography', 'official_artwork', 'wallpaper', 'video', 'other')),
    constraint media_items_verification_status_check
        check (verification_status in ('verified', 'dumpster')),
    constraint media_items_download_status_check
        check (download_status in ('stored', 'failed', 'skipped')),
    constraint media_items_size_nonnegative_check check (size_bytes >= 0),
    constraint media_items_width_check check (width is null or width > 0),
    constraint media_items_height_check check (height is null or height > 0),
    constraint media_items_duration_check
        check (duration_seconds is null or duration_seconds >= 0),
    constraint media_items_stored_check check (
        download_status <> 'stored'
        or (
            size_bytes between 1 and 10000000
            and r2_bucket is not null
            and r2_object_key is not null
            and r2_object_key ~ '^assets/[0-9a-f]{2}/[0-9a-f]{64}\.[a-z0-9]+$'
            and r2_object_key like 'assets/' || left(sha256, 2) || '/' || sha256 || '.%'
        )
    ),
    constraint media_items_r2_object_key_key unique (r2_bucket, r2_object_key)
);

create index if not exists idx_media_items_verification_status
    on public.media_items (verification_status);
create index if not exists idx_media_items_category
    on public.media_items (category);
create index if not exists idx_media_items_media_type
    on public.media_items (media_type);
create index if not exists idx_media_items_download_status
    on public.media_items (download_status);
create index if not exists idx_media_items_created_at
    on public.media_items (created_at desc);
create index if not exists idx_media_items_downloaded_at
    on public.media_items (downloaded_at desc);
create index if not exists idx_media_items_favorite
    on public.media_items (created_at desc)
    where favorite = true;

-- Media sources: preserve every source attribution for an asset.
create table if not exists public.media_sources (
    id uuid primary key default gen_random_uuid(),
    media_item_id uuid not null references public.media_items(id) on delete restrict,
    discovery_id uuid references public.discoveries(id) on delete set null,
    source_platform text,
    source_post_id text,
    source_url text not null,
    normalized_source_url text not null,
    media_url text,
    creator_name text,
    creator_profile_url text,
    caption text,
    title text,
    discovered_at timestamptz not null default now(),
    constraint media_sources_asset_source_key unique (media_item_id, normalized_source_url)
);

create unique index if not exists uq_media_sources_platform_post
    on public.media_sources (media_item_id, source_platform, source_post_id)
    where source_post_id is not null;
create index if not exists idx_media_sources_media_item
    on public.media_sources (media_item_id);
create index if not exists idx_media_sources_discovery
    on public.media_sources (discovery_id);
create index if not exists idx_media_sources_source_platform
    on public.media_sources (source_platform);
create index if not exists idx_media_sources_discovered_at
    on public.media_sources (discovered_at desc);

-- Publications: one row per attempted Discord delivery.
create table if not exists public.publications (
    id uuid primary key default gen_random_uuid(),
    media_item_id uuid not null references public.media_items(id) on delete restrict,
    channel_id bigint not null,
    message_id bigint,
    purpose text not null,
    status text not null default 'pending',
    attempt_count integer not null default 0,
    last_error text,
    created_at timestamptz not null default now(),
    published_at timestamptz,
    constraint publications_purpose_check
        check (purpose in ('daily_digest', 'manual_save', 'random')),
    constraint publications_status_check
        check (status in ('pending', 'published', 'failed')),
    constraint publications_attempt_count_check check (attempt_count >= 0),
    constraint publications_published_message_check
        check (status <> 'published' or message_id is not null)
);

create unique index if not exists uq_publications_digest_item_channel
    on public.publications (media_item_id, channel_id)
    where purpose = 'daily_digest';
create index if not exists idx_publications_media_item
    on public.publications (media_item_id);
create index if not exists idx_publications_status
    on public.publications (status);
create index if not exists idx_publications_channel
    on public.publications (channel_id);
create index if not exists idx_publications_created_at
    on public.publications (created_at desc);

-- Job runs: durable records for scheduled and manually triggered work.
create table if not exists public.job_runs (
    id uuid primary key default gen_random_uuid(),
    job_type text not null,
    outcome text not null default 'running',
    started_at timestamptz not null default now(),
    finished_at timestamptz,
    processed_count integer not null default 0,
    stored_count integer not null default 0,
    skipped_count integer not null default 0,
    error_summary text,
    config_snapshot jsonb not null default '{}'::jsonb,
    constraint job_runs_job_type_check
        check (job_type in ('reddit_collection', 'brave_discovery', 'crawl', 'daily_digest', 'manual_save')),
    constraint job_runs_outcome_check
        check (outcome in ('running', 'succeeded', 'failed', 'partial', 'skipped')),
    constraint job_runs_counts_check
        check (processed_count >= 0 and stored_count >= 0 and skipped_count >= 0),
    constraint job_runs_finished_check
        check (finished_at is null or finished_at >= started_at)
);

create index if not exists idx_job_runs_job_type
    on public.job_runs (job_type);
create index if not exists idx_job_runs_outcome
    on public.job_runs (outcome);
create index if not exists idx_job_runs_started_at
    on public.job_runs (started_at desc);

-- Runtime state: singleton row for persistent bot controls and cursors.
create table if not exists public.runtime_state (
    id smallint primary key default 1,
    collection_enabled boolean not null default false,
    last_reddit_success_at timestamptz,
    last_brave_success_at timestamptz,
    last_digest_success_at timestamptz,
    updated_at timestamptz not null default now(),
    constraint runtime_state_singleton_check check (id = 1)
);

insert into public.runtime_state (id)
values (1)
on conflict (id) do nothing;

-- Digest state: singleton row for the last successful digest window.
create table if not exists public.digest_state (
    id smallint primary key default 1,
    last_window_start timestamptz,
    last_window_end timestamptz,
    last_completed_at timestamptz,
    cursor_media_created_at timestamptz,
    updated_at timestamptz not null default now(),
    constraint digest_state_singleton_check check (id = 1),
    constraint digest_state_window_check check (
        last_window_start is null
        or last_window_end is null
        or last_window_end >= last_window_start
    )
);

insert into public.digest_state (id)
values (1)
on conflict (id) do nothing;

-- Keep updated_at timestamps accurate without application-side writes.
create or replace function public.set_tifa_updated_at()
returns trigger
language plpgsql
as $$
begin
    new.updated_at = now();
    return new;
end;
$$;

drop trigger if exists trg_runtime_state_updated_at on public.runtime_state;
create trigger trg_runtime_state_updated_at
    before update on public.runtime_state
    for each row
    execute function public.set_tifa_updated_at();

drop trigger if exists trg_digest_state_updated_at on public.digest_state;
create trigger trg_digest_state_updated_at
    before update on public.digest_state
    for each row
    execute function public.set_tifa_updated_at();

-- Defense in depth: no anon/authenticated policies exist. The service role
-- bypasses RLS, while public API callers cannot read or write these tables.
alter table public.discoveries enable row level security;
alter table public.discovery_edges enable row level security;
alter table public.media_items enable row level security;
alter table public.media_sources enable row level security;
alter table public.publications enable row level security;
alter table public.job_runs enable row level security;
alter table public.runtime_state enable row level security;
alter table public.digest_state enable row level security;
