"""Phase 4 Discord application commands."""

from __future__ import annotations

import hashlib
import io
import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

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
    embed.set_footer(text="V1")
    project = getattr(
        settings, "project_url", "https://github.com/vishwassharma38/Tifa-Lockhart-Bot"
    )
    if project:
        embed.add_field(name="Project", value=project, inline=False)
        embed.url = project
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
    embed.add_field(name="B2 storage", value=report.storage.label, inline=False)
    embed.add_field(name="Collection", value="Persisted state unavailable", inline=False)
    embed.set_footer(text="Phase 1 foundation")
    return embed


class PhaseFourCommands(commands.Cog):
    """Slash commands that truthfully describe the Phase 1 foundation."""

    def __init__(self, bot: TifaBot) -> None:
        self.bot = bot

    @app_commands.command(name="about", description="Learn about Tifa Lockhart Bot.")
    async def about(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(embed=build_about_embed(self.bot.settings))

    def _owner(self, interaction: discord.Interaction) -> bool:
        return bool(
            self.bot.settings.owner_discord_id
            and interaction.user.id == self.bot.settings.owner_discord_id
        )

    @app_commands.command(name="start", description="Enable scheduled Reddit collection.")
    async def start_collection(self, interaction: discord.Interaction) -> None:
        if not self._owner(interaction):
            await interaction.response.send_message(
                "This command is restricted to the configured owner.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            state = await self.bot.repository.set_collection_enabled(True)
            reason = (
                self.bot.collector.block_reason
                if self.bot.collector
                else "Reddit collection services are unavailable"
            )
            text = (
                "Collection is enabled and may run on the next hourly schedule."
                if not reason
                else f"Collection preference is enabled, but it is blocked: {reason}."
            )
            await interaction.followup.send(
                f"Persisted state: {'enabled' if state.get('collection_enabled') else 'paused'}. {text}",
                ephemeral=True,
            )
        except Exception as exc:
            log.warning("Start command failed (%s)", type(exc).__name__)
            await interaction.followup.send("Could not persist collection state.", ephemeral=True)

    @app_commands.command(name="stop", description="Pause scheduled Reddit collection.")
    async def stop_collection(self, interaction: discord.Interaction) -> None:
        if not self._owner(interaction):
            await interaction.response.send_message(
                "This command is restricted to the configured owner.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            state = await self.bot.repository.set_collection_enabled(False)
            await interaction.followup.send(
                f"Collection state persisted as {'enabled' if state.get('collection_enabled') else 'paused'}. The bot remains connected.",
                ephemeral=True,
            )
        except Exception as exc:
            log.warning("Stop command failed (%s)", type(exc).__name__)
            await interaction.followup.send("Could not persist collection state.", ephemeral=True)

    @app_commands.command(name="save", description="Archive and publish one Reddit post.")
    @app_commands.describe(url="Reddit submission URL or ID")
    async def save(self, interaction: discord.Interaction, url: str) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        pipeline = getattr(self.bot, "pipeline", None)
        if pipeline is None:
            await interaction.followup.send(
                "Save is unavailable because the archive pipeline is not configured.",
                ephemeral=True,
            )
            return
        try:
            result = await pipeline.run(url)
            await interaction.followup.send(
                f"Save result: {result.reason} ({result.status.value}).", ephemeral=True
            )
        except Exception as exc:
            log.warning("Save command failed (%s)", type(exc).__name__)
            await interaction.followup.send(
                "Could not save this post. Check that it is supported and try again.",
                ephemeral=True,
            )

    @app_commands.command(name="random", description="Deliver a random verified archived asset.")
    async def random_asset(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=True)
        publication_id: str | None = None
        try:
            repo, storage = self.bot.repository, self.bot.storage
            row = await repo.random_asset()
            if not row:
                await interaction.followup.send("No verified archived media is available yet.")
                return
            data = await storage.download_bytes(row["storage_object_key"], max_bytes=10_000_000)
            if hashlib.sha256(data).hexdigest() != row["sha256"]:
                await interaction.followup.send("The archived file failed integrity verification.")
                return
            source = await repo.source_for_media(row["id"])
            channel_id = interaction.channel_id or self.bot.settings.saves_channel_id or 0
            publication = await repo.create_publication(str(row["id"]), channel_id, "random")
            publication_id = str(publication["id"])
            filename = row.get("filename") or f"archive.{row.get('extension', 'bin')}"
            file = discord.File(io.BytesIO(data), filename=filename)
            details = [
                (source or {}).get("title")
                or (source or {}).get("caption")
                or row.get("title")
                or row.get("caption")
                or "Archived Tifa media"
            ]
            if source:
                if source.get("creator_name"):
                    creator = f"Creator: {source['creator_name']}"
                    if source.get("creator_profile_url"):
                        creator += f" ({source['creator_profile_url']})"
                    details.append(creator)
                if source.get("source_url"):
                    details.append(f"Source: {source['source_url']}")
            message = await interaction.followup.send("\n".join(details), file=file, wait=True)
            await repo.update_publication(publication_id, status="published", message_id=message.id)
        except Exception as exc:
            log.warning("Random command failed (%s)", type(exc).__name__)
            if publication_id:
                try:
                    await self.bot.repository.update_publication(
                        publication_id, status="failed", last_error="random_delivery_failed"
                    )
                except Exception as update_exc:
                    log.warning(
                        "Random delivery failure could not be recorded (%s)",
                        type(update_exc).__name__,
                    )
            await interaction.followup.send("Could not retrieve or deliver an archived file.")

    @app_commands.command(name="stats", description="Show archived asset and delivery counts.")
    @app_commands.choices(
        period=[app_commands.Choice(name=x.title(), value=x) for x in ("week", "month", "lifetime")]
    )
    async def stats(
        self, interaction: discord.Interaction, period: app_commands.Choice[str]
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            now = datetime.now(ZoneInfo("Asia/Kolkata"))
            start = None
            if period.value == "week":
                start = (
                    (now - timedelta(days=now.weekday()))
                    .replace(hour=0, minute=0, second=0, microsecond=0)
                    .astimezone(UTC)
                    .isoformat()
                )
            elif period.value == "month":
                start = (
                    now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
                    .astimezone(UTC)
                    .isoformat()
                )
            stats = await self.bot.repository.asset_stats(start)
            await interaction.followup.send(
                f"{period.value.title()} — unique archived assets: {stats['total']}; delivered files: {stats['delivered']}; images: {stats['image']}; animations: {stats['animation']}; videos: {stats['video']}.",
                ephemeral=True,
            )
        except Exception as exc:
            log.warning("Stats command failed (%s)", type(exc).__name__)
            await interaction.followup.send("Statistics are unavailable.", ephemeral=True)

    @app_commands.command(name="status", description="Show operational status.")
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
        embed = build_status_embed(report)
        collector = getattr(self.bot, "collector", None)
        blocked = collector.block_reason if collector else "collector unavailable"
        embed.set_field_at(
            4,
            name="Collection",
            value=f"Persisted state unavailable; effective: blocked; Reddit feature: {'enabled' if self.bot.settings.reddit_enabled else 'disabled'}; approval: {'confirmed' if self.bot.settings.reddit_access_approved else 'not confirmed'}; reason: {blocked}",
            inline=False,
        )
        try:
            state = await self.bot.repository.get_runtime_state()
            runs = await self.bot.repository.latest_reddit_runs(1)
            counts = await self.bot.repository.operational_counts()
            latest = runs[0] if runs else None
            effective = bool(state.get("collection_enabled")) and blocked is None
            embed.set_field_at(
                4,
                name="Collection",
                value=f"Persisted: {'enabled' if state.get('collection_enabled') else 'paused'}; effective: {'enabled' if effective else 'blocked/paused'}; Reddit feature: {'enabled' if self.bot.settings.reddit_enabled else 'disabled'}; approval: {'confirmed' if self.bot.settings.reddit_access_approved else 'not confirmed'}; reason: {blocked or 'none'}",
                inline=False,
            )
            embed.add_field(
                name="Latest Reddit run",
                value=(
                    f"{latest.get('outcome')}: {latest.get('error_summary') or 'no errors'}"
                    if latest
                    else "No runs"
                ),
                inline=False,
            )
            embed.add_field(
                name="Automated video acquisition",
                value="Skipped for SSRF safety; direct media and galleries use the public-only fetcher.",
                inline=False,
            )
            embed.add_field(
                name="Last successful collection",
                value=str(state.get("last_reddit_success_at") or "Never"),
                inline=False,
            )
            embed.add_field(
                name="Last digest",
                value=str(counts.get("last_digest_success_at") or "Not implemented in Phase 6"),
                inline=False,
            )
            embed.add_field(name="Queue / backlog", value=str(counts["queued"]), inline=True)
            embed.add_field(name="Dumpster", value=str(counts["dumpster"]), inline=True)
        except Exception as exc:
            log.warning("Extended status unavailable (%s)", type(exc).__name__)
        await interaction.followup.send(embed=embed, ephemeral=True)
