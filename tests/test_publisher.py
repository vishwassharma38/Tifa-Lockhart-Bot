from __future__ import annotations

from datetime import UTC, datetime

import discord
import pytest

from app.bot.publisher import (
    DiscordPublicationError,
    DiscordPublicationRejectedError,
    DiscordPublisher,
    MarkerLookup,
    PublicationContext,
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
    def __init__(
        self,
        channel: FakeChannel | None,
        *,
        channels: dict[int, FakeChannel] | None = None,
    ) -> None:
        self.channel = channel
        self.channels = channels or {}
        self.requested_channel_ids: list[int] = []

    def get_channel(self, channel_id: int) -> FakeChannel | None:
        self.requested_channel_ids.append(channel_id)
        if self.channels:
            return self.channels.get(channel_id)
        return self.channel


def test_message_includes_title_creator_and_clickable_source() -> None:
    message = build_publication_message(make_candidate())

    assert "Tifa at the bar" in message
    assert "Original artwork" in message
    assert "Creator: [artist\\_name](https://www.reddit.com/user/artist_name)" in message
    assert "[Original source](https://www.reddit.com/r/Tifa/comments/abc123/a_post)" in message
    assert "Source ID: abc123" in message


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
    assert call["suppress_embeds"] is True
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


class FakeFooter:
    def __init__(self, text: str) -> None:
        self.text = text


class FakeEmbed:
    def __init__(self, footer_text: str) -> None:
        self.footer = FakeFooter(footer_text)


class FakeHistoryMessage:
    def __init__(
        self,
        message_id: int,
        *,
        content: str = "",
        embed_footers: tuple[str, ...] = (),
    ) -> None:
        self.id = message_id
        self.content = content
        self.embeds = [FakeEmbed(text) for text in embed_footers]


class FakeHistoryChannel(FakeChannel):
    def __init__(self, messages: list[FakeHistoryMessage]) -> None:
        super().__init__()
        self.messages = messages

    async def history(self, *, limit: int = 100):
        for message in self.messages[:limit]:
            yield message


def test_neutral_context_message_includes_category_marker_and_web_source() -> None:
    context = PublicationContext(
        title="Tifa at the bar",
        caption="Original artwork",
        creator_name="artist",
        creator_profile_url="https://example.com/artist",
        source_url="https://example.com/post",
        source_platform="web",
        source_post_id="https://example.com/post",
        category="fanart",
        publication_marker="tifa-digest-publication:abc-123",
    )

    message = build_publication_message(context)

    assert "Tifa at the bar" in message
    assert "Category: fanart" in message
    assert "Creator: [artist](https://example.com/artist)" in message
    assert "[Original source](https://example.com/post)" in message
    assert "Source platform: web" in message
    assert "Reddit post ID" not in message
    assert "Source ID:" not in message
    assert "Publication: tifa-digest-publication:abc-123" in message


def test_web_media_record_does_not_receive_fake_reddit_identity() -> None:
    context = PublicationContext.from_media_record(
        {"title": "Tifa fanart", "category": "fanart"},
        {
            "title": "Tifa fanart",
            "source_platform": "web",
            "source_post_id": "https://example.com/tifa",
            "source_url": "https://example.com/tifa",
            "creator_name": None,
        },
        publication_marker="tifa-digest-publication:abc-123",
    )

    message = build_publication_message(context)

    assert "Source platform: web" in message
    assert "Source ID:" not in message
    assert "Reddit post ID" not in message
    assert "Creator: not provided by source" in message
    assert "Category: fanart" in message


def test_creator_without_profile_is_reported_as_incomplete() -> None:
    message = build_publication_message(
        PublicationContext(
            title="Tifa",
            creator_name="artist",
            source_url="https://example.com/tifa",
        )
    )

    assert "Creator: artist (profile not provided by source)" in message


def test_all_distinct_source_associations_are_clickable_once() -> None:
    context = PublicationContext.from_media_record(
        {"title": "Tifa", "media_type": "animation"},
        [
            {
                "source_url": "https://example.com/post",
                "creator_name": "Artist One",
                "creator_profile_url": "https://example.com/artist-one",
            },
            {
                "source_url": "https://example.com/post",
                "creator_name": "Artist Two",
                "creator_profile_url": "https://example.com/artist-two",
            },
            {"source_url": "https://other.example/post"},
        ],
    )

    message = build_publication_message(context)

    assert "TIFA ARCHIVE · ANIMATION" in message
    assert "Artist One" in message and "Artist Two" in message
    assert message.count("https://example.com/post") == 1
    assert "https://other.example/post" in message
    assert "[Original source](https://example.com/post)" in message


def test_malformed_and_dangerous_urls_are_omitted() -> None:
    context = PublicationContext(
        title="Tifa",
        source_url="javascript:alert(1)",
        creator_name="Artist",
        creator_profile_url="https://example.com:invalid/profile",
    )

    message = build_publication_message(context)

    assert "javascript:" not in message
    assert "example.com:invalid" not in message
    assert "Creator: Artist (profile not provided by source)" in message


def test_oversized_attribution_fails_before_discord_send() -> None:
    sources = [{"source_url": f"https://example.com/{index}/" + "x" * 100} for index in range(30)]
    context = PublicationContext.from_media_record({"media_type": "image"}, sources)
    with pytest.raises(DiscordPublicationRejectedError, match="message length"):
        build_publication_message(context)


async def test_publisher_uses_per_call_channel_override() -> None:
    default_channel = FakeChannel()
    mapped_channel = FakeChannel()
    bot = FakeBot(
        None,
        channels={111: default_channel, 222: mapped_channel},
    )
    publisher = DiscordPublisher(bot, 111)

    await publisher.publish(
        filename="tifa.png",
        data=b"media-bytes",
        context=PublicationContext(title="Tifa", category="fanart"),
        channel_id=222,
    )

    assert bot.requested_channel_ids == [222]
    assert len(mapped_channel.calls) == 1
    assert default_channel.calls == []


async def test_find_publication_marker_matches_message_content() -> None:
    channel = FakeHistoryChannel(
        [
            FakeHistoryMessage(1, content="unrelated"),
            FakeHistoryMessage(2, content="Publication: tifa-digest-publication:abc"),
        ]
    )
    publisher = DiscordPublisher(FakeBot(channel), 1234)

    lookup = await publisher.find_publication_marker(
        channel_id=1234,
        marker="tifa-digest-publication:abc",
    )

    assert lookup == MarkerLookup(message_id=2, conclusive=True)


async def test_find_publication_marker_matches_embed_footer() -> None:
    channel = FakeHistoryChannel(
        [FakeHistoryMessage(3, embed_footers=("Publication: tifa-digest-publication:abc",))]
    )
    publisher = DiscordPublisher(FakeBot(channel), 1234)

    lookup = await publisher.find_publication_marker(
        channel_id=1234,
        marker="tifa-digest-publication:abc",
    )

    assert lookup == MarkerLookup(message_id=3, conclusive=True)


async def test_find_publication_marker_is_conclusive_when_history_is_short() -> None:
    channel = FakeHistoryChannel([FakeHistoryMessage(1, content="unrelated")])
    publisher = DiscordPublisher(FakeBot(channel), 1234)

    lookup = await publisher.find_publication_marker(
        channel_id=1234,
        marker="tifa-digest-publication:missing",
        limit=10,
    )

    assert lookup == MarkerLookup(message_id=None, conclusive=True)


async def test_find_publication_marker_is_inconclusive_when_page_is_full() -> None:
    channel = FakeHistoryChannel(
        [FakeHistoryMessage(index, content="unrelated") for index in range(2)]
    )
    publisher = DiscordPublisher(FakeBot(channel), 1234)

    lookup = await publisher.find_publication_marker(
        channel_id=1234,
        marker="tifa-digest-publication:missing",
        limit=2,
    )

    assert lookup == MarkerLookup(message_id=None, conclusive=False)


async def test_find_publication_marker_requires_history_support() -> None:
    publisher = DiscordPublisher(FakeBot(FakeChannel()), 1234)

    with pytest.raises(DiscordPublicationError, match="history is unavailable"):
        await publisher.find_publication_marker(
            channel_id=1234,
            marker="tifa-digest-publication:abc",
        )


async def test_find_publication_marker_reports_history_errors_safely() -> None:
    class FailingHistoryChannel(FakeChannel):
        async def history(self, *, limit: int = 100):
            del limit
            raise RuntimeError("discord secret-looking detail")
            yield  # pragma: no cover

    publisher = DiscordPublisher(FakeBot(FailingHistoryChannel(error=None)), 1234)

    with pytest.raises(DiscordPublicationError) as exc_info:
        await publisher.find_publication_marker(
            channel_id=1234,
            marker="tifa-digest-publication:abc",
        )

    assert "secret-looking detail" not in str(exc_info.value)
    assert "RuntimeError" in str(exc_info.value)
