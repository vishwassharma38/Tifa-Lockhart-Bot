"""discord.py client lifecycle, scheduler, and application command registration."""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from zoneinfo import ZoneInfo

import discord
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from discord.ext import commands

from app.bot.commands import PhaseFourCommands
from app.bot.publisher import DiscordPublisher
from app.config import Settings
from app.discovery.bing import BingDiscoveryService, BingSearchAdapter
from app.discovery.crawler import DiscoveryCrawler, WebSaveService
from app.discovery.http import SafeHttpClient
from app.jobs.digest import DigestService
from app.media.fetcher import MediaFetcher
from app.pipeline.phase2 import Phase2Pipeline
from app.reddit.collector import RedditCollector
from app.reddit.connector import RedditConnector
from app.status import StatusService

log = logging.getLogger(__name__)


async def sync_application_commands(tree: Any, guild_id: int | None) -> int:
    """Sync the current command tree once, deliberately choosing the scope."""
    if guild_id is not None:
        guild = discord.Object(id=guild_id)
        tree.copy_global_to(guild=guild)
        synced = await tree.sync(guild=guild)
        log.info("Synced %s application commands to the development guild", len(synced))
    else:
        synced = await tree.sync()
        log.info(
            "Synced %s application commands globally; propagation can take up to one hour",
            len(synced),
        )
    return len(synced)


class TifaBot(commands.Bot):
    """Discord gateway client with testable dependency injection."""

    def __init__(
        self,
        settings: Settings,
        status_service: StatusService,
        *,
        sync_commands: bool = True,
        repository: Any | None = None,
        storage: Any | None = None,
    ) -> None:
        intents = discord.Intents.default()
        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=intents,
            help_command=None,
        )
        self.settings = settings
        self.status_service = status_service
        self._sync_commands = sync_commands
        self.repository = repository
        self.storage = storage
        self.collector: RedditCollector | None = None
        self.pipeline: Phase2Pipeline | None = None
        self.bing_service: BingDiscoveryService | None = None
        self.crawler: DiscoveryCrawler | None = None
        self.web_save_service: WebSaveService | None = None
        self.safe_http_client: SafeHttpClient | None = None
        self.digest_service: DigestService | None = None
        self.scheduler: AsyncIOScheduler | None = None
        self._active_collection_task: asyncio.Task[Any] | None = None
        self._active_background_tasks: set[asyncio.Task[Any]] = set()
        self._digest_catch_up_started = False

    async def setup_hook(self) -> None:
        await self.add_cog(PhaseFourCommands(self))
        if self.repository is not None and self.storage is not None:
            try:
                await self.repository.mark_interrupted_jobs()
                try:
                    recovered = await self.repository.recover_processing_discoveries()
                    if recovered:
                        log.info("Recovered %s interrupted discovery record(s)", recovered)
                except Exception as exc:
                    log.warning("Discovery recovery unavailable (%s)", type(exc).__name__)
                connector = RedditConnector(self.settings)
                fetcher = MediaFetcher()
                pipeline = Phase2Pipeline(
                    settings=self.settings,
                    connector=connector,
                    fetcher=fetcher,
                    repository=self.repository,
                    storage=self.storage,
                    publisher=DiscordPublisher(
                        self,
                        self.settings.saves_channel_id or self.settings.discoveries_channel_id or 0,
                    ),
                    publication_purpose="manual_save",
                )
                self.pipeline = pipeline
                self.collector = RedditCollector(
                    self.settings, self.repository, pipeline, connector
                )
                self.safe_http_client = SafeHttpClient(
                    max_concurrent_requests=self.settings.max_concurrent_requests,
                    max_redirects=self.settings.max_redirects,
                    connect_timeout=self.settings.crawl_connect_timeout_seconds,
                    read_timeout=self.settings.crawl_read_timeout_seconds,
                )
                self.bing_service = BingDiscoveryService(
                    self.settings,
                    self.repository,
                    BingSearchAdapter(self.settings, client=self.safe_http_client),
                )
                self.crawler = DiscoveryCrawler(
                    self.settings,
                    self.repository,
                    pipeline,
                    client=self.safe_http_client,
                )
                self.web_save_service = WebSaveService(
                    self.settings,
                    self.repository,
                    pipeline,
                    client=self.safe_http_client,
                )
                self.digest_service = DigestService(
                    settings=self.settings,
                    repository=self.repository,
                    storage=self.storage,
                    publisher=DiscordPublisher(
                        self,
                        self.settings.discoveries_channel_id or 0,
                    ),
                )
                state = await self.repository.get_runtime_state()
                self.scheduler = AsyncIOScheduler(timezone=ZoneInfo("Asia/Kolkata"))
                self.scheduler.add_job(
                    self._scheduled_collection,
                    "cron",
                    minute=0,
                    id="reddit_collection_hourly",
                    replace_existing=True,
                    coalesce=True,
                    max_instances=1,
                    misfire_grace_time=900,
                )
                self.scheduler.add_job(
                    self._scheduled_bing_discovery,
                    "interval",
                    hours=self.settings.bing_search_interval_hours,
                    id="bing_discovery",
                    replace_existing=True,
                    coalesce=True,
                    max_instances=1,
                    misfire_grace_time=900,
                )
                self.scheduler.add_job(
                    self._scheduled_crawl_processing,
                    "interval",
                    minutes=5,
                    id="crawl_processing",
                    replace_existing=True,
                    coalesce=True,
                    max_instances=1,
                    misfire_grace_time=300,
                )
                self.scheduler.add_job(
                    self._scheduled_digest,
                    "cron",
                    hour=4,
                    minute=0,
                    id="daily_digest",
                    replace_existing=True,
                    coalesce=True,
                    max_instances=1,
                    misfire_grace_time=3600,
                )
                self.scheduler.start()
                log.info(
                    "Scheduler initialized; persisted collection=%s Bing interval=%s hours",
                    bool(state.get("collection_enabled")),
                    self.settings.bing_search_interval_hours,
                )
            except Exception as exc:
                log.warning("Scheduler unavailable (%s)", type(exc).__name__)
        if not self._sync_commands:
            return
        try:
            await sync_application_commands(self.tree, self.settings.discord_guild_id)
        except Exception as exc:
            log.warning(
                "Application command sync failed (%s); commands may remain unavailable "
                "until the next successful sync",
                type(exc).__name__,
            )

    async def on_ready(self) -> None:
        user = self.user
        log.info(
            "Connected to Discord as %s (id=%s) in %s guild(s)",
            user,
            user.id if user else "unknown",
            len(self.guilds),
        )
        if self.digest_service is not None and not self._digest_catch_up_started:
            self._digest_catch_up_started = True
            task = asyncio.create_task(
                self._run_startup_digest_catch_up(),
                name="digest-startup-catch-up",
            )
            self._active_background_tasks.add(task)
            task.add_done_callback(self._digest_catch_up_done)

    async def _scheduled_collection(self) -> None:
        if self.collector is not None:
            current = asyncio.current_task()
            self._active_collection_task = current
            if current is not None:
                self._active_background_tasks.add(current)
            try:
                await self.collector.run(scheduled=True)
            finally:
                if current is not None:
                    self._active_background_tasks.discard(current)
                self._active_collection_task = None

    async def _scheduled_bing_discovery(self) -> None:
        current = asyncio.current_task()
        if current is not None:
            self._active_background_tasks.add(current)
        try:
            if self.bing_service is not None:
                await self.bing_service.run(scheduled=True)
            if self.crawler is not None:
                await self.crawler.run_once(scheduled=True)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("Scheduled Bing discovery failed (%s)", type(exc).__name__)
        finally:
            if current is not None:
                self._active_background_tasks.discard(current)

    async def _scheduled_crawl_processing(self) -> None:
        current = asyncio.current_task()
        if current is not None:
            self._active_background_tasks.add(current)
        try:
            if self.crawler is not None:
                await self.crawler.run_once(scheduled=True)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("Scheduled crawl processing failed (%s)", type(exc).__name__)
        finally:
            if current is not None:
                self._active_background_tasks.discard(current)

    async def _scheduled_digest(self) -> None:
        current = asyncio.current_task()
        if current is not None:
            self._active_background_tasks.add(current)
        try:
            if self.digest_service is not None:
                await self.digest_service.run(trigger="scheduled")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("Scheduled digest failed (%s)", type(exc).__name__)
        finally:
            if current is not None:
                self._active_background_tasks.discard(current)

    async def _run_startup_digest_catch_up(self) -> None:
        service = self.digest_service
        if service is None:
            return
        try:
            if await service.catch_up_required():
                log.info("Startup digest catch-up is due")
                await service.run(trigger="startup_catch_up")
            else:
                log.info("Startup digest catch-up is not due")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("Startup digest catch-up failed (%s)", type(exc).__name__)

    def _digest_catch_up_done(self, task: asyncio.Task[Any]) -> None:
        self._active_background_tasks.discard(task)
        if task.cancelled():
            return
        try:
            task.result()
        except Exception as exc:
            log.warning("Startup digest catch-up stopped with an error (%s)", type(exc).__name__)

    async def close(self) -> None:
        log.info("Closing Discord client")
        if self.scheduler is not None:
            self.scheduler.shutdown(wait=False)
        current = asyncio.current_task()
        active = [
            task
            for task in self._active_background_tasks
            if task is not current and not task.done()
        ]
        for task in active:
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                log.warning("Active scheduled job stopped with an error (%s)", type(exc).__name__)
        client = self.safe_http_client
        self.safe_http_client = None
        if client is not None:
            try:
                await client.close()
            except Exception as exc:
                log.warning("HTTP client close failed (%s)", type(exc).__name__)
        await super().close()
