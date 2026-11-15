"""Phase 4/5 Discord application commands."""

from __future__ import annotations

import asyncio
import enum
import hashlib
import io
import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands

from app.async_safety import await_cancellation_safe
from app.bot.publisher import (
    DiscordPublicationError,
    DiscordPublicationMessageError,
    DiscordPublicationRejectedError,
    DiscordPublisher,
    PublicationContext,
    build_publication_message,
    publication_error_from_discord_exception,
)
from app.config import Settings
from app.db.repository import RepositoryError
from app.health.status import StatusReport
from app.jobs.digest import DEFAULT_DIGEST_ATTEMPT_LIMIT, digest_window_incomplete
from app.reddit.connector import parse_submission_id
from app.storage.base import (
    StorageAccessDeniedError,
    StorageError,
    StorageObjectNotFoundError,
    StorageObjectTooLargeError,
    StorageTimeoutError,
)

if TYPE_CHECKING:
    from app.bot.client import TifaBot

log = logging.getLogger(__name__)

EMBED_COLOR = 0x8B1E3F
ABOUT_PURPOSE = "Responsible discovery and preservation of Tifa Lockhart-related public media."
RANDOM_ARCHIVE_MAX_BYTES = 10_000_000
_RANDOM_MARKER_PREFIX = "tifa-random-publication:"
_RANDOM_CANCELLATION_CLEANUP_TIMEOUT_SECONDS = 5.0


class _RandomDeliveryFailure(RuntimeError):
    """A stage-specific, user-safe failure from the /random pipeline."""

    def __init__(self, code: str, user_message: str, *, detail: str | None = None) -> None:
        super().__init__(detail or code)
        self.code = code
        self.user_message = user_message
        self.detail = detail


class _PublicationPersistence(enum.Enum):
    """Outcome of persisting a Discord delivery that already happened."""

    CONFIRMED = "confirmed"
    NOT_PERSISTED = "not_persisted"
    UNCONFIRMED = "unconfirmed"


def _random_publication_marker(publication_id: str) -> str:
    return f"{_RANDOM_MARKER_PREFIX}{publication_id}"


def _safe_stage_detail(exc: BaseException) -> str:
    """Return a bounded diagnostic that cannot expose credentials or raw traces."""
    if isinstance(exc, (DiscordPublicationError, StorageError, RepositoryError)):
        text = " ".join(str(exc).split())[:240]
        return f"{type(exc).__name__}: {text}" if text else type(exc).__name__
    return type(exc).__name__


def _failure_record(code: str, detail: str | None) -> str:
    return (f"{code}: {detail}" if detail else code)[:500]


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

    async def _record_random_failure(
        self,
        publication_id: str,
        code: str,
        detail: str | None,
    ) -> None:
        last_error = _failure_record(code, detail)
        try:
            await self.bot.repository.update_publication(
                publication_id, status="failed", last_error=last_error
            )
        except Exception as exc:
            # Preserve the original stage and reason in logs; a secondary
            # database failure must not erase the only evidence of the cause.
            log.error(
                "Random publication failure could not be recorded "
                "publication_id=%s failure_code=%s reason=%s update_exception=%s",
                publication_id,
                code,
                detail or "none",
                type(exc).__name__,
            )

    async def _record_random_pending(
        self,
        publication_id: str,
        code: str,
        detail: str | None,
    ) -> None:
        try:
            await self.bot.repository.update_publication(
                publication_id,
                status="pending",
                last_error=_failure_record(code, detail),
            )
        except Exception as exc:
            log.error(
                "Random publication uncertain state could not be recorded "
                "publication_id=%s failure_code=%s update_exception=%s",
                publication_id,
                code,
                type(exc).__name__,
            )

    @staticmethod
    def _publication_confirms_published(record: object, message_id: int) -> bool:
        return (
            isinstance(record, dict)
            and record.get("status") == "published"
            and record.get("message_id") == message_id
        )

    async def _persist_random_published(
        self, publication_id: str, message_id: int
    ) -> _PublicationPersistence:
        expected_message_id = int(message_id)
        try:
            persisted = await self.bot.repository.update_publication(
                publication_id,
                status="published",
                message_id=expected_message_id,
                last_error="",
            )
        except Exception as exc:
            log.error(
                "Random image was delivered but publication persistence failed; "
                "reconciling publication_id=%s message_id=%s exception=%s",
                publication_id,
                message_id,
                type(exc).__name__,
            )
            return await self._reconcile_random_publication(
                publication_id, expected_message_id, update_exception=exc
            )
        if self._publication_confirms_published(persisted, expected_message_id):
            return _PublicationPersistence.CONFIRMED
        log.warning(
            "Random publication update did not confirm the delivered message; "
            "reconciling publication_id=%s message_id=%s persisted_status=%s "
            "persisted_message_id=%s",
            publication_id,
            expected_message_id,
            persisted.get("status") if isinstance(persisted, dict) else None,
            persisted.get("message_id") if isinstance(persisted, dict) else None,
        )
        return await self._reconcile_random_publication(publication_id, expected_message_id)

    async def _reconcile_random_publication(
        self,
        publication_id: str,
        message_id: int,
        update_exception: Exception | None = None,
    ) -> _PublicationPersistence:
        """Resolve an ambiguous published update after delivery was confirmed.

        The update may have committed while only its response was lost. Reading
        the row back prevents a later ``pending`` write from downgrading that
        committed delivery. A missing or unreadable record remains unconfirmed
        so the caller performs no further status write.
        """
        try:
            current = await self.bot.repository.get_publication(publication_id)
        except Exception as exc:
            log.error(
                "Random publication persistence state could not be reconciled; "
                "leaving it untouched publication_id=%s message_id=%s "
                "update_exception=%s reconcile_exception=%s",
                publication_id,
                message_id,
                type(update_exception).__name__ if update_exception else "none",
                type(exc).__name__,
            )
            return _PublicationPersistence.UNCONFIRMED

        if not isinstance(current, dict):
            log.error(
                "Random publication record was not found during reconciliation; "
                "leaving state unconfirmed publication_id=%s message_id=%s "
                "update_exception=%s",
                publication_id,
                message_id,
                type(update_exception).__name__ if update_exception else "none",
            )
            return _PublicationPersistence.UNCONFIRMED

        if current.get("status") == "published":
            persisted_message_id = current.get("message_id")
            if persisted_message_id != message_id:
                log.warning(
                    "Random publication is already published with a different message ID; "
                    "never overwriting it publication_id=%s expected_message_id=%s "
                    "persisted_message_id=%s",
                    publication_id,
                    message_id,
                    persisted_message_id,
                )
                return _PublicationPersistence.UNCONFIRMED
            log.info(
                "Random publication update committed before its response was lost "
                "publication_id=%s message_id=%s",
                publication_id,
                message_id,
            )
            return _PublicationPersistence.CONFIRMED

        log.warning(
            "Random publication update was not persisted publication_id=%s message_id=%s "
            "update_exception=%s persisted_status=%s",
            publication_id,
            message_id,
            type(update_exception).__name__ if update_exception else "none",
            current.get("status"),
        )
        return _PublicationPersistence.NOT_PERSISTED

    async def _send_random_error(self, interaction: discord.Interaction, message: str) -> None:
        try:
            await interaction.followup.send(message)
        except Exception as exc:
            log.warning("Random error response could not be delivered (%s)", type(exc).__name__)

    async def _handle_ambiguous_random_publication(
        self,
        *,
        interaction: discord.Interaction,
        publication_id: str,
        channel_id: int,
        marker: str,
        detail: str | None,
    ) -> None:
        """Reconcile an uncertain send against channel history before deciding.

        A successful marker match proves delivery and is persisted. A conclusive
        miss is a real non-delivery. Anything else remains pending and is never
        automatically retried, preventing duplicate Discord messages.
        """
        publisher = DiscordPublisher(self.bot, channel_id)
        try:
            lookup = await publisher.find_publication_marker(
                channel_id=channel_id,
                marker=marker,
            )
        except Exception as exc:
            log.warning(
                "Random publication reconciliation unavailable "
                "publication_id=%s channel_id=%s exception=%s",
                publication_id,
                channel_id,
                type(exc).__name__,
            )
            await self._record_random_pending(
                publication_id,
                "discord_publication_ambiguous",
                detail or "delivery outcome could not be reconciled",
            )
            await self._send_random_error(
                interaction,
                "The delivery outcome is uncertain. The image may already have been posted, "
                "and it will not be resent automatically.",
            )
            return

        if lookup.message_id is not None:
            persistence = await self._persist_random_published(publication_id, lookup.message_id)
            if persistence is _PublicationPersistence.CONFIRMED:
                log.info(
                    "Random publication reconciled from channel history "
                    "publication_id=%s message_id=%s channel_id=%s",
                    publication_id,
                    lookup.message_id,
                    channel_id,
                )
            elif persistence is _PublicationPersistence.NOT_PERSISTED:
                await self._record_random_pending(
                    publication_id,
                    "publication_persistence_failed",
                    detail or "delivered message could not be persisted",
                )
            else:
                log.error(
                    "Random publication was found in channel history but its "
                    "persistence state could not be reconciled publication_id=%s "
                    "message_id=%s channel_id=%s",
                    publication_id,
                    lookup.message_id,
                    channel_id,
                )
            return

        if lookup.conclusive:
            await self._record_random_failure(
                publication_id,
                "discord_publication_not_delivered",
                detail or "Discord did not accept the attachment",
            )
            await self._send_random_error(
                interaction, "The image could not be published to Discord."
            )
            return

        await self._record_random_pending(
            publication_id,
            "discord_publication_ambiguous",
            detail or "delivery outcome could not be confirmed",
        )
        await self._send_random_error(
            interaction,
            "The delivery outcome is uncertain. The image may already have been posted, "
            "and it will not be resent automatically.",
        )

    async def _handle_random_cancellation(
        self, publication_id: str | None, *, send_started: bool
    ) -> None:
        if not publication_id:
            return
        if send_started:
            completed, _result, error = await await_cancellation_safe(
                self._record_random_pending(
                    publication_id,
                    "discord_publication_ambiguous",
                    "command cancelled before delivery confirmation",
                ),
                timeout=_RANDOM_CANCELLATION_CLEANUP_TIMEOUT_SECONDS,
            )
            if not completed:
                log.warning(
                    "Random publication cancellation state was not persisted "
                    "publication_id=%s (%s)",
                    publication_id,
                    type(error).__name__ if error else "timeout",
                )
            return
        completed, _result, error = await await_cancellation_safe(
            self._record_random_failure(
                publication_id,
                "command_cancelled_before_delivery",
                "command was cancelled before the Discord send began",
            ),
            timeout=_RANDOM_CANCELLATION_CLEANUP_TIMEOUT_SECONDS,
        )
        if not completed:
            log.warning(
                "Random publication cancellation state was not persisted publication_id=%s (%s)",
                publication_id,
                type(error).__name__ if error else "timeout",
            )

    @app_commands.command(name="random", description="Deliver a random verified archived asset.")
    async def random_asset(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=True)
        publication_id: str | None = None
        media_id: str | None = None
        channel_id = 0
        source_count = 0
        message_chars: int | None = None
        send_started = False
        delivery_confirmed = False
        try:
            repo, storage = self.bot.repository, self.bot.storage
            try:
                row = await repo.random_asset()
            except Exception as exc:
                raise _RandomDeliveryFailure(
                    "media_selection_failed",
                    "Could not select archived media right now.",
                    detail=_safe_stage_detail(exc),
                ) from exc
            if not row:
                log.info("Random command skipped stage=no_eligible_media")
                await interaction.followup.send("No verified archived media is available yet.")
                return

            media_id = str(row["id"])
            channel_id = interaction.channel_id or self.bot.settings.saves_channel_id or 0
            try:
                publication = await repo.create_publication(media_id, channel_id, "random")
            except Exception as exc:
                raise _RandomDeliveryFailure(
                    "publication_record_creation_failed",
                    "The publication record could not be created.",
                    detail=_safe_stage_detail(exc),
                ) from exc
            publication_id = str(publication["id"])

            try:
                data = await storage.download_bytes(
                    row["storage_object_key"], max_bytes=RANDOM_ARCHIVE_MAX_BYTES
                )
            except StorageObjectNotFoundError as exc:
                raise _RandomDeliveryFailure(
                    "archive_object_not_found",
                    "The archived file is missing from storage.",
                    detail=_safe_stage_detail(exc),
                ) from exc
            except StorageObjectTooLargeError as exc:
                raise _RandomDeliveryFailure(
                    "archive_object_too_large",
                    "The archived file is too large to deliver.",
                    detail=_safe_stage_detail(exc),
                ) from exc
            except StorageTimeoutError as exc:
                raise _RandomDeliveryFailure(
                    "archive_download_failed",
                    "The archived image could not be retrieved right now.",
                    detail=_safe_stage_detail(exc),
                ) from exc
            except StorageAccessDeniedError as exc:
                raise _RandomDeliveryFailure(
                    "archive_download_failed",
                    "The archived image could not be retrieved right now.",
                    detail=_safe_stage_detail(exc),
                ) from exc
            except StorageError as exc:
                raise _RandomDeliveryFailure(
                    "archive_download_failed",
                    "The archived image could not be retrieved right now.",
                    detail=_safe_stage_detail(exc),
                ) from exc
            except Exception as exc:
                raise _RandomDeliveryFailure(
                    "archive_download_failed",
                    "The archived image could not be retrieved right now.",
                    detail=_safe_stage_detail(exc),
                ) from exc

            if hashlib.sha256(data).hexdigest() != row["sha256"]:
                raise _RandomDeliveryFailure(
                    "integrity_verification_failed",
                    "The archived file failed integrity verification.",
                    detail="SHA-256 mismatch",
                )

            try:
                list_sources = getattr(repo, "sources_for_media", None)
                if callable(list_sources):
                    sources = await list_sources(media_id)
                else:
                    source = await repo.source_for_media(media_id)
                    sources = [source] if source else []
            except Exception as exc:
                raise _RandomDeliveryFailure(
                    "source_metadata_retrieval_failed",
                    "The archived image's source details could not be loaded.",
                    detail=_safe_stage_detail(exc),
                ) from exc
            source_count = len(sources)

            marker = _random_publication_marker(publication_id)
            context = PublicationContext.from_media_record(row, sources, publication_marker=marker)
            try:
                message_text = build_publication_message(context)
            except DiscordPublicationMessageError as exc:
                raise _RandomDeliveryFailure(
                    "message_construction_failed",
                    "The archived image could not be formatted for Discord.",
                    detail=_safe_stage_detail(exc),
                ) from exc
            except Exception as exc:
                raise _RandomDeliveryFailure(
                    "message_construction_failed",
                    "The archived image could not be formatted for Discord.",
                    detail=_safe_stage_detail(exc),
                ) from exc
            message_chars = len(message_text)

            filename = row.get("filename") or f"archive.{row.get('extension', 'bin')}"
            buffer = io.BytesIO(data)
            file: discord.File | None = None
            try:
                file = discord.File(buffer, filename=filename)
                send_started = True
                try:
                    message = await interaction.followup.send(
                        message_text,
                        file=file,
                        suppress_embeds=True,
                        wait=True,
                    )
                    delivery_confirmed = True
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    publication_error = publication_error_from_discord_exception(exc)
                    if isinstance(publication_error, DiscordPublicationRejectedError):
                        raise _RandomDeliveryFailure(
                            "discord_publication_rejected",
                            "The image could not be published to Discord.",
                            detail=_safe_stage_detail(publication_error),
                        ) from exc
                    log.warning(
                        "Random Discord delivery outcome uncertain "
                        "publication_id=%s media_id=%s channel_id=%s message_chars=%s "
                        "sources=%s exception=%s",
                        publication_id,
                        media_id,
                        channel_id,
                        message_chars,
                        source_count,
                        type(exc).__name__,
                    )
                    await self._handle_ambiguous_random_publication(
                        interaction=interaction,
                        publication_id=publication_id,
                        channel_id=channel_id,
                        marker=marker,
                        detail=_safe_stage_detail(publication_error),
                    )
                    return
            finally:
                # discord.File does not close a caller-owned buffer. Guard the
                # constructor too so a failed upload allocation cannot leak it.
                if file is not None:
                    file.close()
                buffer.close()

            delivered_message_id = getattr(message, "id", None)
            if delivered_message_id is None:
                await self._handle_ambiguous_random_publication(
                    interaction=interaction,
                    publication_id=publication_id,
                    channel_id=channel_id,
                    marker=marker,
                    detail="Discord did not return a message ID",
                )
                return

            persistence = await self._persist_random_published(
                publication_id, int(delivered_message_id)
            )
            if persistence is _PublicationPersistence.CONFIRMED:
                log.info(
                    "Random publication delivered publication_id=%s media_id=%s "
                    "message_id=%s channel_id=%s message_chars=%s sources=%s",
                    publication_id,
                    media_id,
                    delivered_message_id,
                    channel_id,
                    message_chars,
                    source_count,
                )
            elif persistence is _PublicationPersistence.NOT_PERSISTED:
                # Delivery definitely happened. Never flip this row to failed
                # merely because the follow-up database update failed.
                await self._record_random_pending(
                    publication_id,
                    "publication_persistence_failed",
                    f"delivered message {delivered_message_id} could not be recorded",
                )
            else:
                # The update may have committed even though it could not be
                # read back. Do not issue another status write that could
                # downgrade an already published record.
                log.error(
                    "Random publication delivered but its persistence state remains "
                    "unconfirmed; no further status write attempted publication_id=%s "
                    "message_id=%s channel_id=%s",
                    publication_id,
                    delivered_message_id,
                    channel_id,
                )
        except asyncio.CancelledError:
            await self._handle_random_cancellation(publication_id, send_started=send_started)
            raise
        except _RandomDeliveryFailure as exc:
            log.warning(
                "Random command failed stage=%s media_id=%s publication_id=%s "
                "channel_id=%s sources=%s message_chars=%s exception=%s detail=%s",
                exc.code,
                media_id or "unknown",
                publication_id or "none",
                channel_id,
                source_count,
                message_chars,
                type(exc.__cause__).__name__ if exc.__cause__ else "none",
                exc.detail or "none",
            )
            if publication_id:
                await self._record_random_failure(publication_id, exc.code, exc.detail)
            await self._send_random_error(interaction, exc.user_message)
        except Exception as exc:
            detail = _safe_stage_detail(exc)
            log.warning(
                "Random command failed stage=unexpected media_id=%s publication_id=%s "
                "channel_id=%s exception=%s detail=%s",
                media_id or "unknown",
                publication_id or "none",
                channel_id,
                type(exc).__name__,
                detail,
            )
            if publication_id:
                if delivery_confirmed:
                    # The attachment already reached Discord. Any later local
                    # failure is a reconciliation problem, not a delivery
                    # failure, and must never produce a duplicate-send retry.
                    await self._record_random_pending(
                        publication_id,
                        "publication_persistence_failed",
                        detail,
                    )
                else:
                    await self._record_random_failure(
                        publication_id, "random_delivery_failed", detail
                    )
            if delivery_confirmed:
                return
            await self._send_random_error(interaction, "Could not deliver the archived image.")

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
            frontier = (
                await frontier_service(max_depth=self.bot.settings.max_crawl_depth)
                if callable(frontier_service)
                else {}
            )
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
