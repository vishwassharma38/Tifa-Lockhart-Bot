-- Tifa Lockhart Bot - Phase 2 controlled discovery publication purpose.
-- Apply after 003. This is additive and keeps existing publication rows valid.

do $$
begin
    if to_regclass('public.publications') is not null then
        alter table public.publications
            drop constraint if exists publications_purpose_check;
        alter table public.publications
            add constraint publications_purpose_check
            check (purpose in ('daily_digest', 'manual_save', 'random', 'test_discovery'));
    end if;
end;
$$;
