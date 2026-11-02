-- Tifa Lockhart Bot - harden the updated_at trigger function.
-- Keep the function independent of caller-controlled schemas.
-- This matches the hardening already applied to the live Supabase project.

create or replace function public.set_tifa_updated_at()
returns trigger
language plpgsql
set search_path = ''
as $function$
begin
    new.updated_at = pg_catalog.now();
    return new;
end;
$function$;
