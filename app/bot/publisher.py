"""Discord delivery of an actual archived media file with attribution."""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass
from typing import Any

import discord

from app.reddit.models import RedditCandidate

log = logging.getLogger(__name__)

_MAX_MESSAGE_LENGTH = 2000


class DiscordPublicationError(RuntimeError):
    """Raised when an attachment cannot be delivered to Discord."""


@dataclass(frozen=True, slots=True)
class MarkerLookup:
    """Result of inspecting a channel for a deterministic publication marker."""

    message_id: int | None
    conclusive: bool


@dataclass(frozen=True, slots=True)
class PublicationContext:
    """Provider-neutral attribution for a published media file.

    Reddit candidates remain supported through :meth:`from_candidate`, while
    archived web discoveries and digest records use only the provenance that
    actually exists in the database.
    """

    title: str | None = None
    caption: str | None = None
    creator_name: str | None = None
    creator_profile_url: str | None = None
    source_url: str | None = None
    source_platform: str | None = None
    source_post_id: str | None = None
    category: str | None = None
    publication_marker: str | None = None

    @classmethod
    def from_candidate(cls, candidate: RedditCandidate) -> PublicationContext:
        return cls(
            title=candidate.title,
            caption=candidate.body,
            creator_name=candidate.author_name,
            creator_profile_url=candidate.author_profile_url,
            source_url=candidate.source_url,
            source_platform=getattr(candidate, "source_platform", "reddit") or "reddit",
            source_post_id=getattr(candidate, "source_post_id", None) or candidate.submission_id,
        )

    @classmethod
    def from_media_record(
        cls,
        media: dict[str, Any],
        source: dict[str, Any] | None = None,
        *,
        publication_marker: str | None = None,
    ) -> PublicationContext:
        provenance = source or {}
        return cls(
            title=provenance.get("title") or media.get("title"),
            caption=provenance.get("caption") or media.get("caption"),
            creator_name=provenance.get("creator_name"),
            creator_profile_url=provenance.get("creator_profile_url"),
            source_url=provenance.get("source_url"),
            source_platform=provenance.get("source_platform"),
            source_post_id=provenance.get("source_post_id"),
            category=media.get("category"),
            publication_marker=publication_marker,
        )


def _as_context(value: PublicationContext | RedditCandidate) -> PublicationContext:
    if isinstance(value, PublicationContext):
        return value
    return PublicationContext.from_candidate(value)


def build_publication_message(
    value: PublicationContext | RedditCandidate,
) -> str:
    """Build truthful caption text without inventing missing attribution."""
    context = _as_context(value)

    creator_name = (context.creator_name or "").strip()
    profile_url = (context.creator_profile_url or "").strip()
    if creator_name:
        creator_line = f"Creator: {creator_name}"
        if profile_url:
            creator_line = f"{creator_line} ({profile_url})"
        else:
            creator_line = f"{creator_line} (profile not provided by source)"
    else:
        creator_line = "Creator: not provided by source"

    detail_lines = [creator_line]
    category = (context.category or "").strip()
    if category:
        detail_lines.append(f"Category: {category}")

    source_url = (context.source_url or "").strip()
    if source_url:
        detail_lines.append(f"Source: {source_url}")
    elif context.publication_marker:
        detail_lines.append("Source: not provided by source")

    platform = (context.source_platform or "").strip().lower()
    post_id = (context.source_post_id or "").strip()
    if platform == "reddit":
        if post_id:
            detail_lines.append(f"Reddit post ID: {post_id}")
    elif platform:
        detail_lines.append(f"Source platform: {platform}")
        # Web discoveries use a normalized-URL identity, not a native platform
        # post ID. Do not present that generated identity as fabricated metadata.
        if post_id and platform != "web":
            detail_lines.append(f"Source ID: {post_id}")
    elif post_id:
        detail_lines.append(f"Source ID: {post_id}")

    marker = (context.publication_marker or "").strip()
    if marker:
        detail_lines.append(f"Publication: {marker}")

    reserved = sum(len(line) + 1 for line in detail_lines) + 1
    remaining = max(0, _MAX_MESSAGE_LENGTH - reserved - 1)

    text_parts: list[str] = []
    title = (context.title or "").strip()
    body = (context.caption or "").strip()
    if title:
        text_parts.append(title[:remaining])
        remaining = max(0, remaining - len(text_parts[-1]) - 1)
    if body and remaining > 0:
        text_parts.append(body[:remaining])

    parts = [*text_parts, "", *detail_lines]
    return "\n".join(parts).strip()[: _MAX_MESSAGE_LENGTH - 1]


class DiscordPublisher:
    """Send a bounded media payload as a real Discord file attachment."""

    def __init__(self, bot: Any, channel_id: int | None = None) -> None:
        self._bot = bot
        self._channel_id = channel_id or 0

    @property
    def channel_id(self) -> int:
        return self._channel_id

    async def _resolve_channel(self, channel_id: int) -> Any:
        if channel_id <= 0:
            raise DiscordPublicationError("Configured Discord channel is unavailable")
        channel = self._bot.get_channel(channel_id)
        if channel is None:
            fetch_channel = getattr(self._bot, "fetch_channel", None)
            if callable(fetch_channel):
                try:
                    channel = await fetch_channel(channel_id)
                except Exception as exc:
                    raise DiscordPublicationError(
                        f"Configured Discord channel is unavailable ({type(exc).__name__})"
                    ) from None
        if channel is None:
            raise DiscordPublicationError("Configured Discord channel is unavailable")
        return channel

    async def publish(
        self,
        *,
        filename: str,
        data: bytes,
        candidate: RedditCandidate | None = None,
        context: PublicationContext | None = None,
        channel_id: int | None = None,
    ) -> int:
        if candidate is None and context is None:
            raise DiscordPublicationError("Publication attribution is required")
        target_channel = channel_id or self._channel_id
        channel = await self._resolve_channel(target_channel)

        file = discord.File(io.BytesIO(data), filename=filename)
        try:
            message = await channel.send(
                content=build_publication_message(context or candidate),
                file=file,
            )
        except Exception as exc:
            raise DiscordPublicationError(
                f"Discord attachment delivery failed ({type(exc).__name__})"
            ) from None
        finally:
            file.close()

        message_id = getattr(message, "id", None)
        if message_id is None:
            raise DiscordPublicationError("Discord message ID was not returned")
        return int(message_id)

    async def find_publication_marker(
        self,
        *,
        channel_id: int,
        marker: str,
        limit: int = 500,
    ) -> MarkerLookup:
        """Inspect channel history for a stable digest publication marker.

        A missing marker is only conclusive when the bounded history scan did
        not hit its limit. Callers must fail closed on an inconclusive scan.
        """
        channel = await self._resolve_channel(channel_id)
        history = getattr(channel, "history", None)
        if not callable(history):
            raise DiscordPublicationError("Discord message history is unavailable")

        seen = 0
        try:
            async for message in history(limit=max(1, limit)):
                seen += 1
                content = getattr(message, "content", "") or ""
                if marker in content:
                    message_id = getattr(message, "id", None)
                    if message_id is None:
                        raise DiscordPublicationError(
                            "Discord message history returned an invalid message"
                        )
                    return MarkerLookup(message_id=int(message_id), conclusive=True)
                for embed in getattr(message, "embeds", None) or ():
                    footer = getattr(embed, "footer", None)
                    footer_text = getattr(footer, "text", None)
                    if isinstance(footer_text, str) and marker in footer_text:
                        message_id = getattr(message, "id", None)
                        if message_id is None:
                            raise DiscordPublicationError(
                                "Discord message history returned an invalid message"
                            )
                        return MarkerLookup(message_id=int(message_id), conclusive=True)
        except DiscordPublicationError:
            raise
        except Exception as exc:
            raise DiscordPublicationError(
                f"Discord message history lookup failed ({type(exc).__name__})"
            ) from None

        return MarkerLookup(message_id=None, conclusive=seen < max(1, limit))
