from __future__ import annotations

from datetime import UTC, datetime

import discord
import pytest

from app.bot.publisher import (
    DiscordPublicationError,
    DiscordPublisher,
    build_publication_message,
)
from app.reddit.models import RedditCandidate


def make_candidate(**overrides: object) -> RedditCandidate:
    values = {
        "submission_id": "abc123",
        "permalink": "https://www.reddit.com/r/Tifa/comments/abc123/a_post",
        "source_url": "https://www.reddit.com/r/Tifa/comments/abc123/a_post",
        "title": "Tifa at the bar",
        "body": "Original artwork",
        "subreddit": "Tifa",
        "author_name": "artist_name",
        "author_profile_url": "https://www.reddit.com/user/artist_name",
        "created_at": datetime(2024, 1, 1, tzinfo=UTC),
        "media_url": "https://i.redd.it/example.png",
    }
    values.update(overrides)
    return RedditCandidate(**values)


class FakeMessage:
    id = 987654321


class FakeChannel:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[dict[str, object]] = []

    async def send(self, **kwargs: object) -> FakeMessage:
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return FakeMessage()


class FakeBot:
    def __init__(self, channel: FakeChannel | None) -> None:
        self.channel = channel

    def get_channel(self, _channel_id: int) -> FakeChannel | None:
        return self.channel


def test_message_includes_title_creator_and_clickable_source() -> None:
    message = build_publication_message(make_candidate())

    assert "Tifa at the bar" in message
    assert "Original artwork" in message
    assert "Creator: artist_name (https://www.reddit.com/user/artist_name)" in message
    assert "Source: https://www.reddit.com/r/Tifa/comments/abc123/a_post" in message
    assert "Reddit post ID: abc123" in message


def test_message_does_not_invent_missing_creator() -> None:
    message = build_publication_message(make_candidate(author_name=None, author_profile_url=None))

    assert "Creator: not provided by source" in message
    assert "artist_name" not in message


async def test_publisher_sends_an_actual_file_attachment() -> None:
    channel = FakeChannel()
    publisher = DiscordPublisher(FakeBot(channel), 1234)

    message_id = await publisher.publish(
        filename="reddit-abc123.png",
        data=b"media-bytes",
        candidate=make_candidate(),
    )

    assert message_id == 987654321
    assert len(channel.calls) == 1
    call = channel.calls[0]
    assert isinstance(call["file"], discord.File)
    assert call["file"].filename == "reddit-abc123.png"
    assert "Tifa at the bar" in str(call["content"])
    assert "https://www.reddit.com/r/Tifa/comments/abc123/a_post" in str(call["content"])


async def test_missing_channel_is_a_publication_failure() -> None:
    publisher = DiscordPublisher(FakeBot(None), 1234)

    with pytest.raises(DiscordPublicationError):
        await publisher.publish(
            filename="reddit-abc123.png",
            data=b"media-bytes",
            candidate=make_candidate(),
        )


async def test_discord_send_failure_is_reported_without_payload() -> None:
    channel = FakeChannel(error=RuntimeError("discord webhook secret-looking detail"))
    publisher = DiscordPublisher(FakeBot(channel), 1234)

    with pytest.raises(DiscordPublicationError) as exc_info:
        await publisher.publish(
            filename="reddit-abc123.png",
            data=b"media-bytes",
            candidate=make_candidate(),
        )

    assert "secret-looking detail" not in str(exc_info.value)
