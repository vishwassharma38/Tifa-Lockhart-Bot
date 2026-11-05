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
        self.scheduler: AsyncIOScheduler | None = None
        self._active_collection_task: asyncio.Task[Any] | None = None

    async def setup_hook(self) -> None:
        await self.add_cog(PhaseFourCommands(self))
        if self.repository is not None and self.storage is not None:
            try:
                await self.repository.mark_interrupted_jobs()
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
                self.scheduler.start()
                log.info(
                    "Reddit scheduler initialized; persisted collection=%s",
                    bool(state.get("collection_enabled")),
                )
            except Exception as exc:
                log.warning("Reddit scheduler unavailable (%s)", type(exc).__name__)
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

    async def _scheduled_collection(self) -> None:
        if self.collector is not None:
            self._active_collection_task = asyncio.current_task()
            try:
                await self.collector.run(scheduled=True)
            finally:
                self._active_collection_task = None

    async def close(self) -> None:
        log.info("Closing Discord client")
        if self.scheduler is not None:
            self.scheduler.shutdown(wait=False)
        active = self._active_collection_task
        if active is not None and active is not asyncio.current_task():
            try:
                await active
            except Exception as exc:
                log.warning(
                    "Active Reddit collection stopped with an error (%s)", type(exc).__name__
                )
        await super().close()
