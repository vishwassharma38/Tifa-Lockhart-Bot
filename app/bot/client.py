"""discord.py client lifecycle and Phase 1 command registration."""

from __future__ import annotations

import logging
from typing import Any

import discord
from discord.ext import commands

from app.bot.commands import PhaseOneCommands
from app.config import Settings
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

    async def setup_hook(self) -> None:
        await self.add_cog(PhaseOneCommands(self))
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

    async def close(self) -> None:
        log.info("Closing Discord client")
        await super().close()
