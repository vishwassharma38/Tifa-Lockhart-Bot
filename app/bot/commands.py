"""Phase 4/5 Discord application commands."""

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

from app.bot.publisher import PublicationContext, build_publication_message
from app.config import Settings
from app.health.status import StatusReport
from app.jobs.digest import DEFAULT_DIGEST_ATTEMPT_LIMIT, digest_window_incomplete
from app.reddit.connector import parse_submission_id

if TYPE_CHECKING:
    from app.bot.client import TifaBot

log = logging.getLogger(__name__)

EMBED_COLOR = 0x8B1E3F
ABOUT_PURPOSE = "Responsible discovery and preservation of Tifa Lockhart-related public media."


def _is_reddit_reference(value: str) -> bool:
    """Return whether the input should use the existing Reddit save flow."""
    try:
        parse_submission_id(value)
    except (TypeError, ValueError):
        return False
    return True


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
    embed.set_footer(text="Phase 5 discovery and crawler")
    return embed


class PhaseFourCommands(commands.Cog):
    """Slash commands for the Phase V1 discovery and archive foundation."""

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

    @app_commands.command(name="start", description="Enable scheduled discovery and crawling.")
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

    @app_commands.command(name="stop", description="Pause scheduled discovery and crawling.")
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

    @app_commands.command(
        name="save", description="Archive and publish one Reddit post or public page."
    )
    @app_commands.describe(url="Reddit submission URL or ID, or a public media/page URL")
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
            if _is_reddit_reference(url):
                result = await pipeline.run(url)
            else:
                web_save = getattr(self.bot, "web_save_service", None)
                if web_save is None:
                    await interaction.followup.send(
                        "Public URL saves are unavailable because the safe web service is not configured.",
                        ephemeral=True,
                    )
                    return
                result = await web_save.save(url)
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
            list_sources = getattr(repo, "sources_for_media", None)
            if callable(list_sources):
                sources = await list_sources(row["id"])
            else:
                source = await repo.source_for_media(row["id"])
                sources = [source] if source else []
            channel_id = interaction.channel_id or self.bot.settings.saves_channel_id or 0
            publication = await repo.create_publication(str(row["id"]), channel_id, "random")
            publication_id = str(publication["id"])
            filename = row.get("filename") or f"archive.{row.get('extension', 'bin')}"
            file = discord.File(io.BytesIO(data), filename=filename)
            context = PublicationContext.from_media_record(row, sources)
            message = await interaction.followup.send(
                build_publication_message(context),
                file=file,
                suppress_embeds=True,
                wait=True,
            )
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
            frontier_service = getattr(self.bot.repository, "crawl_frontier_summary", None)
            frontier = await frontier_service() if callable(frontier_service) else {}
            latest = runs[0] if runs else None
            effective = bool(state.get("collection_enabled")) and blocked is None
            embed.set_field_at(
                4,
                name="Collection",
                value=f"Persisted: {'enabled' if state.get('collection_enabled') else 'paused'}; effective: {'enabled' if effective else 'blocked/paused'}; Reddit feature: {'enabled' if self.bot.settings.reddit_enabled else 'disabled'}; approval: {'confirmed' if self.bot.settings.reddit_access_approved else 'not confirmed'}; reason: {blocked or 'none'}",
                inline=False,
            )
            bing_service = getattr(self.bot, "bing_service", None)
            bing_block = (
                bing_service.block_reason(state)
                if bing_service is not None
                else "Bing service unavailable"
            )
            if not self.bot.settings.bing_html_enabled:
                bing_state = "disabled by feature flag"
            elif not self.bot.settings.bing_html_access_approved:
                bing_state = "enabled but access not approved"
            elif state.get("bing_paused"):
                bing_state = "persistently paused"
            else:
                bing_state = "enabled and approved, waiting for schedule"
            bing_runs = await self.bot.repository.latest_bing_runs(1)
            crawl_runs = await self.bot.repository.latest_crawl_runs(1)
            bing_latest = bing_runs[0] if bing_runs else None
            crawl_latest = crawl_runs[0] if crawl_runs else None
            digest_runs = await self.bot.repository.latest_digest_runs(1)
            digest_counts = await self.bot.repository.digest_publication_counts(
                attempt_limit=DEFAULT_DIGEST_ATTEMPT_LIMIT
            )
            digest_state = await self.bot.repository.get_digest_state()
            digest_latest = digest_runs[0] if digest_runs else None
            if digest_latest:
                digest_latest_text = str(digest_latest.get("outcome") or "unknown")
                digest_error = digest_latest.get("error_summary")
                if digest_error:
                    digest_latest_text = f"{digest_latest_text} ({str(digest_error)[:150]})"
            else:
                digest_latest_text = "No runs"
            if digest_window_incomplete(digest_state):
                digest_window_text = "incomplete / awaiting recovery"
            elif digest_state.get("last_window_end"):
                digest_window_text = "complete"
            else:
                digest_window_text = "not started"

            def run_summary(row: dict[str, object] | None) -> str:
                if not row:
                    return "No runs"
                return f"{row.get('outcome')}: {row.get('error_summary') or 'no errors'}"

            def crawl_run_summary(row: dict[str, object] | None) -> str:
                if not row:
                    return "No runs"
                return (
                    f"{row.get('outcome')}; domains visited: {row.get('domains_visited_count', 0)}; "
                    f"new domains: {row.get('new_domains_discovered_count', 0)}; "
                    f"external/internal links: {row.get('external_links_admitted_count', 0)}/"
                    f"{row.get('internal_links_admitted_count', 0)}; "
                    f"skipped links: {row.get('skipped_url_count', 0)}; "
                    f"budget limited: {'yes' if row.get('resource_budget_limited') else 'no'}; "
                    f"errors: {row.get('error_summary') or 'none'}"
                )

            embed.add_field(
                name="Bing discovery",
                value=(
                    f"Feature: {'enabled' if self.bot.settings.bing_html_enabled else 'disabled'}; "
                    f"access: {'approved' if self.bot.settings.bing_html_access_approved else 'not approved'}; "
                    f"state: {bing_state}; pause reason: "
                    f"{state.get('bing_pause_reason') or 'none'}; block: {bing_block or 'none'}"
                ),
                inline=False,
            )
            embed.add_field(
                name="Latest Reddit run",
                value=run_summary(latest),
                inline=False,
            )
            embed.add_field(
                name="Latest Bing run",
                value=run_summary(bing_latest),
                inline=False,
            )
            embed.add_field(
                name="Latest crawler run",
                value=crawl_run_summary(crawl_latest),
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
                name="Last successful Bing discovery",
                value=str(state.get("last_bing_success_at") or "Never"),
                inline=False,
            )
            embed.add_field(
                name="Daily digest",
                value=(
                    f"Schedule: 04:00 IST; last success: "
                    f"{state.get('last_digest_success_at') or 'Never'}; "
                    f"latest: {digest_latest_text}; "
                    f"pending: {digest_counts['pending']}; "
                    f"retryable failed: {digest_counts['failed_retryable']}; "
                    f"exhausted: {digest_counts['failed_exhausted']}; "
                    f"window: {digest_window_text}"
                ),
                inline=False,
            )
            errors = [
                str(row.get("error_summary"))
                for row in (crawl_latest, bing_latest, latest)
                if row and row.get("error_summary")
            ]
            embed.add_field(
                name="Latest relevant job error",
                value=(errors[0][:300] if errors else "None"),
                inline=False,
            )
            embed.add_field(name="Queue / backlog", value=str(counts["queued"]), inline=True)
            embed.add_field(name="Dumpster", value=str(counts["dumpster"]), inline=True)
            if frontier:
                embed.add_field(
                    name="Crawler domains",
                    value=(
                        f"known: {frontier.get('domains_discovered', 0)}; "
                        f"explored: {frontier.get('domains_explored', 0)}; "
                        f"unexplored: {frontier.get('domains_unexplored', 0)}; "
                        f"queued: {frontier.get('queued_domains', 0)}; "
                        f"cooling: {frontier.get('domains_cooling_down', 0)}; "
                        f"visited in 7d: {frontier.get('domains_visited_recently', 0)}"
                    ),
                    inline=False,
                )
                oldest_hours = int(frontier.get("oldest_queued_seconds", 0) or 0) / 3600
                embed.add_field(
                    name="Crawler queue",
                    value=(
                        f"queued URLs: {frontier.get('queued_urls', 0)}; "
                        f"due: {frontier.get('due_urls', 0)}; "
                        f"delayed: {frontier.get('delayed_urls', 0)}; "
                        f"processing: {frontier.get('processing_urls', 0)}; "
                        f"retry backlog: {frontier.get('retry_backlog', 0)}; "
                        f"oldest queued: {oldest_hours:.1f}h"
                    ),
                    inline=False,
                )
        except Exception as exc:
            log.warning("Extended status unavailable (%s)", type(exc).__name__)
        await interaction.followup.send(embed=embed, ephemeral=True)
