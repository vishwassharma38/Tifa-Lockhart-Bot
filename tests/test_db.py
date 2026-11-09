from __future__ import annotations

import asyncio
import threading

import httpx
import pytest

from app.db.client import DatabaseInitializationError, SupabaseDatabase
from app.db.repository import MediaRepository
from app.health.status import DependencyStatus, ServiceState
from app.status import StatusService


class FakeResponse:
    data = [{"id": 1}]


class FakeQuery:
    def __init__(self) -> None:
        self.selected: list[str] = []
        self.limited: int | None = None
        self.executed = False

    def select(self, *columns: str) -> FakeQuery:
        self.selected = list(columns)
        return self

    def eq(self, _column: str, _value: object) -> FakeQuery:
        return self

    def limit(self, count: int) -> FakeQuery:
        self.limited = count
        return self

    def execute(self) -> FakeResponse:
        self.executed = True
        return FakeResponse()


class FakeClient:
    def __init__(self) -> None:
        self.tables: list[str] = []
        self.query = FakeQuery()
        self.closed = False

    def table(self, name: str) -> FakeQuery:
        self.tables.append(name)
        return self.query

    async def aclose(self) -> None:
        self.closed = True


class FailingClient:
    def table(self, _name: str) -> FakeQuery:
        raise RuntimeError("query failed")


class FakeFactory:
    def __init__(self, client: object) -> None:
        self.client = client
        self.calls: list[tuple[object, ...]] = []

    def __call__(self, *args: object) -> object:
        self.calls.append(args)
        return self.client


async def immediate_thread(function, *args, **kwargs):
    return function(*args, **kwargs)


async def test_unconfigured_database_reports_not_configured() -> None:
    factory = FakeFactory(FakeClient())
    database = SupabaseDatabase(
        "",
        "",
        client_factory=factory,
        to_thread=immediate_thread,
    )

    assert database.configured is False
    assert database.initialize() is False
    status = await database.verify()

    assert status.state is ServiceState.NOT_CONFIGURED
    assert factory.calls == []


async def test_configured_database_initializes_once_and_verifies() -> None:
    client = FakeClient()
    factory = FakeFactory(client)
    database = SupabaseDatabase(
        "https://example.supabase.co",
        "service-role-key",
        client_factory=factory,
        to_thread=immediate_thread,
    )

    assert database.configured is True
    assert database._verify_timeout == 5.0
    assert database.initialize() is True
    assert database.initialize() is True
    status = await database.verify()

    assert status.state is ServiceState.VERIFIED
    assert len(factory.calls) == 1
    assert client.tables == ["runtime_state"]
    assert client.query.selected == ["id"]
    assert client.query.limited == 1
    assert client.query.executed is True
    assert client.closed is False


async def test_initialization_failure_is_secret_free_and_unavailable() -> None:
    def factory(*_args: object) -> object:
        raise RuntimeError("bad credentials: service-role-key")

    database = SupabaseDatabase(
        "https://example.supabase.co",
        "service-role-key",
        client_factory=factory,
        to_thread=immediate_thread,
    )

    with pytest.raises(DatabaseInitializationError) as exc_info:
        database.initialize()
    assert "service-role-key" not in str(exc_info.value)

    status = await database.verify()
    assert status.state is ServiceState.UNAVAILABLE


async def test_query_failure_is_reported_as_unavailable() -> None:
    factory = FakeFactory(FailingClient())
    database = SupabaseDatabase(
        "https://example.supabase.co",
        "service-role-key",
        client_factory=factory,
        to_thread=immediate_thread,
    )
    database.initialize()

    status = await database.verify()

    assert status.state is ServiceState.UNAVAILABLE


async def test_close_releases_and_closes_the_client() -> None:
    client = FakeClient()
    database = SupabaseDatabase(
        "https://example.supabase.co",
        "service-role-key",
        client_factory=FakeFactory(client),
        to_thread=immediate_thread,
    )
    database.initialize()

    await database.close()
    await database.close()

    assert client.closed is True


class FakeHttpClient:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def test_initialize_injects_http_client_options_into_supabase() -> None:
    http_client = FakeHttpClient()
    factory = FakeFactory(FakeClient())
    database = SupabaseDatabase(
        "https://example.supabase.co",
        "service-role-key",
        client_factory=factory,
        http_client_factory=lambda: http_client,
    )

    assert database.initialize() is True

    assert len(factory.calls) == 1
    url, key, options = factory.calls[0]
    assert url == "https://example.supabase.co"
    assert key == "service-role-key"
    assert options.httpx_client is http_client


async def test_initialize_builds_and_injects_a_shared_http1_transport(monkeypatch) -> None:
    real_client = httpx.Client
    constructor_options: dict[str, object] = {}

    def capture_client_construction(**kwargs):
        constructor_options.update(kwargs)
        return real_client(**kwargs)

    monkeypatch.setattr("app.db.client.httpx.Client", capture_client_construction)
    database = SupabaseDatabase("https://example.supabase.co", "service-role-key")
    try:
        assert database.initialize() is True

        http_client = database._http_client
        assert isinstance(http_client, real_client)
        assert constructor_options == {
            "http2": False,
            "timeout": 30.0,
            "follow_redirects": True,
        }
        assert http_client.timeout == httpx.Timeout(30.0)
        assert http_client.follow_redirects is True
        assert database.client.postgrest.session is http_client
    finally:
        await database.close()


async def test_repository_reuses_one_http_client_for_multiple_operations() -> None:
    http_client = FakeHttpClient()
    factory = FakeFactory(FakeClient())
    database = SupabaseDatabase(
        "https://example.supabase.co",
        "service-role-key",
        client_factory=factory,
        http_client_factory=lambda: http_client,
        to_thread=immediate_thread,
    )
    repository = MediaRepository(database, to_thread=immediate_thread)

    await repository.get_media_by_sha256("abc")
    await repository.get_media_by_sha256("def")

    assert len(factory.calls) == 1
    assert factory.calls[0][2].httpx_client is http_client

    await database.close()
    assert http_client.closed is True


async def test_repository_and_status_queries_share_one_global_four_slot_limit(
    make_settings,
) -> None:
    client = FakeClient()
    lock = threading.Lock()
    active = 0
    maximum = 0
    four_workers_entered = threading.Event()
    release_workers = threading.Event()
    all_calls_started = asyncio.Event()
    started_calls = 0

    def controlled_execute() -> FakeResponse:
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
            if active == 4:
                four_workers_entered.set()
        release_workers.wait(timeout=3)
        with lock:
            active -= 1
        return FakeResponse()

    client.query.execute = controlled_execute  # type: ignore[method-assign]
    database = SupabaseDatabase(
        "https://example.supabase.co",
        "service-role-key",
        client_factory=FakeFactory(client),
        max_concurrent_requests=4,
        verify_timeout=2,
    )
    repository = MediaRepository(database)

    class NoopStorage:
        async def verify(self, timeout: float | None = None) -> DependencyStatus:
            return DependencyStatus.not_configured()

    status_service = StatusService(make_settings(), database, NoopStorage(), check_timeout=2)

    async def track_call(call):
        nonlocal started_calls
        started_calls += 1
        if started_calls == 6:
            all_calls_started.set()
        return await call

    calls = [track_call(repository.get_runtime_state()) for _ in range(3)] + [
        track_call(status_service.collect(discord_connected=True)) for _ in range(3)
    ]
    tasks = [asyncio.create_task(call) for call in calls]
    try:
        assert await asyncio.wait_for(asyncio.to_thread(four_workers_entered.wait, 2), timeout=3)
        await asyncio.wait_for(all_calls_started.wait(), timeout=2)
        assert maximum == 4
    finally:
        release_workers.set()

    results = await asyncio.gather(*tasks)
    assert maximum == 4
    assert all(result.supabase.state is ServiceState.VERIFIED for result in results[3:])
    await database.close()


async def test_timed_out_health_check_keeps_global_slot_until_worker_finishes() -> None:
    client = FakeClient()
    first_worker_entered = threading.Event()
    release_first_worker = threading.Event()
    second_worker_entered = threading.Event()
    calls = 0
    lock = threading.Lock()

    def controlled_execute() -> FakeResponse:
        nonlocal calls
        with lock:
            calls += 1
            call_number = calls
        if call_number == 1:
            first_worker_entered.set()
            release_first_worker.wait(timeout=3)
        else:
            second_worker_entered.set()
        return FakeResponse()

    client.query.execute = controlled_execute  # type: ignore[method-assign]
    database = SupabaseDatabase(
        "https://example.supabase.co",
        "service-role-key",
        client_factory=FakeFactory(client),
        max_concurrent_requests=1,
        verify_timeout=0.05,
    )
    repository = MediaRepository(database)
    assert database._verify_timeout == 0.05

    try:
        status = await database.verify()
        assert status.state is ServiceState.UNAVAILABLE
        assert await asyncio.to_thread(first_worker_entered.wait, 1)

        next_operation = asyncio.create_task(repository.get_runtime_state())
        await asyncio.sleep(0)  # Let the queued operation reach the shared limiter.
        assert not second_worker_entered.is_set()

        release_first_worker.set()
        await asyncio.wait_for(next_operation, timeout=2)
        assert second_worker_entered.is_set()
    finally:
        release_first_worker.set()
        await database.close()


def test_initialization_failure_closes_the_owned_http_client() -> None:
    http_client = FakeHttpClient()

    def factory(*_args: object) -> object:
        raise RuntimeError("bad credentials: service-role-key")

    database = SupabaseDatabase(
        "https://example.supabase.co",
        "service-role-key",
        client_factory=factory,
        http_client_factory=lambda: http_client,
    )

    with pytest.raises(DatabaseInitializationError):
        database.initialize()

    assert http_client.closed is True
    assert database._http_client is None
