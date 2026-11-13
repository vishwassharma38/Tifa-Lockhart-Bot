from __future__ import annotations

import asyncio
from zoneinfo import ZoneInfo

import pytest

from app.bot import client as bot_client
from app.bot.client import TifaBot
from app.discovery.bing import BingDiscoveryService, BingSearchAdapter


class DummyStatusService:
    async def collect(self, **_kwargs: object) -> object:
        raise AssertionError("status service must not be called during setup")


class FakeBotRepository:
    def __init__(self, state: dict[str, object] | None = None) -> None:
        self.state = state or {"collection_enabled": False}
        self.interrupted_calls = 0
        self.recovery_calls = 0

    async def mark_interrupted_jobs(self) -> None:
        self.interrupted_calls += 1

    async def recover_processing_discoveries(self) -> int:
        self.recovery_calls += 1
        return 0

    async def get_runtime_state(self) -> dict[str, object]:
        return dict(self.state)

    async def record_source(self, *_args: object, **_kwargs: object) -> dict[str, object]:
        return {}


class FakeDigestService:
    def __init__(
        self,
        *,
        catch_up_required: bool = False,
        catch_up_error: Exception | None = None,
        run_error: Exception | None = None,
    ) -> None:
        self.catch_up = catch_up_required
        self.catch_up_error = catch_up_error
        self.run_error = run_error
        self.catch_up_calls = 0
        self.run_calls: list[str] = []

    async def catch_up_required(self) -> bool:
        self.catch_up_calls += 1
        if self.catch_up_error is not None:
            raise self.catch_up_error
        return self.catch_up

    async def run(self, *, trigger: str) -> None:
        self.run_calls.append(trigger)
        if self.run_error is not None:
            raise self.run_error


class FakeBingService:
    def __init__(self) -> None:
        self.run_calls: list[bool] = []

    async def run(self, *, scheduled: bool = True) -> None:
        self.run_calls.append(scheduled)


class FakeCrawler:
    def __init__(self) -> None:
        self.run_calls: list[bool] = []

    async def run_once(self, *, scheduled: bool = True) -> None:
        self.run_calls.append(scheduled)


def build_bot(
    make_settings,
    *,
    sync_commands: bool,
    repository: object | None = None,
    storage: object | None = None,
) -> TifaBot:
    return TifaBot(
        make_settings(),
        DummyStatusService(),
        sync_commands=sync_commands,
        repository=repository,
        storage=storage,
    )


async def test_setup_hook_registers_phase_one_commands(make_settings) -> None:
    bot = build_bot(make_settings, sync_commands=False)
    try:
        await bot.setup_hook()
        names = {command.name for command in bot.tree.get_commands()}
        assert names == {"about", "status", "start", "stop", "save", "random", "stats"}
    finally:
        await bot.close()


async def test_setup_hook_syncs_globally_when_no_guild_is_configured(
    make_settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[int | None] = []

    async def fake_sync(_tree: object, guild_id: int | None) -> int:
        calls.append(guild_id)
        return 2

    monkeypatch.setattr(bot_client, "sync_application_commands", fake_sync)
    bot = build_bot(make_settings, sync_commands=True)
    try:
        await bot.setup_hook()
    finally:
        await bot.close()

    assert calls == [None]


async def test_setup_hook_sync_uses_guild_when_configured(
    make_settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[int | None] = []

    async def fake_sync(_tree: object, guild_id: int | None) -> int:
        calls.append(guild_id)
        return 2

    monkeypatch.setattr(bot_client, "sync_application_commands", fake_sync)
    bot = build_bot(
        lambda: make_settings(DISCORD_GUILD_ID="123456789"),
        sync_commands=True,
    )
    try:
        await bot.setup_hook()
    finally:
        await bot.close()

    assert calls == [123456789]


async def test_setup_hook_survives_command_sync_failure(
    make_settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def failing_sync(_tree: object, _guild_id: int | None) -> int:
        raise RuntimeError("discord command sync failed")

    monkeypatch.setattr(bot_client, "sync_application_commands", failing_sync)
    bot = build_bot(make_settings, sync_commands=True)
    try:
        await bot.setup_hook()
        names = {command.name for command in bot.tree.get_commands()}
        assert names == {"about", "status", "start", "stop", "save", "random", "stats"}
    finally:
        await bot.close()


async def test_setup_hook_registers_daily_digest_at_four_ist(make_settings) -> None:
    repository = FakeBotRepository()
    bot = build_bot(
        make_settings,
        sync_commands=False,
        repository=repository,
        storage=object(),
    )
    try:
        await bot.setup_hook()
        assert bot.scheduler is not None
        assert bot.digest_service is not None
        assert repository.interrupted_calls == 1
        job = bot.scheduler.get_job("daily_digest")
        assert job is not None
        trigger = job.trigger
        assert "hour='4'" in str(trigger)
        assert "minute='0'" in str(trigger)
        assert getattr(trigger, "timezone", None) == ZoneInfo("Asia/Kolkata")
        assert job.coalesce is True
        assert job.max_instances == 1
        assert job.misfire_grace_time == 3600
        assert bot.scheduler.get_job("reddit_collection_hourly") is not None
    finally:
        await bot.close()


async def test_setup_hook_registers_bing_discovery_with_shared_client(make_settings) -> None:
    repository = FakeBotRepository()
    bot = build_bot(
        make_settings,
        sync_commands=False,
        repository=repository,
        storage=object(),
    )
    try:
        await bot.setup_hook()
        assert bot.scheduler is not None
        assert isinstance(bot.bing_service, BingDiscoveryService)
        assert isinstance(bot.bing_service._adapter, BingSearchAdapter)
        assert bot.safe_http_client is not None
        assert bot.bing_service._adapter._client is bot.safe_http_client
        job = bot.scheduler.get_job("bing_discovery")
        assert job is not None
        assert job.trigger.interval.total_seconds() == 8 * 60 * 60
        assert job.coalesce is True
        assert job.max_instances == 1
        assert job.misfire_grace_time == 900
    finally:
        await bot.close()


async def test_scheduled_bing_discovery_runs_search_then_crawl(make_settings) -> None:
    bot = build_bot(make_settings, sync_commands=False)
    bing = FakeBingService()
    crawler = FakeCrawler()
    bot.bing_service = bing
    bot.crawler = crawler
    try:
        await bot._scheduled_bing_discovery()
    finally:
        await bot.close()

    assert bing.run_calls == [True]
    assert crawler.run_calls == [True]


async def test_scheduled_crawl_failure_does_not_take_the_bot_offline(make_settings) -> None:
    class FailingCrawler:
        def __init__(self) -> None:
            self.calls = 0

        async def run_once(self, *, scheduled: bool = True) -> None:
            self.calls += 1
            raise RuntimeError("crawl persistence failed")

    bot = build_bot(make_settings, sync_commands=False)
    crawler = FailingCrawler()
    bot.crawler = crawler
    try:
        await bot._scheduled_crawl_processing()
        assert not bot.is_closed()
        await bot._scheduled_crawl_processing()
        assert not bot.is_closed()
        assert crawler.calls == 1

        cooldown_until = bot._crawler_cooldown_until
        assert cooldown_until is not None
        bot._crawl_now = lambda: cooldown_until
        await bot._scheduled_crawl_processing()
        assert not bot.is_closed()
    finally:
        await bot.close()

    assert crawler.calls == 2


async def test_scheduled_digest_runs_while_collection_is_paused(make_settings) -> None:
    repository = FakeBotRepository({"collection_enabled": False})
    bot = build_bot(
        make_settings,
        sync_commands=False,
        repository=repository,
        storage=object(),
    )
    digest = FakeDigestService()
    try:
        await bot.setup_hook()
        bot.digest_service = digest
        assert repository.state["collection_enabled"] is False
        await bot._scheduled_digest()
        assert digest.run_calls == ["scheduled"]
    finally:
        await bot.close()


async def test_startup_catch_up_runs_once_when_due(make_settings) -> None:
    bot = build_bot(make_settings, sync_commands=False)
    digest = FakeDigestService(catch_up_required=True)
    bot.digest_service = digest
    try:
        await bot.on_ready()
        await asyncio.gather(*list(bot._active_background_tasks))
        await bot.on_ready()
        await asyncio.gather(*list(bot._active_background_tasks))
    finally:
        await bot.close()

    assert digest.catch_up_calls == 1
    assert digest.run_calls == ["startup_catch_up"]


async def test_startup_catch_up_is_skipped_when_not_due(make_settings) -> None:
    bot = build_bot(make_settings, sync_commands=False)
    digest = FakeDigestService(catch_up_required=False)
    bot.digest_service = digest
    try:
        await bot.on_ready()
        await asyncio.gather(*list(bot._active_background_tasks))
    finally:
        await bot.close()

    assert digest.catch_up_calls == 1
    assert digest.run_calls == []


async def test_startup_catch_up_failure_does_not_escape(make_settings) -> None:
    bot = build_bot(make_settings, sync_commands=False)
    digest = FakeDigestService(
        catch_up_required=True,
        run_error=RuntimeError("digest unavailable"),
    )
    bot.digest_service = digest
    try:
        await bot.on_ready()
        await asyncio.gather(*list(bot._active_background_tasks))
    finally:
        await bot.close()

    assert digest.run_calls == ["startup_catch_up"]


async def test_startup_catch_up_detection_failure_does_not_escape(make_settings) -> None:
    bot = build_bot(make_settings, sync_commands=False)
    digest = FakeDigestService(catch_up_error=RuntimeError("state unavailable"))
    bot.digest_service = digest
    try:
        await bot.on_ready()
        await asyncio.gather(*list(bot._active_background_tasks))
    finally:
        await bot.close()

    assert digest.catch_up_calls == 1
    assert digest.run_calls == []


async def test_scheduled_digest_failure_does_not_escape(make_settings) -> None:
    bot = build_bot(make_settings, sync_commands=False)
    digest = FakeDigestService(run_error=RuntimeError("digest unavailable"))
    bot.digest_service = digest
    try:
        await bot._scheduled_digest()
    finally:
        await bot.close()

    assert digest.run_calls == ["scheduled"]
