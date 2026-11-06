"""Discord delivery of an actual archived media file with attribution."""

from __future__ import annotations

import io
import logging
from typing import Any

import discord

from app.reddit.models import RedditCandidate

log = logging.getLogger(__name__)

_MAX_MESSAGE_LENGTH = 2000


class DiscordPublicationError(RuntimeError):
    """Raised when an attachment cannot be delivered to Discord."""


def build_publication_message(candidate: RedditCandidate) -> str:
    """Build truthful caption text without inventing missing attribution."""
    if candidate.author_name:
        creator_line = f"Creator: {candidate.author_name}"
        if candidate.author_profile_url:
            creator_line = f"{creator_line} ({candidate.author_profile_url})"
    else:
        creator_line = "Creator: not provided by source"

    source_platform = getattr(candidate, "source_platform", "reddit") or "reddit"
    if source_platform == "reddit":
        identity_line = f"Reddit post ID: {candidate.submission_id}"
    else:
        identity_line = f"Source ID: {candidate.submission_id}"
    detail_lines = [
        creator_line,
        f"Source: {candidate.source_url}",
        identity_line,
    ]
    reserved = sum(len(line) + 1 for line in detail_lines) + 1
    remaining = max(0, _MAX_MESSAGE_LENGTH - reserved - 1)

    text_parts: list[str] = []
    title = candidate.title.strip()
    body = (candidate.body or "").strip()
    if title:
        text_parts.append(title[:remaining])
        remaining = max(0, remaining - len(text_parts[-1]) - 1)
    if body and remaining > 0:
        text_parts.append(body[:remaining])

    parts = [*text_parts, "", *detail_lines]
    return "\n".join(parts).strip()[: _MAX_MESSAGE_LENGTH - 1]


class DiscordPublisher:
    """Send a bounded media payload as a real Discord file attachment."""

    def __init__(self, bot: Any, channel_id: int) -> None:
        self._bot = bot
        self._channel_id = channel_id

    @property
    def channel_id(self) -> int:
        return self._channel_id

    async def publish(
        self,
        *,
        filename: str,
        data: bytes,
        candidate: RedditCandidate,
    ) -> int:
        channel = self._bot.get_channel(self._channel_id)
        if channel is None:
            fetch_channel = getattr(self._bot, "fetch_channel", None)
            if callable(fetch_channel):
                channel = await fetch_channel(self._channel_id)
        if channel is None:
            raise DiscordPublicationError("Configured Discord channel is unavailable")

        file = discord.File(io.BytesIO(data), filename=filename)
        try:
            message = await channel.send(
                content=build_publication_message(candidate),
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
