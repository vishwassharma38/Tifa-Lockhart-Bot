"""Discord delivery of an actual archived media file with attribution."""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlsplit

import discord

from app.reddit.models import RedditCandidate

log = logging.getLogger(__name__)

_MAX_MESSAGE_LENGTH = 2000


class DiscordPublicationError(RuntimeError):
    """Raised when an attachment cannot be delivered to Discord."""


class DiscordPublicationRejectedError(DiscordPublicationError):
    """Raised when Discord definitively rejects an attachment request."""


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
    sources: tuple[dict[str, Any], ...] = ()
    media_type: str | None = None

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
        source: dict[str, Any] | list[dict[str, Any]] | None = None,
        *,
        publication_marker: str | None = None,
    ) -> PublicationContext:
        sources = tuple(source) if isinstance(source, list) else ((source,) if source else ())
        provenance = next((row for row in sources if row.get("source_url")), {})
        creator = next((row for row in sources if row.get("creator_name")), {})
        return cls(
            title=provenance.get("title") or media.get("title"),
            caption=provenance.get("caption") or media.get("caption"),
            creator_name=creator.get("creator_name"),
            creator_profile_url=creator.get("creator_profile_url"),
            source_url=provenance.get("source_url"),
            source_platform=provenance.get("source_platform"),
            source_post_id=provenance.get("source_post_id"),
            category=media.get("category"),
            publication_marker=publication_marker,
            sources=sources,
            media_type=media.get("media_type"),
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

    def clean_label(value: Any) -> str:
        text = " ".join(str(value or "").split())
        markdown_special = set("\\`*_{}[]()#+-.!|>~")
        return "".join(
            f"\\{character}" if character in markdown_special else character for character in text
        )

    def safe_url(value: Any) -> str | None:
        text = str(value or "").strip()
        try:
            parsed = urlsplit(text)
        except ValueError:
            return None
        if (
            parsed.scheme.lower() not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or any(ord(char) < 32 or char.isspace() for char in text)
        ):
            return None
        try:
            _port = parsed.port
        except ValueError:
            return None
        return quote(text, safe=":/?@!$&'*,;=%+-._~")

    media_type = clean_label(context.media_type).upper() or "MEDIA"
    lines = [f"**TIFA ARCHIVE · {media_type}**"]
    title = clean_label(context.title)
    if title:
        lines.append(title[:500])
    creator = clean_label(context.creator_name)
    profile = safe_url(context.creator_profile_url)
    if creator:
        lines.append(
            f"Creator: [{creator}]({profile})"
            if profile
            else f"Creator: {creator} (profile not provided by source)"
        )
    else:
        lines.append("Creator: not provided by source")
    category = clean_label(context.category)
    if category:
        lines.append(f"Category: {category}")

    associations = list(context.sources)
    if not associations and context.source_url:
        associations = [{"source_url": context.source_url}]
    seen: set[str] = set()
    if context.creator_profile_url:
        seen.add(str(context.creator_profile_url).strip())
    links: list[str] = []
    for source in associations:
        additional_creator = clean_label(source.get("creator_name"))
        additional_profile = safe_url(source.get("creator_profile_url"))
        if additional_creator and additional_creator != creator:
            if additional_profile:
                lines.append(f"Creator: [{additional_creator}]({additional_profile})")
                seen.add(str(source.get("creator_profile_url") or "").strip())
            else:
                lines.append(f"Creator: {additional_creator} (profile not provided by source)")
        original = str(source.get("source_url") or "").strip()
        url = safe_url(original)
        if url and original not in seen:
            seen.add(original)
            links.append(f"[Original source]({url})")
        for key, label in (
            ("creator_profile_url", "Creator profile"),
            ("external_url", "Related source"),
        ):
            original = str(source.get(key) or "").strip()
            url = safe_url(original)
            if url and original not in seen:
                seen.add(original)
                links.append(f"[{label}]({url})")
    if links:
        lines.append("Sources: " + " · ".join(links))
    elif context.publication_marker:
        lines.append("Source: not provided by source")

    marker = str(context.publication_marker or "").strip()
    if marker:
        lines.append(f"Publication: {marker}")
    platform = clean_label(context.source_platform)
    if platform:
        lines.append(f"Source platform: {platform}")
    if context.source_post_id and platform != "web":
        lines.append(f"Source ID: {clean_label(context.source_post_id)}")
    body = clean_label(context.caption)
    if body:
        lines.insert(2 if title else 1, body[:300])
    message = "\n".join(lines)
    if len(message) > _MAX_MESSAGE_LENGTH:
        # Never silently drop attribution: fail before sending and leave the
        # durable publication row available for operator review.
        raise DiscordPublicationRejectedError("Attribution exceeds Discord message length")
    return message


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
        content = build_publication_message(context or candidate)

        file = discord.File(io.BytesIO(data), filename=filename)
        try:
            message = await channel.send(
                content=content,
                file=file,
                suppress_embeds=True,
            )
        except discord.HTTPException as exc:
            if 400 <= exc.status < 500 and exc.status != 429:
                raise DiscordPublicationRejectedError(
                    f"Discord rejected attachment delivery (HTTP {exc.status})"
                ) from None
            raise DiscordPublicationError(
                f"Discord attachment delivery outcome is uncertain ({type(exc).__name__})"
            ) from None
        except Exception as exc:
            raise DiscordPublicationError(
                f"Discord attachment delivery outcome is uncertain ({type(exc).__name__})"
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
