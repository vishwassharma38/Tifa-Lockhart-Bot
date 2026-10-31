"""Phase 1 slash commands: /about and /status."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands

from app.config import Settings
from app.health.status import StatusReport

if TYPE_CHECKING:
    from app.bot.client import TifaBot

log = logging.getLogger(__name__)

EMBED_COLOR = 0x8B1E3F
ABOUT_PURPOSE = "Responsible discovery and preservation of Tifa Lockhart-related public media."


def build_about_embed(settings: Settings) -> discord.Embed:
    embed = discord.Embed(
        title="Tifa Lockhart Bot",
        description=ABOUT_PURPOSE,
        color=EMBED_COLOR,
    )
    embed.add_field(name="Version", value=settings.app_version, inline=True)
    embed.add_field(name="Developer", value="SoftwareFromVish", inline=True)
    embed.set_footer(text="Phase 1 foundation")
    return embed


def build_status_embed(report: StatusReport) -> discord.Embed:
    embed = discord.Embed(
        title="Tifa Lockhart Bot - Status",
        color=EMBED_COLOR,
    )
    embed.add_field(name="Discord", value=report.discord_label, inline=True)
    embed.add_field(
        name="Version",
        value=f"{report.app_version} ({report.app_env})",
        inline=True,
    )
    embed.add_field(name="Supabase", value=report.supabase.label, inline=False)
    embed.add_field(name="R2 storage", value=report.r2.label, inline=False)
    embed.add_field(name="Collection", value=report.collection_state, inline=False)
    embed.set_footer(text="Phase 1 foundation")
    return embed


class PhaseOneCommands(commands.Cog):
    """Slash commands that truthfully describe the Phase 1 foundation."""

    def __init__(self, bot: TifaBot) -> None:
        self.bot = bot

    @app_commands.command(name="about", description="Learn about Tifa Lockhart Bot.")
    async def about(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(embed=build_about_embed(self.bot.settings))

    @app_commands.command(name="status", description="Show Phase 1 dependency and runtime status.")
    async def status_command(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            report = await self.bot.status_service.collect(
                discord_connected=self.bot.is_ready(),
            )
        except Exception as exc:
            log.warning("Status command failed (%s)", type(exc).__name__)
            await interaction.followup.send(
                "Status check failed. See the bot logs for details.",
                ephemeral=True,
            )
            return
        await interaction.followup.send(embed=build_status_embed(report), ephemeral=True)
