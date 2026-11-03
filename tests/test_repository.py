from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.db.client import SupabaseDatabase
from app.db.repository import MediaRepository, RepositoryError
from app.reddit.models import RedditCandidate


def make_candidate(**overrides: object) -> RedditCandidate:
    values = {
        "submission_id": "abc123",
        "permalink": "https://www.reddit.com/r/Tifa/comments/abc123/a_post",
        "source_url": "https://www.reddit.com/r/Tifa/comments/abc123/a_post",
        "title": "Tifa at the bar",
        "body": "Original artwork",
        "subreddit": "Tifa",
        "author_name": "artist_name",
        "author_profile_url": "https://www.reddit.com/user/artist_name",
        "created_at": datetime(2024, 1, 1, tzinfo=UTC),
        "media_url": "https://i.redd.it/example.png",
    }
    values.update(overrides)
    return RedditCandidate(**values)


class FakeResponse:
    def __init__(self, data: list[dict[str, object]]) -> None:
        self.data = data


class FakeQuery:
    def __init__(self, table: FakeTable, operation: str) -> None:
        self.table = table
        self.operation = operation
        self.filters: list[tuple[str, object]] = []
        self.limit_value: int | None = None

    def eq(self, column: str, value: object) -> FakeQuery:
        self.filters.append((column, value))
        return self

    def limit(self, value: int) -> FakeQuery:
        self.limit_value = value
        return self

    def execute(self) -> FakeResponse:
        return FakeResponse(self.table.results.get(self.operation, []))


class FakeTable:
    def __init__(self, name: str) -> None:
        self.name = name
        self.results: dict[str, list[dict[str, object]]] = {}
        self.queries: list[FakeQuery] = []
        self.calls: list[tuple[str, object, object]] = []

    def select(self, *columns: str) -> FakeQuery:
        self.calls.append(("select", columns, None))
        query = FakeQuery(self, "select")
        self.queries.append(query)
        return query

    def insert(self, payload: dict[str, object]) -> FakeQuery:
        self.calls.append(("insert", payload, None))
        query = FakeQuery(self, "insert")
        self.queries.append(query)
        return query

    def upsert(
        self,
        payload: dict[str, object],
        *,
        on_conflict: str | None = None,
    ) -> FakeQuery:
        self.calls.append(("upsert", payload, on_conflict))
        query = FakeQuery(self, "upsert")
        self.queries.append(query)
        return query

    def update(self, payload: dict[str, object]) -> FakeQuery:
        self.calls.append(("update", payload, None))
        query = FakeQuery(self, "update")
        self.queries.append(query)
        return query


class FakeClient:
    def __init__(self) -> None:
        self.tables: dict[str, FakeTable] = {}

    def table(self, name: str) -> FakeTable:
        return self.tables.setdefault(name, FakeTable(name))


async def immediate_thread(function, *args, **kwargs):
    return function(*args, **kwargs)


def make_repository(client: FakeClient) -> MediaRepository:
    database = SupabaseDatabase(
        "https://example.supabase.co",
        "service-role-key",
        client_factory=lambda *_args: client,
    )
    return MediaRepository(database, to_thread=immediate_thread)


async def test_get_media_by_sha256_returns_first_matching_row() -> None:
    client = FakeClient()
    client.table("media_items").results["select"] = [{"id": "media-1", "sha256": "abc"}]
    repository = make_repository(client)

    row = await repository.get_media_by_sha256("abc")

    assert row == {"id": "media-1", "sha256": "abc"}
    query = client.table("media_items").queries[0]
    assert query.filters == [("sha256", "abc")]
    assert query.limit_value == 1


async def test_get_media_by_sha256_returns_none_when_missing() -> None:
    repository = make_repository(FakeClient())

    assert await repository.get_media_by_sha256("missing") is None


async def test_upsert_discovery_records_truthful_attribution_and_metadata() -> None:
    client = FakeClient()
    client.table("discoveries").results["upsert"] = [{"id": "discovery-1"}]
    repository = make_repository(client)
    candidate = make_candidate(
        media_metadata={"width": 800},
        extraction_reason="ok",
    )

    row = await repository.upsert_discovery(candidate)

    assert row == {"id": "discovery-1"}
    operation, payload, on_conflict = client.table("discoveries").calls[0]
    assert operation == "upsert"
    assert on_conflict == "normalized_url"
    assert payload["url"] == candidate.source_url
    assert payload["normalized_url"] == candidate.normalized_source_url
    assert payload["source_platform_id"] == "abc123"
    assert payload["creator_name"] == "artist_name"
    assert payload["creator_profile_url"] == "https://www.reddit.com/user/artist_name"
    assert payload["metadata_json"]["width"] == 800
    assert payload["metadata_json"]["media_url"] == "https://i.redd.it/example.png"
    assert payload["metadata_json"]["extraction_reason"] == "ok"


async def test_upsert_discovery_falls_back_to_select_when_upsert_returns_no_row() -> None:
    client = FakeClient()
    client.table("discoveries").results["upsert"] = []
    client.table("discoveries").results["select"] = [{"id": "existing-discovery"}]
    repository = make_repository(client)

    row = await repository.upsert_discovery(make_candidate())

    assert row == {"id": "existing-discovery"}
    select_query = client.table("discoveries").queries[1]
    assert select_query.filters == [("normalized_url", make_candidate().normalized_source_url)]


async def test_create_media_item_inserts_exact_payload() -> None:
    client = FakeClient()
    client.table("media_items").results["insert"] = [{"id": "media-1"}]
    repository = make_repository(client)
    payload = {"sha256": "abc", "download_status": "stored"}

    row = await repository.create_media_item(payload)

    assert row == {"id": "media-1"}
    assert client.table("media_items").calls == [("insert", payload, None)]


async def test_record_source_upserts_reddit_attribution() -> None:
    client = FakeClient()
    client.table("media_sources").results["upsert"] = [{"id": "source-1"}]
    repository = make_repository(client)
    candidate = make_candidate()

    row = await repository.record_source(
        "media-1",
        candidate,
        discovery_id="discovery-1",
        media_url="https://i.redd.it/example.png",
    )

    assert row == {"id": "source-1"}
    operation, payload, on_conflict = client.table("media_sources").calls[0]
    assert operation == "upsert"
    assert on_conflict == "media_item_id,normalized_source_url"
    assert payload["media_item_id"] == "media-1"
    assert payload["discovery_id"] == "discovery-1"
    assert payload["source_post_id"] == "abc123"
    assert payload["source_url"] == candidate.source_url
    assert payload["media_url"] == "https://i.redd.it/example.png"
    assert payload["creator_name"] == "artist_name"
    assert payload["caption"] == "Original artwork"


async def test_create_publication_starts_pending() -> None:
    client = FakeClient()
    client.table("publications").results["insert"] = [{"id": "publication-1"}]
    repository = make_repository(client)

    row = await repository.create_publication("media-1", 999, "test_discovery")

    assert row == {"id": "publication-1"}
    _, payload, _ = client.table("publications").calls[0]
    assert payload == {
        "media_item_id": "media-1",
        "channel_id": 999,
        "purpose": "test_discovery",
        "status": "pending",
        "attempt_count": 1,
    }


async def test_update_publication_records_message_and_published_time() -> None:
    client = FakeClient()
    client.table("publications").results["update"] = [{"id": "publication-1"}]
    repository = make_repository(client)

    row = await repository.update_publication(
        "publication-1",
        status="published",
        message_id=555,
    )

    assert row == {"id": "publication-1"}
    _, payload, _ = client.table("publications").calls[0]
    assert payload["status"] == "published"
    assert payload["message_id"] == 555
    assert "published_at" in payload
    query = client.table("publications").queries[0]
    assert query.filters == [("id", "publication-1")]


async def test_update_publication_records_failure_without_published_time() -> None:
    client = FakeClient()
    repository = make_repository(client)

    await repository.update_publication(
        "publication-1",
        status="failed",
        last_error="publication_failed",
    )

    _, payload, _ = client.table("publications").calls[0]
    assert payload["status"] == "failed"
    assert payload["last_error"] == "publication_failed"
    assert "published_at" not in payload


async def test_unconfigured_database_fails_clearly() -> None:
    database = SupabaseDatabase("", "")
    repository = MediaRepository(database, to_thread=immediate_thread)

    with pytest.raises(RepositoryError):
        await repository.get_media_by_sha256("abc")
