from __future__ import annotations

import asyncio

import pytest

from app.config import ConfigurationError
from app.main import Application


class FakeHealthServer:
    def __init__(self, fail_start: bool = False) -> None:
        self.fail_start = fail_start
        self.started = 0
        self.stopped = 0

    async def start(self) -> None:
        if self.fail_start:
            raise RuntimeError("health server failed")
        self.started += 1

    async def stop(self) -> None:
        self.stopped += 1


class FakeService:
    def __init__(self, *, configured: bool = True, fail_init: bool = False) -> None:
        self.configured = configured
        self.fail_init = fail_init
        self.init_attempts = 0
        self.closed = 0

    def initialize(self) -> bool:
        self.init_attempts += 1
        if self.fail_init:
            raise RuntimeError("service initialization failed")
        return self.configured

    async def close(self) -> None:
        self.closed += 1


class FakeBot:
    def __init__(
        self,
        *,
        fail_start: bool = False,
        on_start: object | None = None,
    ) -> None:
        self.fail_start = fail_start
        self.on_start = on_start
        self.token: str | None = None
        self.started = False
        self.closed = 0
        self._stop = asyncio.Event()

    async def start(self, token: str) -> None:
        self.token = token
        self.started = True
        if callable(self.on_start):
            self.on_start()
        if self.fail_start:
            raise RuntimeError("discord authentication failed")
        await self._stop.wait()

    async def close(self) -> None:
        self.closed += 1
        self._stop.set()


def build_app(make_settings, **overrides):
    settings = make_settings()
    health = overrides.pop("health_server", FakeHealthServer())
    database = overrides.pop("database", FakeService())
    storage = overrides.pop("storage", FakeService())
    bot = overrides.pop("bot", FakeBot())
    app = Application(
        settings,
        bot_factory=lambda: bot,
        database=database,
        storage=storage,
        health_server=health,
        install_signal_handlers=False,
        **overrides,
    )
    return app, health, database, storage, bot


async def test_start_and_stop_close_all_started_resources(make_settings) -> None:
    app, health, database, storage, bot = build_app(make_settings)

    await app.start()
    await asyncio.sleep(0)

    assert health.started == 1
    assert database.init_attempts == 1
    assert storage.init_attempts == 1
    assert bot.started is True
    assert bot.token == "test-discord-token"

    await app.stop()
    await app.stop()

    assert bot.closed == 1
    assert database.closed == 1
    assert storage.closed == 1
    assert health.stopped == 1


async def test_run_returns_cleanly_when_shutdown_is_requested(make_settings) -> None:
    app, health, database, storage, bot = build_app(make_settings)
    bot.on_start = app.request_shutdown

    await app.run()

    assert bot.started is True
    assert bot.closed == 1
    assert database.closed == 1
    assert storage.closed == 1
    assert health.stopped == 1


async def test_bot_start_failure_cleans_up_everything(make_settings) -> None:
    bot = FakeBot(fail_start=True)
    app, health, database, storage, _bot = build_app(make_settings, bot=bot)

    with pytest.raises(RuntimeError, match="discord"):
        await app.run()

    assert bot.closed == 1
    assert database.closed == 1
    assert storage.closed == 1
    assert health.stopped == 1


async def test_health_start_failure_does_not_start_dependencies_or_bot(make_settings) -> None:
    health = FakeHealthServer(fail_start=True)
    app, _health, database, storage, bot = build_app(make_settings, health_server=health)

    with pytest.raises(RuntimeError, match="health"):
        await app.start()

    assert database.init_attempts == 0
    assert storage.init_attempts == 0
    assert bot.started is False
    assert database.closed == 1
    assert storage.closed == 1
    assert health.stopped == 1


async def test_optional_service_initialization_failure_keeps_process_running(make_settings) -> None:
    database = FakeService(configured=True, fail_init=True)
    storage = FakeService(configured=False)
    app, _health, _database, _storage, bot = build_app(
        make_settings,
        database=database,
        storage=storage,
    )

    await app.start()
    await asyncio.sleep(0)

    assert database.init_attempts == 1
    assert storage.init_attempts == 1
    assert bot.started is True

    await app.stop()


async def test_missing_discord_token_fails_before_health_server_starts(make_settings) -> None:
    settings = make_settings(DISCORD_TOKEN=None)
    health = FakeHealthServer()
    app = Application(
        settings,
        bot_factory=FakeBot,
        database=FakeService(),
        storage=FakeService(),
        health_server=health,
        install_signal_handlers=False,
    )

    with pytest.raises(ConfigurationError, match="DISCORD_TOKEN"):
        await app.start()

    assert health.started == 0


async def test_partial_service_configuration_fails_before_health_server_starts(
    make_settings,
) -> None:
    settings = make_settings(SUPABASE_URL="https://example.supabase.co")
    health = FakeHealthServer()
    app = Application(
        settings,
        bot_factory=FakeBot,
        database=FakeService(),
        storage=FakeService(),
        health_server=health,
        install_signal_handlers=False,
    )

    with pytest.raises(ConfigurationError, match="SUPABASE_SERVICE_ROLE_KEY"):
        await app.start()

    assert health.started == 0


async def test_partial_b2_configuration_fails_before_health_server_starts(
    make_settings,
) -> None:
    settings = make_settings(B2_ENDPOINT_URL="https://s3.us-west-004.backblazeb2.com")
    health = FakeHealthServer()
    app = Application(
        settings,
        bot_factory=FakeBot,
        database=FakeService(),
        storage=FakeService(),
        health_server=health,
        install_signal_handlers=False,
    )

    with pytest.raises(ConfigurationError, match="B2_REGION"):
        await app.start()

    assert health.started == 0


async def test_unconfigured_b2_storage_does_not_prevent_startup(make_settings) -> None:
    settings = make_settings()
    health = FakeHealthServer()
    bot = FakeBot()
    app = Application(
        settings,
        bot_factory=lambda: bot,
        health_server=health,
        install_signal_handlers=False,
    )

    await app.start()
    await asyncio.sleep(0)

    assert health.started == 1
    assert bot.started is True

    await app.stop()

    assert bot.closed == 1
    assert health.stopped == 1
