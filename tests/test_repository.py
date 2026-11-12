from __future__ import annotations

import asyncio
import logging
import threading
from contextlib import suppress
from datetime import UTC, datetime

import httpx
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
        ignore_duplicates: bool = False,
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


def make_database(client: FakeClient) -> SupabaseDatabase:
    return SupabaseDatabase(
        "https://example.supabase.co",
        "service-role-key",
        client_factory=lambda *_args: client,
    )


def make_repository(client: FakeClient) -> MediaRepository:
    return MediaRepository(make_database(client), to_thread=immediate_thread)


class FailNTimesThread:
    """Raise a transport error for the first N dispatches, then execute normally."""

    def __init__(self, failures: int) -> None:
        self.remaining = failures
        self.calls = 0

    async def __call__(self, function, *args, **kwargs):
        self.calls += 1
        if self.remaining > 0:
            self.remaining -= 1
            raise httpx.ReadError("connection reset by peer")
        return function(*args, **kwargs)


class AlwaysFailsThread:
    def __init__(self, error: Exception) -> None:
        self.error = error
        self.calls = 0

    async def __call__(self, function, *args, **kwargs):
        self.calls += 1
        raise self.error


class CommitThenDuplicateThread:
    """Model a committed insert whose first response was lost by the client."""

    def __init__(self, client: FakeClient) -> None:
        self.client = client
        self.calls = 0

    async def __call__(self, function, *args, **kwargs):
        self.calls += 1
        if self.calls == 1:
            error = RuntimeError("duplicate key")
            error.code = "23505"  # type: ignore[attr-defined]
            self.client.table("media_items").errors["insert"] = error
            raise httpx.ReadError("response lost after commit")
        return function(*args, **kwargs)


def capturing_sleep(delays: list[float]):
    async def sleep(delay: float) -> None:
        delays.append(delay)

    return sleep


def make_retrying_repository(
    client: FakeClient,
    thread: object,
    *,
    delays: list[float] | None = None,
    **kwargs: object,
) -> MediaRepository:
    return MediaRepository(
        make_database(client),
        to_thread=thread,  # type: ignore[arg-type]
        retry_sleep=capturing_sleep(delays if delays is not None else []),
        jitter=lambda _low, _high: 0.0,
        **kwargs,
    )


async def test_transient_transport_error_retries_then_succeeds() -> None:
    client = FakeClient()
    client.table("media_items").results["select"] = [{"id": "media-1"}]
    thread = FailNTimesThread(2)
    delays: list[float] = []
    repository = make_retrying_repository(client, thread, delays=delays)

    row = await repository.get_media_by_sha256("abc")

    assert row == {"id": "media-1"}
    assert thread.calls == 3
    assert delays == [0.5, 1.0]


async def test_transient_transport_error_stops_after_three_attempts(caplog) -> None:
    thread = AlwaysFailsThread(httpx.ReadError("socket failure"))
    repository = make_retrying_repository(FakeClient(), thread)

    with caplog.at_level(logging.WARNING):
        with pytest.raises(httpx.ReadError, match="socket failure"):
            await repository.get_media_by_sha256("abc")

    assert thread.calls == 3
    assert "attempt=1/3" in caplog.text
    assert "attempt=3/3" in caplog.text
    assert "get_media_by_sha256" in caplog.text
    assert "service-role-key" not in caplog.text


async def test_permanent_exception_is_not_retried() -> None:
    thread = AlwaysFailsThread(ValueError("invalid query"))
    repository = make_retrying_repository(FakeClient(), thread)

    with pytest.raises(ValueError, match="invalid query"):
        await repository.get_media_by_sha256("abc")

    assert thread.calls == 1


async def test_repeat_safe_update_retries_and_plain_insert_does_not() -> None:
    client = FakeClient()
    thread = FailNTimesThread(1)
    repository = make_retrying_repository(client, thread)

    await repository.finish_job_run("run-1", {"stored": 3}, "succeeded")
    assert thread.calls == 2
    assert len(client.table("job_runs").calls) == 1

    insert_thread = AlwaysFailsThread(httpx.ReadError("response lost"))
    insert_repository = make_retrying_repository(FakeClient(), insert_thread)
    with pytest.raises(httpx.ReadError):
        await insert_repository.create_publication("media-1", 10, "daily_digest")
    assert insert_thread.calls == 1


async def test_media_insert_retries_only_when_unique_content_key_exists() -> None:
    hash_value = "a" * 64
    client = FakeClient()
    client.table("media_items").results["select"] = [{"id": "existing", "sha256": hash_value}]
    thread = CommitThenDuplicateThread(client)
    repository = make_retrying_repository(client, thread)
    recovered = await repository.create_media_item({"sha256": hash_value})

    assert recovered == {"id": "existing", "sha256": hash_value, "_recovered": True}
    assert thread.calls == 2

    unsafe_thread = AlwaysFailsThread(httpx.ReadError("response lost"))
    unsafe = make_retrying_repository(FakeClient(), unsafe_thread)
    with pytest.raises(httpx.ReadError):
        await unsafe.create_media_item({"url": "https://example.com/file"})
    assert unsafe_thread.calls == 1


async def test_concurrency_limit_bounds_operations_and_recovers_after_failure() -> None:
    active = 0
    maximum = 0
    lock = threading.Lock()
    gate = threading.Event()
    both_started = threading.Event()

    def operation(fail: bool = False) -> str:
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
            if active == 2:
                both_started.set()
        gate.wait(timeout=2)
        with lock:
            active -= 1
        if fail:
            raise ValueError("controlled failure")
        return "ok"

    database = SupabaseDatabase(
        "https://example.supabase.co", "service-role-key", max_concurrent_requests=2
    )
    repository = MediaRepository(database)
    tasks = [asyncio.create_task(repository._run(operation)) for _ in range(3)]
    assert await asyncio.wait_for(asyncio.to_thread(both_started.wait, 2), timeout=3)
    assert maximum == 2
    gate.set()
    results = await asyncio.gather(*tasks)
    assert results == ["ok", "ok", "ok"]

    with pytest.raises(ValueError, match="controlled failure"):
        await repository._run(lambda: (_ for _ in ()).throw(ValueError("controlled failure")))
    assert await repository._run(lambda: "recovered") == "recovered"


async def test_cancelled_caller_keeps_permit_until_thread_finishes() -> None:
    entered = threading.Event()
    release = threading.Event()
    second_entered = threading.Event()

    def slow() -> str:
        entered.set()
        release.wait(timeout=3)
        return "done"

    def second() -> str:
        second_entered.set()
        return "second"

    database = SupabaseDatabase(
        "https://example.supabase.co", "service-role-key", max_concurrent_requests=1
    )
    repository = MediaRepository(database)
    first = asyncio.create_task(repository._run(slow))
    assert await asyncio.to_thread(entered.wait, 2)
    first.cancel()
    second_task = asyncio.create_task(repository._run(second))
    await asyncio.sleep(0.05)
    assert not first.done()
    assert not second_entered.is_set()
    release.set()
    with suppress(asyncio.CancelledError):
        await first
    assert await asyncio.wait_for(second_task, timeout=2) == "second"
    assert second_entered.is_set()


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


async def test_digest_destination_retarget_uses_claim_fenced_rpc() -> None:
    client = FakeClient()
    client.rpc_data = {"claim_valid": True, "channel_id": 222}
    repository = make_repository(client)

    result = await repository.retarget_digest_publication("publication-1", "claim-token", 222)

    assert result == {"claim_valid": True, "channel_id": 222}
    assert client.rpc_calls == [
        (
            "retarget_daily_digest_publication",
            {
                "p_publication_id": "publication-1",
                "p_claim_token": "claim-token",
                "p_channel_id": 222,
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

    job_id = await repository.create_job_run({"phrase": "Tifa"}, job_type="bing_discovery")

    assert job_id == "job-1"
    _, payload, _ = client.table("job_runs").calls[0]
    assert payload["job_type"] == "bing_discovery"
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
        [("job_type", "bing_discovery"), ("outcome", "running")],
        [("job_type", "crawl"), ("outcome", "running")],
        [("job_type", "daily_digest"), ("outcome", "running")],
    ]
    updates = [call for call in client.table("job_runs").calls if call[0] == "update"]
    assert len(updates) == 4
    assert all(
        payload["error_summary"] == "interrupted_by_restart"
        for _operation, payload, _conflict in updates
    )


async def test_bing_pause_state_is_persisted_and_clearable() -> None:
    client = FakeClient()
    client.table("runtime_state").results["update"] = [{"id": 1}]
    repository = make_repository(client)

    await repository.pause_bing("http_403_access_denied")
    pause_payload = client.table("runtime_state").calls[0][1]
    assert pause_payload["bing_paused"] is True
    assert pause_payload["bing_pause_reason"] == "http_403_access_denied"
    assert pause_payload["bing_paused_at"]

    await repository.clear_bing_pause()
    clear_payload = client.table("runtime_state").calls[1][1]
    assert clear_payload == {
        "bing_paused": False,
        "bing_pause_reason": None,
        "bing_paused_at": None,
    }

    await repository.mark_bing_success()
    success_payload = client.table("runtime_state").calls[2][1]
    assert "last_bing_success_at" in success_payload


async def test_operational_counts_include_queue_bing_and_pause_state() -> None:
    client = FakeClient()
    client.table("runtime_state").results["select"] = [
        {
            "collection_enabled": True,
            "bing_paused": True,
            "bing_pause_reason": "http_403_access_denied",
            "last_bing_success_at": "2026-01-01T00:00:00+00:00",
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
    assert counts["bing_paused"] is True
    assert counts["bing_pause_reason"] == "http_403_access_denied"
    assert counts["last_bing_success_at"] == "2026-01-01T00:00:00+00:00"
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


async def test_claim_discoveries_uses_atomic_fair_frontier_rpc() -> None:
    client = FakeClient()
    client.rpc_data = [{"id": "discovery-1", "status": "processing", "depth": 2}]
    repository = make_repository(client)

    rows = await repository.claim_discoveries(limit=25, max_depth=6, per_domain_limit=2)

    assert rows[0]["status"] == "processing"
    assert client.rpc_calls == [
        (
            "claim_crawl_discoveries",
            {
                "p_limit": 25,
                "p_max_depth": 6,
                "p_per_domain_limit": 2,
                "p_new_domain_share": 0.8,
            },
        )
    ]


async def test_crawl_frontier_summary_and_robots_policy_persistence() -> None:
    client = FakeClient()
    client.rpc_data = {
        "domains_discovered": "20",
        "domains_unexplored": 8,
        "domains_explored": 12,
        "domains_cooling_down": 2,
        "queued_domains": 7,
        "domains_visited_recently": 6,
    }
    client.table("crawl_origin_state").results["select"] = [
        {
            "robots_body": "User-agent: *\\nDisallow: /private",
            "robots_status": 200,
            "robots_category": "ok",
            "robots_expires_at": "2026-11-11T00:00:00+00:00",
        }
    ]
    repository = make_repository(client)

    summary = await repository.crawl_frontier_summary()
    policy = await repository.get_robots_policy("https://example.com")
    await repository.store_robots_policy(
        "https://example.com",
        "example.com",
        "example.com",
        {"robots_status": 200, "robots_category": "ok", "robots_body": ""},
    )

    assert summary["domains_discovered"] == 20
    assert summary["domains_unexplored"] == 8
    assert policy["robots_status"] == 200
    assert client.rpc_calls[-1] == ("crawl_frontier_summary", {"p_max_depth": 6})
    assert client.table("crawl_origin_state").queries[0].filters == [
        ("origin", "https://example.com")
    ]
    domain_upsert = next(
        call for call in client.table("crawl_domain_state").calls if call[0] == "upsert"
    )
    assert domain_upsert[1] == {
        "registrable_domain": "example.com",
        "hostnames": ["example.com"],
    }
    upsert = next(call for call in client.table("crawl_origin_state").calls if call[0] == "upsert")
    assert upsert[1]["registrable_domain"] == "example.com"


async def test_frontier_backfill_uses_stored_host_when_legacy_url_is_invalid() -> None:
    client = FakeClient()
    discoveries = client.table("discoveries")
    discoveries.queued_results["select"] = [
        [
            {
                "id": "00000000-0000-0000-0000-000000000001",
                "normalized_url": "http://localhost/private",
                "domain": "gallery.example.org",
            }
        ],
        [],
    ]
    client.rpc_data = 1
    repository = make_repository(client)

    updated = await repository.backfill_frontier_domains()

    assert updated == 1
    assert client.rpc_calls[0][0] == "backfill_crawl_domain_identities"
    item = client.rpc_calls[0][1]["p_items"][0]
    assert item["hostname"] == "gallery.example.org"
    assert item["registrable_domain"] == "example.org"


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
        "claimed_at": None,
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
        discovered_from_url="https://www.bing.com/search?q=tifa",
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
    client.rpc_data = 2
    repository = make_repository(client)

    recovered = await repository.recover_processing_discoveries()

    assert recovered == 2
    assert client.rpc_calls == [("recover_expired_crawl_claims", {"p_lease_seconds": 900})]


async def test_latest_phase5_runs_filter_job_type_and_order_descending() -> None:
    client = FakeClient()
    client.table("job_runs").results["select"] = [{"id": "run-1"}]
    repository = make_repository(client)

    bing = await repository.latest_bing_runs(2)
    crawl = await repository.latest_crawl_runs(1)

    assert bing == [{"id": "run-1"}]
    assert crawl == [{"id": "run-1"}]
    queries = client.table("job_runs").queries
    assert queries[0].filters == [("job_type", "bing_discovery")]
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
        "publication-1",
        expected_attempt_count=1,
        claim_token="claim-token",
        digest_cycle="2026-11-08",
    )

    assert row["attempt_count"] == 2
    assert client.rpc_calls == [
        (
            "advance_daily_digest_attempt_for_cycle",
            {
                "p_publication_id": "publication-1",
                "p_claim_token": "claim-token",
                "p_expected_attempt_count": 1,
                "p_digest_cycle": "2026-11-08",
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
        "publication-1",
        expected_attempt_count=1,
        claim_token="stale-token",
        digest_cycle="2026-11-08",
    )

    assert row is None
    assert client.rpc_calls[0][0] == "advance_daily_digest_attempt_for_cycle"


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
        ("digest_terminal", False),
    ]
    assert len(query.or_expressions) == 2
    assert query.or_expressions[0].startswith("next_attempt_at.is.null,next_attempt_at.lte.")
    assert query.or_expressions[1].startswith(
        "delivery_claim_token.is.null,delivery_claim_expires_at.is.null,"
    )
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
