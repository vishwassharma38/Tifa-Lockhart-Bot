from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.db.client import SupabaseDatabase
from app.db.repository import MediaRepository, RepositoryError
from app.discovery.models import WebCandidate
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
    def __init__(self, data: list[dict[str, object]], count: int | None = None) -> None:
        self.data = data
        self.count = count


class FakeQuery:
    def __init__(self, table: FakeTable, operation: str, count: str | None = None) -> None:
        self.table = table
        self.operation = operation
        self.count = count
        self.filters: list[tuple[str, object]] = []
        self.limit_value: int | None = None
        self.or_expression: str | None = None
        self.or_expressions: list[str] = []
        self.order_value: tuple[str, bool] | None = None
        self.orders: list[tuple[str, bool]] = []
        self.range_value: tuple[int, int] | None = None

    def eq(self, column: str, value: object) -> FakeQuery:
        self.filters.append((column, value))
        return self

    def is_(self, column: str, value: object) -> FakeQuery:
        self.filters.append((f"is.{column}", value))
        return self

    def gte(self, column: str, value: object) -> FakeQuery:
        self.filters.append((f"gte.{column}", value))
        return self

    def lte(self, column: str, value: object) -> FakeQuery:
        self.filters.append((f"lte.{column}", value))
        return self

    def lt(self, column: str, value: object) -> FakeQuery:
        self.filters.append((f"lt.{column}", value))
        return self

    def in_(self, column: str, values: list[object]) -> FakeQuery:
        self.filters.append((f"in.{column}", list(values)))
        return self

    def or_(self, expression: str) -> FakeQuery:
        self.or_expression = expression
        self.or_expressions.append(expression)
        return self

    def order(
        self, column: str, *, desc: bool = False, nullsfirst: bool | None = None
    ) -> FakeQuery:
        self.orders.append((column, desc))
        self.order_value = (column, desc)
        return self

    def limit(self, value: int) -> FakeQuery:
        self.limit_value = value
        return self

    def range(self, start: int, end: int) -> FakeQuery:
        self.range_value = (start, end)
        return self

    def execute(self) -> FakeResponse:
        error = self.table.errors.get(self.operation)
        if error is not None:
            raise error
        queued = self.table.queued_results.get(self.operation)
        if queued:
            data = queued.pop(0)
        else:
            data = self.table.results.get(self.operation, [])
        count = self.table.counts.get(self.operation)
        queued_counts = self.table.queued_counts.get(self.operation)
        if self.count is not None:
            if queued_counts:
                count = queued_counts.pop(0)
            elif count is None:
                status_values = [value for column, value in self.filters if column == "status"]
                if status_values:
                    count = self.table.counts_by_status.get(str(status_values[0]), 0)
                else:
                    count = len(data)
        return FakeResponse(data, count=count)


class FakeTable:
    def __init__(self, name: str) -> None:
        self.name = name
        self.results: dict[str, list[dict[str, object]]] = {}
        self.errors: dict[str, Exception] = {}
        self.counts: dict[str, int] = {}
        self.counts_by_status: dict[str, int] = {}
        self.queued_results: dict[str, list[list[dict[str, object]]]] = {}
        self.queued_counts: dict[str, list[int]] = {}
        self.queries: list[FakeQuery] = []
        self.calls: list[tuple[str, object, object]] = []

    def select(self, *columns: str, count: str | None = None) -> FakeQuery:
        self.calls.append(("select", columns, None))
        query = FakeQuery(self, "select", count=count)
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
        self.rpc_calls: list[tuple[str, dict[str, object]]] = []
        self.rpc_data: object = {"id": "publication-1", "quota_exhausted": False}

    def table(self, name: str) -> FakeTable:
        return self.tables.setdefault(name, FakeTable(name))

    def rpc(self, name: str, params: dict[str, object]):
        self.rpc_calls.append((name, params))
        return FakeQueryResult(self.rpc_data)


class FakeQueryResult:
    def __init__(self, data: object) -> None:
        self.data = data

    def execute(self) -> FakeResponse:
        return FakeResponse(self.data)  # type: ignore[arg-type]


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


async def test_create_media_item_recovers_concurrent_duplicate() -> None:
    client = FakeClient()
    client.table("media_items").errors["insert"] = RuntimeError("duplicate key")
    client.table("media_items").results["select"] = [{"id": "existing-media", "sha256": "abc"}]
    repository = make_repository(client)

    row = await repository.create_media_item({"sha256": "abc", "download_status": "stored"})

    assert row == {"id": "existing-media", "sha256": "abc", "_recovered": True}
    assert client.table("media_items").queries[1].filters == [("sha256", "abc")]


async def test_create_media_item_reraises_when_recovery_finds_nothing() -> None:
    client = FakeClient()
    client.table("media_items").errors["insert"] = RuntimeError("database unavailable")
    repository = make_repository(client)

    with pytest.raises(RuntimeError, match="database unavailable"):
        await repository.create_media_item({"sha256": "abc"})


async def test_create_media_item_does_not_recover_non_conflict_errors() -> None:
    client = FakeClient()
    client.table("media_items").errors["insert"] = RuntimeError("database unavailable")
    client.table("media_items").results["select"] = [{"id": "existing-media", "sha256": "abc"}]
    repository = make_repository(client)

    with pytest.raises(RuntimeError, match="database unavailable"):
        await repository.create_media_item({"sha256": "abc"})


async def test_update_media_item_metadata_only_writes_present_safe_fields() -> None:
    client = FakeClient()
    client.table("media_items").results["update"] = [{"id": "media-1"}]
    repository = make_repository(client)

    row = await repository.update_media_item_metadata(
        "media-1",
        {
            "width": 800,
            "height": None,
            "duration_seconds": 3.5,
            "perceptual_hash": "abcd",
            "storage_bucket": "not-allowed",
        },
    )

    assert row == {"id": "media-1"}
    _, payload, _ = client.table("media_items").calls[0]
    assert payload == {
        "width": 800,
        "duration_seconds": 3.5,
        "perceptual_hash": "abcd",
    }
    assert client.table("media_items").queries[0].filters == [("id", "media-1")]


async def test_list_media_with_perceptual_hash_filters_empty_values() -> None:
    client = FakeClient()
    client.table("media_items").results["select"] = [
        {"id": "media-1", "perceptual_hash": "abcd"},
        {"id": "media-2", "perceptual_hash": None},
        {"id": "media-3"},
    ]
    repository = make_repository(client)

    rows = await repository.list_media_with_perceptual_hash(limit=10)

    assert rows == [{"id": "media-1", "perceptual_hash": "abcd"}]
    assert client.table("media_items").queries[0].limit_value == 10


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
        source_media_id="gallery-media-1",
    )

    assert row == {"id": "source-1"}
    operation, payload, on_conflict = client.table("media_sources").calls[0]
    assert operation == "upsert"
    assert on_conflict == "media_item_id,normalized_source_url,source_media_id"
    assert payload["media_item_id"] == "media-1"
    assert payload["discovery_id"] == "discovery-1"
    assert payload["source_post_id"] == "abc123"
    assert payload["source_media_id"] == "gallery-media-1"
    assert payload["source_url"] == candidate.source_url
    assert payload["media_url"] == "https://i.redd.it/example.png"
    assert payload["creator_name"] == "artist_name"
    assert payload["caption"] == "Original artwork"


async def test_sources_for_media_retrieves_every_association_in_stable_order() -> None:
    client = FakeClient()
    sources = [{"id": "source-1"}, {"id": "source-2"}]
    client.table("media_sources").results["select"] = sources
    repository = make_repository(client)

    result = await repository.sources_for_media("media-1")

    assert result == sources
    query = client.table("media_sources").queries[0]
    assert query.filters == [("media_item_id", "media-1")]
    assert query.orders == [("discovered_at", False), ("id", False)]
    assert query.range_value == (0, 999)


async def test_digest_reservation_uses_atomic_database_function() -> None:
    client = FakeClient()
    repository = make_repository(client)

    result = await repository.reserve_digest_publication("media-1", 123, "2026-11-08", limit=50)

    assert result == {"id": "publication-1", "quota_exhausted": False}
    assert client.rpc_calls == [
        (
            "reserve_daily_digest_publication",
            {
                "p_media_item_id": "media-1",
                "p_channel_id": 123,
                "p_digest_cycle": "2026-11-08",
                "p_limit": 50,
            },
        )
    ]


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


async def test_create_job_run_supports_phase5_job_types() -> None:
    client = FakeClient()
    client.table("job_runs").results["insert"] = [{"id": "job-1"}]
    repository = make_repository(client)

    job_id = await repository.create_job_run({"phrase": "Tifa"}, job_type="brave_discovery")

    assert job_id == "job-1"
    _, payload, _ = client.table("job_runs").calls[0]
    assert payload["job_type"] == "brave_discovery"
    assert payload["config_snapshot"] == {"phrase": "Tifa"}
    with pytest.raises(ValueError, match="Unsupported job type"):
        await repository.create_job_run({}, job_type="manual_save")


async def test_mark_interrupted_jobs_covers_worker_and_digest_jobs() -> None:
    client = FakeClient()
    client.table("job_runs").results["select"] = [{"id": "job-1"}]
    repository = make_repository(client)

    await repository.mark_interrupted_jobs()

    queries = [query for query in client.table("job_runs").queries if query.operation == "select"]
    assert [query.filters for query in queries] == [
        [("job_type", "reddit_collection"), ("outcome", "running")],
        [("job_type", "brave_discovery"), ("outcome", "running")],
        [("job_type", "crawl"), ("outcome", "running")],
        [("job_type", "daily_digest"), ("outcome", "running")],
    ]
    updates = [call for call in client.table("job_runs").calls if call[0] == "update"]
    assert len(updates) == 4
    assert all(
        payload["error_summary"] == "interrupted_by_restart"
        for _operation, payload, _conflict in updates
    )


async def test_brave_pause_state_is_persisted_and_clearable() -> None:
    client = FakeClient()
    client.table("runtime_state").results["update"] = [{"id": 1}]
    repository = make_repository(client)

    await repository.pause_brave("http_403_access_denied")
    pause_payload = client.table("runtime_state").calls[0][1]
    assert pause_payload["brave_paused"] is True
    assert pause_payload["brave_pause_reason"] == "http_403_access_denied"
    assert pause_payload["brave_paused_at"]

    await repository.clear_brave_pause()
    clear_payload = client.table("runtime_state").calls[1][1]
    assert clear_payload == {
        "brave_paused": False,
        "brave_pause_reason": None,
        "brave_paused_at": None,
    }

    await repository.mark_brave_success()
    success_payload = client.table("runtime_state").calls[2][1]
    assert "last_brave_success_at" in success_payload


async def test_operational_counts_include_queue_brave_and_pause_state() -> None:
    client = FakeClient()
    client.table("runtime_state").results["select"] = [
        {
            "collection_enabled": True,
            "brave_paused": True,
            "brave_pause_reason": "http_403_access_denied",
            "last_brave_success_at": "2026-01-01T00:00:00+00:00",
            "last_digest_success_at": None,
        }
    ]
    discoveries = client.table("discoveries")
    discoveries.counts_by_status = {
        "queued": 7,
        "processing": 1,
        "verified": 4,
        "dumpster": 3,
        "rejected": 2,
        "blocked": 1,
        "failed": 1,
        "oversize": 1,
    }
    repository = make_repository(client)

    counts = await repository.operational_counts()

    assert counts["queued"] == 7
    assert counts["processing"] == 1
    assert counts["verified"] == 4
    assert counts["dumpster"] == 3
    assert counts["brave_paused"] is True
    assert counts["brave_pause_reason"] == "http_403_access_denied"
    assert counts["last_brave_success_at"] == "2026-01-01T00:00:00+00:00"
    count_query = discoveries.queries[0]
    assert count_query.count == "exact"
    assert count_query.filters == [("status", "queued")]


async def test_generic_discovery_upsert_is_idempotent_by_normalized_url() -> None:
    existing = {"id": "discovery-1", "normalized_url": "https://example.com/a"}
    client = FakeClient()
    client.table("discoveries").results["select"] = [existing]
    repository = make_repository(client)

    row, created = await repository.upsert_discovery_record(
        {"url": "https://example.com/a", "normalized_url": "https://example.com/a"}
    )

    assert row == existing
    assert created is False
    assert not [call for call in client.table("discoveries").calls if call[0] == "insert"]


async def test_generic_discovery_upsert_inserts_new_normalized_url() -> None:
    client = FakeClient()
    client.table("discoveries").results["insert"] = [{"id": "discovery-2"}]
    repository = make_repository(client)

    row, created = await repository.upsert_discovery_record(
        {"url": "https://example.com/b", "normalized_url": "https://example.com/b"}
    )

    assert row == {"id": "discovery-2"}
    assert created is True
    insert = next(call for call in client.table("discoveries").calls if call[0] == "insert")
    assert insert[1]["normalized_url"] == "https://example.com/b"


async def test_claim_discoveries_filters_due_rows_and_uses_conditional_update() -> None:
    client = FakeClient()
    discoveries = client.table("discoveries")
    discoveries.results["select"] = [{"id": "discovery-1", "status": "queued", "depth": 2}]
    discoveries.results["update"] = [{"id": "discovery-1", "status": "processing", "depth": 2}]
    repository = make_repository(client)

    rows = await repository.claim_discoveries(limit=5, max_depth=6)

    assert rows[0]["status"] == "processing"
    select_query = discoveries.queries[0]
    assert ("status", "queued") in select_query.filters
    assert ("lte.depth", 6) in select_query.filters
    assert select_query.or_expression is not None
    assert "next_attempt_at.is.null" in select_query.or_expression
    assert select_query.order_value == ("discovered_at", False)
    assert select_query.limit_value == 5
    update_query = discoveries.queries[1]
    assert update_query.operation == "update"
    assert ("id", "discovery-1") in update_query.filters
    assert ("status", "queued") in update_query.filters


async def test_update_discovery_filters_fields_and_rejects_unknown_status() -> None:
    client = FakeClient()
    client.table("discoveries").results["update"] = [{"id": "discovery-1"}]
    repository = make_repository(client)

    await repository.update_discovery(
        "discovery-1",
        {
            "status": "verified",
            "http_status": 200,
            "metadata_json": {"title": "Tifa"},
            "not_allowed": "ignored",
        },
    )

    _, payload, _ = client.table("discoveries").calls[0]
    assert payload == {
        "status": "verified",
        "http_status": 200,
        "metadata_json": {"title": "Tifa"},
    }
    with pytest.raises(RepositoryError, match="Unsupported discovery status"):
        await repository.update_discovery("discovery-1", {"status": "unknown"})


async def test_record_discovery_edge_deduplicates_and_preserves_root_paths() -> None:
    client = FakeClient()
    edges = client.table("discovery_edges")
    edges.results["select"] = [{"id": "edge-1"}]
    repository = make_repository(client)

    existing = await repository.record_discovery_edge(
        parent_discovery_id=None,
        child_discovery_id="child-1",
        child_normalized_url="https://example.com/child",
        discovered_from_url="https://search.brave.com/search?q=tifa",
        depth=0,
    )

    assert existing == {"id": "edge-1"}
    root_query = edges.queries[0]
    assert ("is.parent_discovery_id", "null") in root_query.filters
    assert not [call for call in edges.calls if call[0] == "insert"]

    edges.results["select"] = []
    edges.results["insert"] = [{"id": "edge-2"}]
    created = await repository.record_discovery_edge(
        parent_discovery_id="parent-1",
        child_discovery_id="child-1",
        child_normalized_url="https://example.com/child",
        discovered_from_url="https://example.com/parent",
        depth=1,
    )
    assert created == {"id": "edge-2"}
    inserted = next(call for call in edges.calls if call[0] == "insert")
    assert inserted[1]["parent_discovery_id"] == "parent-1"
    assert inserted[1]["depth"] == 1


async def test_recover_processing_discoveries_requeues_interrupted_rows() -> None:
    client = FakeClient()
    discoveries = client.table("discoveries")
    discoveries.results["select"] = [{"id": "discovery-1"}, {"id": "discovery-2"}]
    discoveries.results["update"] = [{"id": "discovery-1"}]
    repository = make_repository(client)

    recovered = await repository.recover_processing_discoveries()

    assert recovered == 2
    assert discoveries.queries[0].filters == [("status", "processing")]
    updates = [call for call in discoveries.calls if call[0] == "update"]
    assert len(updates) == 2
    assert all(payload["status"] == "queued" for _operation, payload, _conflict in updates)
    assert all(
        payload["last_error"] == "interrupted_by_restart"
        for _operation, payload, _conflict in updates
    )


async def test_latest_phase5_runs_filter_job_type_and_order_descending() -> None:
    client = FakeClient()
    client.table("job_runs").results["select"] = [{"id": "run-1"}]
    repository = make_repository(client)

    brave = await repository.latest_brave_runs(2)
    crawl = await repository.latest_crawl_runs(1)

    assert brave == [{"id": "run-1"}]
    assert crawl == [{"id": "run-1"}]
    queries = client.table("job_runs").queries
    assert queries[0].filters == [("job_type", "brave_discovery")]
    assert queries[0].order_value == ("started_at", True)
    assert queries[0].limit_value == 2
    assert queries[1].filters == [("job_type", "crawl")]


async def test_web_candidate_reuses_existing_discovery_and_records_web_source() -> None:
    client = FakeClient()
    client.table("discoveries").results["select"] = [{"id": "discovery-1"}]
    client.table("media_sources").results["upsert"] = [{"id": "source-1"}]
    repository = make_repository(client)
    candidate = WebCandidate(
        page_url="https://example.com/tifa",
        title="Tifa fanart",
        author_name="artist",
        discovery_id="discovery-1",
    )

    discovery = await repository.upsert_discovery(candidate)
    source = await repository.record_source("media-1", candidate, source_media_id="web-1")

    assert discovery == {"id": "discovery-1"}
    assert not [call for call in client.table("discoveries").calls if call[0] == "upsert"]
    assert source == {"id": "source-1"}
    _, payload, _ = client.table("media_sources").calls[0]
    assert payload["source_platform"] == "web"
    assert payload["source_post_id"] == candidate.submission_id
    assert payload["source_media_id"] == "web-1"


async def test_ensure_digest_publication_creates_zero_attempt_pending_row() -> None:
    client = FakeClient()
    publications = client.table("publications")
    publications.queued_results["select"] = [[]]
    publications.results["insert"] = [
        {"id": "publication-1", "status": "pending", "attempt_count": 0}
    ]
    repository = make_repository(client)

    row = await repository.ensure_digest_publication("media-1", 222)

    assert row == {"id": "publication-1", "status": "pending", "attempt_count": 0}
    insert_payload = next(
        payload for operation, payload, _conflict in publications.calls if operation == "insert"
    )
    assert insert_payload == {
        "media_item_id": "media-1",
        "channel_id": 222,
        "purpose": "daily_digest",
        "status": "pending",
        "attempt_count": 0,
    }
    select_query = publications.queries[0]
    assert select_query.filters == [
        ("media_item_id", "media-1"),
        ("channel_id", 222),
        ("purpose", "daily_digest"),
    ]
    assert select_query.limit_value == 1


async def test_ensure_digest_publication_reuses_existing_row() -> None:
    existing = {"id": "publication-1", "status": "published", "attempt_count": 1}
    client = FakeClient()
    client.table("publications").results["select"] = [existing]
    repository = make_repository(client)

    row = await repository.ensure_digest_publication("media-1", 222)

    assert row == existing
    assert not [call for call in client.table("publications").calls if call[0] == "insert"]


async def test_ensure_digest_publication_recovers_unique_conflict() -> None:
    existing = {"id": "existing-publication", "status": "pending", "attempt_count": 1}
    client = FakeClient()
    publications = client.table("publications")
    publications.queued_results["select"] = [[], [existing]]
    publications.errors["insert"] = RuntimeError("duplicate key value violates unique constraint")
    repository = make_repository(client)

    row = await repository.ensure_digest_publication("media-1", 222)

    assert row == existing
    assert [query.operation for query in publications.queries] == ["select", "insert", "select"]


async def test_ensure_digest_publication_reraises_non_conflict_errors() -> None:
    client = FakeClient()
    publications = client.table("publications")
    publications.queued_results["select"] = [[]]
    publications.errors["insert"] = RuntimeError("database unavailable")
    repository = make_repository(client)

    with pytest.raises(RuntimeError, match="database unavailable"):
        await repository.ensure_digest_publication("media-1", 222)


async def test_record_publication_attempt_requires_and_uses_claim_token() -> None:
    client = FakeClient()
    client.rpc_data = {
        "id": "publication-1",
        "status": "pending",
        "attempt_count": 2,
        "attempt_advanced": True,
    }
    repository = make_repository(client)

    row = await repository.record_publication_attempt(
        "publication-1", expected_attempt_count=1, claim_token="claim-token"
    )

    assert row["attempt_count"] == 2
    assert client.rpc_calls == [
        (
            "advance_daily_digest_attempt",
            {
                "p_publication_id": "publication-1",
                "p_claim_token": "claim-token",
                "p_expected_attempt_count": 1,
                "p_attempt_limit": 3,
                "p_lease_seconds": 900,
            },
        )
    ]


async def test_record_publication_attempt_returns_no_claim_when_update_loses_race() -> None:
    client = FakeClient()
    client.rpc_data = {"attempt_advanced": False}
    repository = make_repository(client)

    row = await repository.record_publication_attempt(
        "publication-1", expected_attempt_count=1, claim_token="stale-token"
    )

    assert row is None
    assert client.rpc_calls[0][0] == "advance_daily_digest_attempt"


async def test_digest_claim_rpc_preserves_explicit_loser_result() -> None:
    client = FakeClient()
    client.rpc_data = {"claim_acquired": False}
    repository = make_repository(client)

    result = await repository.claim_digest_publication(
        "publication-1", "claim-token", lease_seconds=600
    )

    assert result == {"claim_acquired": False}
    assert client.rpc_calls == [
        (
            "claim_daily_digest_publication",
            {
                "p_publication_id": "publication-1",
                "p_claim_token": "claim-token",
                "p_lease_seconds": 600,
            },
        )
    ]


async def test_list_retryable_digest_publications_filters_unresolved_rows() -> None:
    client = FakeClient()
    publications = client.table("publications")
    publications.results["select"] = [{"id": "publication-1"}]
    repository = make_repository(client)

    rows = await repository.list_retryable_digest_publications(attempt_limit=3, limit=25)

    assert rows == [{"id": "publication-1"}]
    query = publications.queries[0]
    assert query.filters == [
        ("purpose", "daily_digest"),
        ("in.status", ["pending", "failed"]),
        ("lt.attempt_count", 3),
    ]
    assert query.or_expression is not None
    assert query.or_expression.startswith("next_attempt_at.is.null,next_attempt_at.lte.")
    assert query.orders == [
        ("next_attempt_at", False),
        ("created_at", False),
        ("id", False),
    ]
    assert query.limit_value == 25


async def test_list_exhausted_ambiguous_publications_reads_every_page() -> None:
    client = FakeClient()
    publications = client.table("publications")
    publications.queued_results["select"] = [
        [{"id": f"publication-{index}"} for index in range(100)],
        [{"id": f"publication-{index}"} for index in range(100, 200)],
        [{"id": f"publication-{index}"} for index in range(200, 207)],
    ]
    repository = make_repository(client)

    rows = await repository.list_exhausted_ambiguous_digest_publications(
        attempt_limit=3,
        page_size=100,
    )

    assert len(rows) == 207
    assert [query.range_value for query in publications.queries] == [
        (0, 99),
        (0, 99),
        (0, 99),
    ]
    assert all(
        query.orders == [("created_at", False), ("id", False)] for query in publications.queries
    )
    assert publications.queries[0].or_expressions[0] == (
        "status.eq.pending,last_error.like.discord_delivery_ambiguous:*,"
        "last_error.like.discord_delivery_failed*"
    )
    assert all(
        len(query.or_expressions) >= 2
        and query.or_expressions[1].startswith("next_attempt_at.is.null,next_attempt_at.lte.")
        for query in publications.queries
    )
    assert all(len(query.or_expressions) == 3 for query in publications.queries[1:])
    assert publications.queries[1].or_expressions[2].startswith("created_at.gt.")


async def test_record_reconciliation_diagnostic_preserves_ambiguous_error() -> None:
    client = FakeClient()
    publications = client.table("publications")
    publications.results["update"] = [{"id": "publication-1"}]
    repository = make_repository(client)

    await repository.record_publication_reconciliation_error(
        "publication-1",
        "delivery_unreconciled",
        preserve_error="discord_delivery_ambiguous:TimeoutError",
    )

    payload = next(
        payload for operation, payload, _conflict in publications.calls if operation == "update"
    )
    assert payload == {
        "last_error": "discord_delivery_ambiguous:TimeoutError;delivery_unreconciled"
    }


async def test_list_digest_candidates_filters_window_and_paginates_deterministically() -> None:
    client = FakeClient()
    media = client.table("media_items")
    media.results["select"] = [{"id": "media-1", "created_at": "2026-01-02T00:00:00+00:00"}]
    repository = make_repository(client)
    window_start = "2026-01-01T00:00:00+00:00"
    window_end = "2026-01-03T00:00:00+00:00"

    rows = await repository.list_digest_candidates(
        window_start=window_start,
        window_end=window_end,
        limit=50,
        offset=100,
    )

    assert rows == [{"id": "media-1", "created_at": "2026-01-02T00:00:00+00:00"}]
    query = media.queries[0]
    assert ("verification_status", "verified") in query.filters
    assert ("download_status", "stored") in query.filters
    assert ("gte.created_at", window_start) in query.filters
    assert ("lte.created_at", window_end) in query.filters
    assert query.orders == [("created_at", False), ("id", False)]
    assert query.range_value == (100, 149)


async def test_list_digest_candidates_uses_inclusive_resume_cursor() -> None:
    client = FakeClient()
    repository = make_repository(client)
    cursor = "2026-01-02T03:04:05+00:00"

    await repository.list_digest_candidates(
        window_start="2026-01-01T00:00:00+00:00",
        window_end="2026-01-03T00:00:00+00:00",
        cursor=cursor,
        limit=10,
    )

    query = client.table("media_items").queries[0]
    assert ("gte.created_at", cursor) in query.filters
    assert query.range_value == (0, 9)


async def test_digest_publication_counts_report_pending_retryable_and_exhausted() -> None:
    client = FakeClient()
    publications = client.table("publications")
    publications.queued_counts["select"] = [5, 2, 3]
    repository = make_repository(client)

    counts = await repository.digest_publication_counts(attempt_limit=3)

    assert counts == {"pending": 5, "failed_retryable": 2, "failed_exhausted": 3}
    queries = publications.queries
    assert all(query.count == "exact" for query in queries)
    assert ("purpose", "daily_digest") in queries[0].filters
    assert ("status", "pending") in queries[0].filters
    assert ("status", "failed") in queries[1].filters
    assert ("lt.attempt_count", 3) in queries[1].filters
    assert ("status", "failed") in queries[2].filters
    assert ("gte.attempt_count", 3) in queries[2].filters


async def test_digest_state_is_read_and_updated_through_existing_singleton() -> None:
    client = FakeClient()
    digest_state = client.table("digest_state")
    digest_state.results["select"] = [{"id": 1, "last_window_start": "2026-01-01T00:00:00+00:00"}]
    digest_state.results["update"] = [{"id": 1}]
    repository = make_repository(client)

    state = await repository.get_digest_state()
    updated = await repository.update_digest_state(
        {
            "last_window_start": "2026-01-02T00:00:00+00:00",
            "last_window_end": "2026-01-03T00:00:00+00:00",
            "last_completed_at": None,
            "id": 9,
            "updated_at": "ignored",
        }
    )

    assert state["last_window_start"] == "2026-01-01T00:00:00+00:00"
    assert updated == {"id": 1}
    update_payload = next(
        payload for operation, payload, _conflict in digest_state.calls if operation == "update"
    )
    assert update_payload == {
        "last_window_start": "2026-01-02T00:00:00+00:00",
        "last_window_end": "2026-01-03T00:00:00+00:00",
    }
    assert digest_state.queries[0].filters == [("id", 1)]
    assert digest_state.queries[1].filters == [("id", 1)]


async def test_mark_digest_success_updates_runtime_state() -> None:
    client = FakeClient()
    client.table("runtime_state").results["update"] = [{"id": 1}]
    repository = make_repository(client)

    await repository.mark_digest_success()

    update_payload = client.table("runtime_state").calls[0][1]
    assert "last_digest_success_at" in update_payload
    assert client.table("runtime_state").queries[0].filters == [("id", 1)]


async def test_latest_digest_runs_uses_daily_digest_job_type() -> None:
    client = FakeClient()
    client.table("job_runs").results["select"] = [{"id": "run-1", "outcome": "partial"}]
    repository = make_repository(client)

    rows = await repository.latest_digest_runs(2)

    assert rows == [{"id": "run-1", "outcome": "partial"}]
    query = client.table("job_runs").queries[0]
    assert query.filters == [("job_type", "daily_digest")]
    assert query.order_value == ("started_at", True)
    assert query.limit_value == 2


async def test_mark_publication_published_requires_message_id_in_payload() -> None:
    client = FakeClient()
    client.table("publications").results["update"] = [{"id": "publication-1"}]
    repository = make_repository(client)

    await repository.mark_publication_published("publication-1", 555)

    payload = client.table("publications").calls[0][1]
    assert payload["status"] == "published"
    assert payload["message_id"] == 555
    assert payload["published_at"]
    assert payload["last_error"] is None
