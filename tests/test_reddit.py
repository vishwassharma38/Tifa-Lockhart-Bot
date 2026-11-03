from __future__ import annotations

import pytest

from app.reddit.connector import (
    RedditAccessNotApprovedError,
    RedditConfigurationError,
    RedditConnector,
    normalize_submission,
    parse_submission_id,
)

REDDIT_ENV = {
    "REDDIT_CLIENT_ID": "client-id",
    "REDDIT_CLIENT_SECRET": "client-secret",
    "REDDIT_USER_AGENT": "test-user-agent",
}


class FakeAuthor:
    def __init__(self, name: str | None) -> None:
        self.name = name


class FakeSubreddit:
    display_name = "TifaLockhart"


class FakeSubmission:
    def __init__(self, **overrides: object) -> None:
        self.id = "abc123"
        self.permalink = "/r/TifaLockhart/comments/abc123/a_post/"
        self.title = "Tifa at the bar"
        self.selftext = "Original artwork"
        self.author = FakeAuthor("artist_name")
        self.subreddit = FakeSubreddit()
        self.created_utc = 1_700_000_000
        self.url = "https://i.redd.it/example.jpeg"
        self.is_gallery = False
        self.is_video = False
        self.is_self = False
        self.post_hint = "image"
        for key, value in overrides.items():
            setattr(self, key, value)


class FakeReddit:
    def __init__(self, submission: FakeSubmission) -> None:
        self._submission = submission
        self.requested_ids: list[str] = []

    def submission(self, *, id: str) -> FakeSubmission:
        self.requested_ids.append(id)
        return self._submission


class FakeRedditFactory:
    def __init__(self, submission: FakeSubmission | None = None) -> None:
        self.submission = submission or FakeSubmission()
        self.calls: list[dict[str, object]] = []

    def __call__(self, **kwargs: object) -> FakeReddit:
        self.calls.append(kwargs)
        return FakeReddit(self.submission)


async def immediate_thread(function, *args, **kwargs):
    return function(*args, **kwargs)


def test_parse_submission_id_accepts_ids_and_urls() -> None:
    assert parse_submission_id("AbC123") == "abc123"
    assert parse_submission_id("https://www.reddit.com/r/Tifa/comments/xyz789/title/") == "xyz789"
    assert parse_submission_id("https://redd.it/xyz789") == "xyz789"

    with pytest.raises(ValueError):
        parse_submission_id("https://example.com/not-reddit")


def test_normalize_submission_preserves_available_fields() -> None:
    candidate = normalize_submission(FakeSubmission())

    assert candidate.submission_id == "abc123"
    assert candidate.source_url == "https://www.reddit.com/r/TifaLockhart/comments/abc123/a_post"
    assert candidate.title == "Tifa at the bar"
    assert candidate.body == "Original artwork"
    assert candidate.subreddit == "TifaLockhart"
    assert candidate.author_name == "artist_name"
    assert candidate.author_profile_url == "https://www.reddit.com/user/artist_name"
    assert candidate.media_url == "https://i.redd.it/example.jpeg"
    assert candidate.extraction_reason is None


@pytest.mark.parametrize("author", [None, FakeAuthor("[deleted]"), FakeAuthor("")])
def test_normalize_submission_leaves_missing_attribution_missing(author) -> None:
    candidate = normalize_submission(FakeSubmission(author=author))

    assert candidate.author_name is None
    assert candidate.author_profile_url is None


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"is_gallery": True}, "reddit_gallery_not_supported_in_phase2"),
        ({"url": "https://v.redd.it/abc"}, "reddit_hosted_video_not_supported_in_phase2"),
        (
            {"url": "https://preview.redd.it/abc.jpg"},
            "reddit_preview_is_not_the_original",
        ),
        (
            {"url": "https://www.reddit.com/r/Tifa/comments/other/"},
            "submission_is_not_a_direct_media_url",
        ),
        (
            {"url": "https://example.com/artwork.html"},
            "media_url_extension_not_supported",
        ),
    ],
)
def test_normalize_submission_records_clear_skip_reasons(overrides, reason) -> None:
    candidate = normalize_submission(FakeSubmission(**overrides))

    assert candidate.media_url is None
    assert candidate.extraction_reason == reason


async def test_gate_disabled_makes_no_praw_call(make_settings) -> None:
    factory = FakeRedditFactory()
    connector = RedditConnector(
        make_settings(REDDIT_ACCESS_APPROVED="false"),
        reddit_factory=factory,
        to_thread=immediate_thread,
    )

    with pytest.raises(RedditAccessNotApprovedError):
        await connector.fetch_candidate("abc123")

    assert factory.calls == []


async def test_approved_without_credentials_makes_no_praw_call(make_settings) -> None:
    factory = FakeRedditFactory()
    connector = RedditConnector(
        make_settings(REDDIT_ACCESS_APPROVED="true"),
        reddit_factory=factory,
        to_thread=immediate_thread,
    )

    with pytest.raises(RedditConfigurationError):
        await connector.fetch_candidate("abc123")

    assert factory.calls == []


async def test_approved_connector_fetches_and_normalizes(make_settings) -> None:
    factory = FakeRedditFactory()
    settings = make_settings(REDDIT_ACCESS_APPROVED="true", **REDDIT_ENV)
    connector = RedditConnector(
        settings,
        reddit_factory=factory,
        to_thread=immediate_thread,
    )

    candidate = await connector.fetch_candidate("https://redd.it/abc123")

    assert candidate.submission_id == "abc123"
    assert factory.calls == [
        {
            "client_id": "client-id",
            "client_secret": "client-secret",
            "user_agent": "test-user-agent",
        }
    ]
