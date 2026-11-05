-- Add durable Phase 4 collection controls and complete Reddit run counters.
alter table public.runtime_state
    add column if not exists collection_changed_at timestamptz;

alter table public.job_runs
    add column if not exists discovered_count integer not null default 0,
    add column if not exists examined_count integer not null default 0,
    add column if not exists duplicate_count integer not null default 0,
    add column if not exists failed_count integer not null default 0;

alter table public.job_runs
    drop constraint if exists job_runs_counts_check;
alter table public.job_runs
    add constraint job_runs_counts_check check (
        processed_count >= 0 and stored_count >= 0 and skipped_count >= 0
        and discovered_count >= 0 and examined_count >= 0
        and duplicate_count >= 0 and failed_count >= 0
    );
