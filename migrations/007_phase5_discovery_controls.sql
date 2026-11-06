-- Add durable Phase 5 discovery controls.
--
-- ``next_attempt_at`` keeps Retry-After and retry scheduling durable across
-- application restarts. Brave restriction state is intentionally persisted in
-- the existing singleton runtime row so a restart or /start cannot clear it.

alter table public.discoveries
    add column if not exists next_attempt_at timestamptz;

create index if not exists idx_discoveries_queue_claim
    on public.discoveries (status, next_attempt_at, discovered_at);

create index if not exists idx_discoveries_depth_status
    on public.discoveries (depth, status);

alter table public.runtime_state
    add column if not exists brave_paused boolean not null default false,
    add column if not exists brave_pause_reason text,
    add column if not exists brave_paused_at timestamptz;
