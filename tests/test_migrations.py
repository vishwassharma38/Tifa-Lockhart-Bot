from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "migrations"

REQUIRED_TABLES = {
    "discoveries",
    "discovery_edges",
    "media_items",
    "media_sources",
    "publications",
    "job_runs",
    "runtime_state",
    "digest_state",
}


def read_migrations() -> tuple[list[Path], str]:
    files = sorted(MIGRATIONS.glob("*.sql"))
    return files, "\n".join(path.read_text(encoding="utf-8") for path in files)


def read_migration(name: str) -> str:
    return (MIGRATIONS / name).read_text(encoding="utf-8")


def test_migrations_are_named_and_ordered() -> None:
    files, _sql = read_migrations()

    assert files, "at least one migration must be checked in"
    for index, path in enumerate(files, start=1):
        assert re.fullmatch(r"\d{3}_[a-z0-9_]+\.sql", path.name), path.name
        assert int(path.name[:3]) == index


def test_required_tables_are_created() -> None:
    _files, sql = read_migrations()

    for table in REQUIRED_TABLES:
        assert re.search(
            rf"create table if not exists public\.{table}\b",
            sql,
            flags=re.IGNORECASE,
        ), table


def test_sha256_is_unique_in_postgres() -> None:
    _files, sql = read_migrations()

    assert re.search(r"constraint media_items_sha256_key unique \(sha256\)", sql, re.I)
    assert "sha256 ~ '^[0-9a-f]{64}$'" in sql


def test_foreign_keys_and_indexes_are_present() -> None:
    _files, sql = read_migrations()

    assert "references public.discoveries(id)" in sql
    assert "references public.media_items(id)" in sql
    assert "idx_discoveries_status" in sql
    assert "idx_media_items_created_at" in sql
    assert "idx_publications_status" in sql
    assert "idx_job_runs_started_at" in sql


def test_digest_publication_idempotency_index_exists() -> None:
    _files, sql = read_migrations()

    assert "uq_publications_digest_item_channel" in sql
    assert "where purpose = 'daily_digest'" in sql


def test_global_digest_reservation_migration_is_additive_and_atomic() -> None:
    sql = read_migration("008_global_digest_budget.sql").lower()
    assert "add column if not exists digest_cycle" in sql
    assert "add column if not exists digest_slot_reserved" in sql
    assert "uq_publications_digest_item_global" in sql
    assert "reserve_daily_digest_publication" in sql
    assert "for update" in sql
    assert "asia/kolkata" in sql
    assert "drop table" not in sql
    assert "delete from" not in sql


def test_digest_claims_retry_fairness_and_rollover_migration_is_fenced() -> None:
    sql = read_migration("009_digest_claims_retry_fairness.sql").lower()
    for column in (
        "delivery_claim_token",
        "delivery_claim_expires_at",
        "next_attempt_at",
        "retry_schedule_count",
    ):
        assert f"add column if not exists {column}" in sql
    assert "claim_daily_digest_publication" in sql
    assert "renew_daily_digest_claim" in sql
    assert "advance_daily_digest_attempt" in sql
    assert "delivery_claim_token = p_claim_token" in sql
    assert "delivery_claim_expires_at > v_now" in sql
    assert "attempt_count = greatest(0, p_expected_attempt_count)" in sql
    assert "rollover_reconciled_digest_publication" in sql
    assert "attempt_count > 0" in sql
    assert "grant execute" in sql and "to service_role" in sql
    assert "drop table" not in sql
    assert "delete from" not in sql


def test_runtime_and_digest_singletons_are_seeded() -> None:
    _files, sql = read_migrations()

    assert "insert into public.runtime_state (id)" in sql
    assert "insert into public.digest_state (id)" in sql
    assert "on conflict (id) do nothing" in sql


def test_rls_is_enabled_for_every_table() -> None:
    _files, sql = read_migrations()

    for table in REQUIRED_TABLES:
        assert f"alter table public.{table} enable row level security;" in sql


def test_schema_uses_utc_timestamps_and_no_media_binary_columns() -> None:
    _files, sql = read_migrations()
    lowered = sql.lower()

    assert sql.lower().count("timestamptz") >= 20
    assert "bytea" not in lowered
    assert "blob" not in lowered
    assert "gen_random_uuid()" in sql


def test_migration_is_not_destructive() -> None:
    _files, sql = read_migrations()

    assert not re.search(r"\bdrop\s+table\b", sql, re.I)
    assert not re.search(r"\btruncate\b", sql, re.I)
    assert not re.search(r"\bdelete\s+from\b", sql, re.I)
    assert "on delete cascade" not in sql.lower()


def test_media_size_ceiling_is_enforced() -> None:
    _files, sql = read_migrations()

    assert "size_bytes between 1 and 10000000" in sql
    assert "media_items_stored_check" in sql


def test_stored_object_key_is_content_addressed() -> None:
    _files, sql = read_migrations()

    assert "r2_object_key ~ '^assets/[0-9a-f]{2}/[0-9a-f]{64}\\.[a-z0-9]+$'" in sql
    assert "r2_object_key like 'assets/' || left(sha256, 2) || '/' || sha256 || '.%'" in sql


def test_provider_neutral_storage_migration_is_safe_and_present() -> None:
    files, _sql = read_migrations()
    names = [path.name for path in files]

    assert "001_initial_schema.sql" in names
    assert "002_provider_neutral_storage_columns.sql" in names
    assert "003_harden_updated_at_function_search_path.sql" in names
    assert "004_phase2_test_discovery_publication.sql" in names

    sql = read_migration("002_provider_neutral_storage_columns.sql")
    lowered = sql.lower()

    assert "rename column r2_bucket to storage_bucket" in lowered
    assert "rename column r2_object_key to storage_object_key" in lowered
    assert "rename constraint media_items_r2_object_key_key" in lowered
    assert "media_items_storage_object_key_key" in lowered
    assert "drop table" not in lowered
    assert "drop column" not in lowered
    assert "delete from" not in lowered
    assert "truncate" not in lowered


def test_phase3_per_asset_source_provenance_is_additive() -> None:
    files, _sql = read_migrations()
    names = [path.name for path in files]

    assert "005_phase3_per_asset_source_provenance.sql" in names

    sql = read_migration("005_phase3_per_asset_source_provenance.sql")
    lowered = sql.lower()

    assert "add column if not exists source_media_id" in lowered
    assert "media_item_id, normalized_source_url, source_media_id" in lowered
    assert "source_platform" in lowered
    assert "source_post_id" in lowered
    assert "drop table" not in lowered
    assert "drop column" not in lowered
    assert "delete from" not in lowered
    assert "truncate" not in lowered


def test_phase4_migration_extends_existing_runtime_and_job_tables() -> None:
    sql = read_migration("006_phase4_reddit_collection.sql").lower()
    assert "add column if not exists collection_changed_at" in sql
    assert "add column if not exists discovered_count" in sql
    assert "add column if not exists examined_count" in sql
    assert "add column if not exists duplicate_count" in sql
    assert "add column if not exists failed_count" in sql
    assert "create table" not in sql


def test_phase5_migration_adds_durable_discovery_and_brave_controls() -> None:
    files, _sql = read_migrations()
    names = [path.name for path in files]

    assert "007_phase5_discovery_controls.sql" in names

    sql = read_migration("007_phase5_discovery_controls.sql")
    lowered = sql.lower()

    assert "add column if not exists next_attempt_at" in lowered
    assert "idx_discoveries_queue_claim" in lowered
    assert "add column if not exists brave_paused" in lowered
    assert "add column if not exists brave_pause_reason" in lowered
    assert "add column if not exists brave_paused_at" in lowered
    assert "create table" not in lowered
    assert "drop table" not in lowered
    assert "drop column" not in lowered
    assert "delete from" not in lowered
    assert "truncate" not in lowered


def test_updated_at_function_search_path_is_hardened() -> None:
    sql = read_migration("003_harden_updated_at_function_search_path.sql")

    assert "create or replace function public.set_tifa_updated_at()" in sql
    assert "set search_path = ''" in sql
    assert "pg_catalog.now()" in sql


def test_phase2_test_discovery_publication_purpose_is_additive() -> None:
    sql = read_migration("004_phase2_test_discovery_publication.sql")
    lowered = sql.lower()

    assert "test_discovery" in lowered
    assert "drop constraint if exists publications_purpose_check" in lowered
    assert "add constraint publications_purpose_check" in lowered
    assert "drop table" not in lowered
    assert "drop column" not in lowered
    assert "delete from" not in lowered
    assert "truncate" not in lowered
