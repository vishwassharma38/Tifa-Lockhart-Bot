from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.bot.client import CRAWL_COOLDOWN, TifaBot


class DummyStatusService:
    async def collect(self, **_kwargs: object) -> object:
        raise AssertionError("status service must not be called")


class GateCrawler:
    def __init__(self) -> None:
        self.calls = 0
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def run_once(self, *, scheduled: bool = True) -> None:
        assert scheduled is True
        self.calls += 1
        self.started.set()
        await self.release.wait()


def make_bot(make_settings, now: list[datetime]) -> TifaBot:
    bot = TifaBot(make_settings(), DummyStatusService(), sync_commands=False)
    bot._crawl_now = lambda: now[0]
    bot.scheduler = AsyncIOScheduler(timezone=ZoneInfo("Asia/Kolkata"))
    bot.scheduler.start()
    return bot


async def test_normal_completion_schedules_one_crawl_after_full_cooldown(make_settings) -> None:
    now = [datetime(2030, 1, 1, tzinfo=UTC)]
    bot = make_bot(make_settings, now)
    crawler = GateCrawler()
    bot.crawler = crawler
    try:
        first = asyncio.create_task(bot._scheduled_crawl_processing())
        await crawler.started.wait()

        # A direct duplicate trigger cannot start or queue another crawl.
        await bot._scheduled_crawl_processing()
        assert crawler.calls == 1

        now[0] += timedelta(minutes=2)
        crawler.release.set()
        await first

        next_run = now[0] + CRAWL_COOLDOWN
        assert bot._crawler_cooldown_until == next_run
        job = bot.scheduler.get_job("crawl_processing")
        assert job is not None
        assert job.trigger.run_date == next_run

        # Triggers during cooldown are dropped, including the Bing event path.
        now[0] = next_run - timedelta(microseconds=1)
        await bot._scheduled_crawl_processing()
        await bot._scheduled_bing_discovery()
        assert crawler.calls == 1
        assert bot.scheduler.get_job("crawl_processing").trigger.run_date == next_run

        now[0] = next_run
        await bot._scheduled_crawl_processing()
        assert crawler.calls == 2
        assert bot._crawler_cooldown_until == next_run + CRAWL_COOLDOWN
        assert len([job for job in bot.scheduler.get_jobs() if job.id == "crawl_processing"]) == 1
    finally:
        await bot.close()


async def test_long_running_crawl_does_not_overlap_and_cooldown_starts_on_finish(
    make_settings,
) -> None:
    now = [datetime(2030, 1, 1, tzinfo=UTC)]
    bot = make_bot(make_settings, now)
    crawler = GateCrawler()
    bot.crawler = crawler
    try:
        first = asyncio.create_task(bot._scheduled_crawl_processing())
        await crawler.started.wait()

        now[0] += timedelta(minutes=5)
        await bot._scheduled_crawl_processing()
        now[0] += timedelta(minutes=5)
        await bot._scheduled_bing_discovery()
        assert crawler.calls == 1

        crawler.release.set()
        await first
        next_run = now[0] + CRAWL_COOLDOWN
        assert bot._crawler_cooldown_until == next_run

        now[0] = next_run - timedelta(seconds=1)
        await bot._scheduled_crawl_processing()
        assert crawler.calls == 1

        now[0] = next_run
        await bot._scheduled_crawl_processing()
        assert crawler.calls == 2
    finally:
        await bot.close()


async def test_failed_crawl_still_gets_completion_based_cooldown(make_settings) -> None:
    now = [datetime(2030, 1, 1, tzinfo=UTC)]

    class FailingCrawler:
        def __init__(self) -> None:
            self.calls = 0

        async def run_once(self, *, scheduled: bool = True) -> None:
            self.calls += 1
            raise RuntimeError("crawl failed")

    bot = make_bot(make_settings, now)
    crawler = FailingCrawler()
    bot.crawler = crawler
    try:
        await bot._scheduled_crawl_processing()
        cooldown_until = now[0] + CRAWL_COOLDOWN
        assert bot._crawler_cooldown_until == cooldown_until

        now[0] = cooldown_until - timedelta(microseconds=1)
        await bot._scheduled_crawl_processing()
        assert crawler.calls == 1

        now[0] = cooldown_until
        await bot._scheduled_crawl_processing()
        assert crawler.calls == 2
        assert bot._crawler_cooldown_until == cooldown_until + CRAWL_COOLDOWN
    finally:
        await bot.close()


async def test_cancellation_waits_for_crawler_cleanup_before_starting_cooldown(
    make_settings,
) -> None:
    now = [datetime(2030, 1, 1, tzinfo=UTC)]
    started = asyncio.Event()
    cleanup_started = asyncio.Event()
    cleanup_release = asyncio.Event()

    class CancellationAwareCrawler:
        async def run_once(self, *, scheduled: bool = True) -> None:
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cleanup_started.set()
                await cleanup_release.wait()
                raise

    bot = make_bot(make_settings, now)
    bot.crawler = CancellationAwareCrawler()
    try:
        task = asyncio.create_task(bot._scheduled_crawl_processing())
        await started.wait()
        task.cancel()
        await cleanup_started.wait()

        assert not task.done()
        assert bot._crawler_cooldown_until is None
        assert bot._crawler_active is True

        now[0] += timedelta(minutes=2)
        cleanup_release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert bot._crawler_active is False
        assert bot._crawler_cooldown_until == now[0] + CRAWL_COOLDOWN
    finally:
        cleanup_release.set()
        await bot.close()


async def test_shutdown_cancels_and_joins_crawler_without_rescheduling(make_settings) -> None:
    now = [datetime(2030, 1, 1, tzinfo=UTC)]
    started = asyncio.Event()
    cleanup_started = asyncio.Event()
    cleanup_release = asyncio.Event()

    class CancellationAwareCrawler:
        async def run_once(self, *, scheduled: bool = True) -> None:
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cleanup_started.set()
                await cleanup_release.wait()
                raise

    bot = make_bot(make_settings, now)
    bot.crawler = CancellationAwareCrawler()
    task = asyncio.create_task(bot._scheduled_crawl_processing())
    await started.wait()
    try:
        closing = asyncio.create_task(bot.close())
        await cleanup_started.wait()
        assert not closing.done()

        cleanup_release.set()
        await closing
        with pytest.raises(asyncio.CancelledError):
            await task

        assert bot._shutting_down is True
        assert bot._crawler_cooldown_until is None
        assert bot._active_background_tasks == set()
    finally:
        cleanup_release.set()
        if not bot.is_closed():
            await bot.close()
