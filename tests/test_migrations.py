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
