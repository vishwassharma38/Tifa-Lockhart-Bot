from __future__ import annotations

import httpx
import pytest

from app.db.client import DatabaseInitializationError, SupabaseDatabase
from app.db.repository import MediaRepository
from app.health.status import ServiceState


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


async def test_initialize_builds_a_shared_http1_transport() -> None:
    database = SupabaseDatabase("https://example.supabase.co", "service-role-key")
    try:
        assert database.initialize() is True

        http_client = database._http_client
        assert isinstance(http_client, httpx.Client)
        assert http_client.timeout == httpx.Timeout(30.0)
        assert http_client._transport._pool._http2 is False
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
