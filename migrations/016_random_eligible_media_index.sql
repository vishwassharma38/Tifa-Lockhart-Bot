-- Fast, uniformly random eligible-media selection.
-- Additive only: this partial index changes no rows and no existing constraint.
--
-- /random counts eligible rows and then fetches one row by ordered offset.
-- The predicate matches the repository filter exactly so PostgreSQL can use
-- this index for both the count and the ordered single-row range lookup.
-- Rollback: drop index if exists public.idx_media_items_random_eligible;

create index if not exists idx_media_items_random_eligible
    on public.media_items (id)
    where verification_status = 'verified'
      and download_status = 'stored';
