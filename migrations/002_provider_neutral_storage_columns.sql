-- Tifa Lockhart Bot - provider-neutral storage column names.
-- Apply after 001_initial_schema.sql. This migration preserves existing data.
-- It renames columns and the related uniqueness constraint; it does not drop
-- or recreate public.media_items.

do $$
begin
    if to_regclass('public.media_items') is not null then
        if exists (
            select 1
            from information_schema.columns
            where table_schema = 'public'
              and table_name = 'media_items'
              and column_name = 'r2_bucket'
        ) and not exists (
            select 1
            from information_schema.columns
            where table_schema = 'public'
              and table_name = 'media_items'
              and column_name = 'storage_bucket'
        ) then
            alter table public.media_items rename column r2_bucket to storage_bucket;
        end if;

        if exists (
            select 1
            from information_schema.columns
            where table_schema = 'public'
              and table_name = 'media_items'
              and column_name = 'r2_object_key'
        ) and not exists (
            select 1
            from information_schema.columns
            where table_schema = 'public'
              and table_name = 'media_items'
              and column_name = 'storage_object_key'
        ) then
            alter table public.media_items rename column r2_object_key to storage_object_key;
        end if;
    end if;
end;
$$;

-- Rename the unique constraint. PostgreSQL also renames its backing index
-- when a constraint is renamed; the index guard below covers databases where
-- the index and constraint names diverged.
do $$
begin
    if to_regclass('public.media_items') is not null then
        if exists (
            select 1
            from pg_constraint
            where conrelid = 'public.media_items'::regclass
              and conname = 'media_items_r2_object_key_key'
        ) and not exists (
            select 1
            from pg_constraint
            where conrelid = 'public.media_items'::regclass
              and conname = 'media_items_storage_object_key_key'
        ) then
            alter table public.media_items
                rename constraint media_items_r2_object_key_key to media_items_storage_object_key_key;
        end if;
    end if;
end;
$$;

do $$
begin
    if to_regclass('public.media_items') is not null then
        if exists (
            select 1
            from pg_class c
            join pg_namespace n on n.oid = c.relnamespace
            where n.nspname = 'public'
              and c.relkind = 'i'
              and c.relname = 'media_items_r2_object_key_key'
        ) and not exists (
            select 1
            from pg_class c
            join pg_namespace n on n.oid = c.relnamespace
            where n.nspname = 'public'
              and c.relkind = 'i'
              and c.relname = 'media_items_storage_object_key_key'
        ) then
            alter index public.media_items_r2_object_key_key
                rename to media_items_storage_object_key_key;
        end if;
    end if;
end;
$$;

comment on column public.media_items.storage_bucket is
    'Private object-storage bucket that contains the media object.';
comment on column public.media_items.storage_object_key is
    'Provider-neutral object key for the media object.';
