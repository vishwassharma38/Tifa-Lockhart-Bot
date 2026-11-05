-- Tifa Lockhart Bot - Phase 3 per-asset source provenance.
-- Apply after 004. This is additive: it keeps every existing media item and
-- source row. It only adds the per-asset discriminator needed so multiple
-- gallery entries from one Reddit post can each keep a distinct source row.

do $$
begin
    if to_regclass('public.media_sources') is not null then
        alter table public.media_sources
            add column if not exists source_media_id text not null default '';

        alter table public.media_sources
            drop constraint if exists media_sources_asset_source_key;
        alter table public.media_sources
            add constraint media_sources_asset_source_key
            unique (media_item_id, normalized_source_url, source_media_id);

        drop index if exists public.uq_media_sources_platform_post;
        create unique index if not exists uq_media_sources_platform_post
            on public.media_sources (
                media_item_id,
                source_platform,
                source_post_id,
                source_media_id
            )
            where source_post_id is not null;
    end if;
end;
$$;

create index if not exists idx_media_sources_source_media_id
    on public.media_sources (source_media_id);
