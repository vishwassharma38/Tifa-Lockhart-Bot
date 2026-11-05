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
        ({"is_gallery": True}, "gallery_metadata_missing"),
        ({"url": "https://v.redd.it/abc"}, "reddit_video_metadata_missing"),
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


def test_gallery_uses_original_assets_and_keeps_per_item_ids() -> None:
    submission = FakeSubmission(
        is_gallery=True,
        url="https://www.reddit.com/gallery/abc123",
        gallery_data={
            "items": [
                {"media_id": "one", "caption": "first"},
                {"media_id": "two"},
                {"media_id": "three"},
                {"media_id": "missing"},
                {"media_id": "bad"},
            ]
        },
        media_metadata={
            "one": {
                "status": "valid",
                "e": "Image",
                "m": "image/png",
                "s": {
                    "u": "https://i.redd.it/one.png",
                    "x": "https://preview.redd.it/one.png",
                },
            },
            "two": {
                "status": "valid",
                "e": "Image",
                "m": "image/jpeg",
                "s": {
                    "u": "https://i.redd.it/two.jpg",
                    "x": "https://preview.redd.it/two.jpg",
                },
            },
            "three": {
                "status": "valid",
                "e": "Image",
                "m": "image/png",
                "s": {"u": "https://preview.redd.it/three.png?width=640"},
            },
            "bad": {
                "status": "failed",
                "e": "Image",
                "m": "image/png",
                "s": {"u": "https://i.redd.it/bad.png"},
            },
        },
    )

    candidate = normalize_submission(submission)

    assert candidate.media_url is None
    assert [asset.source_media_id for asset in candidate.media_assets] == [
        "one",
        "two",
        "three",
    ]
    assert [asset.url for asset in candidate.media_assets] == [
        "https://i.redd.it/one.png",
        "https://i.redd.it/two.jpg",
        "https://preview.redd.it/three.png?width=640",
    ]
    assert [asset.gallery_index for asset in candidate.media_assets] == [0, 1, 2]
    assert [asset.source_metadata["url_selection"] for asset in candidate.media_assets] == [
        "reddit_metadata_s_u",
        "reddit_metadata_s_u",
        "reddit_metadata_s_u",
    ]
    assert candidate.media_assets[0].filename == "reddit-abc123-1.png"
    assert candidate.media_metadata["skipped_gallery_items"] == [
        {"index": 3, "media_id": "missing", "reason": "gallery_item_metadata_missing"},
        {"index": 4, "media_id": "bad", "reason": "gallery_item_not_valid"},
    ]
    assert candidate.media_metadata["gallery_diagnostics"] == {
        "total_entries": 5,
        "supported_entries": 3,
        "skipped_entries": 2,
        "skipped_items": candidate.media_metadata["skipped_gallery_items"],
    }


@pytest.mark.parametrize(
    ("category", "mime", "expected"),
    [
        ("Image", "image/jpeg", "jpg"),
        ("Image", "image/png", "png"),
        ("AnimatedImage", "image/gif", "gif"),
    ],
)
def test_gallery_resolves_real_reddit_category_and_mime(category, mime, expected) -> None:
    candidate = normalize_submission(
        FakeSubmission(
            is_gallery=True,
            gallery_data={"items": [{"media_id": "realid", "caption": "caption"}]},
            media_metadata={
                "realid": {
                    "status": "valid",
                    "e": category,
                    "m": mime,
                    "s": {"u": f"https://preview.redd.it/realid.{expected}?width=640"},
                }
            },
        )
    )
    asset = candidate.media_assets[0]
    assert asset.url == f"https://preview.redd.it/realid.{expected}?width=640"
    assert asset.source_metadata["url_selection"] == "reddit_metadata_s_u"
    assert asset.source_media_id == "realid"
    assert asset.source_metadata["caption"] == "caption"


@pytest.mark.parametrize(
    ("metadata", "reason"),
    [
        ({"status": "valid", "e": "Image"}, "gallery_item_missing_mime_type"),
        ({"status": "valid", "e": "Image", "m": "image/bmp"}, "gallery_item_unsupported_mime_type"),
        ({"status": "failed", "e": "Image", "m": "image/png"}, "gallery_item_not_valid"),
        (
            {"status": "valid", "e": "RedditVideo", "m": "video/mp4"},
            "gallery_item_reddit_video_unsupported",
        ),
        (
            {"status": "valid", "e": "Unknown", "m": "image/png"},
            "gallery_item_unsupported_category",
        ),
    ],
)
def test_gallery_reports_specific_unsupported_reasons(metadata, reason) -> None:
    candidate = normalize_submission(
        FakeSubmission(
            is_gallery=True,
            gallery_data={"items": [{"media_id": "item"}]},
            media_metadata={"item": metadata},
        )
    )
    assert candidate.media_assets == ()
    assert candidate.media_metadata["skipped_gallery_items"][0]["reason"] == reason


def test_gallery_missing_id_and_mixed_order_and_preview_rejection() -> None:
    candidate = normalize_submission(
        FakeSubmission(
            is_gallery=True,
            gallery_data={"items": [{"media_id": "a"}, {}, {"media_id": "b"}]},
            media_metadata={
                "a": {
                    "status": "valid",
                    "e": "Image",
                    "m": "image/png",
                    "s": {"u": "https://preview.redd.it/a.png"},
                },
                "b": {
                    "status": "valid",
                    "e": "Image",
                    "m": "image/jpeg",
                    "s": {"u": "https://preview.redd.it/b.jpg"},
                },
            },
        )
    )
    assert [asset.source_media_id for asset in candidate.media_assets] == ["a", "b"]
    assert [asset.gallery_index for asset in candidate.media_assets] == [0, 2]
    assert [asset.url for asset in candidate.media_assets] == [
        "https://preview.redd.it/a.png",
        "https://preview.redd.it/b.jpg",
    ]
    assert all(
        asset.source_metadata["url_selection"] == "reddit_metadata_s_u"
        for asset in candidate.media_assets
    )
    assert candidate.media_metadata["skipped_gallery_items"] == [
        {"index": 1, "reason": "gallery_item_missing_media_id"}
    ]


def test_gallery_missing_s_u_uses_explicit_fallback() -> None:
    candidate = normalize_submission(
        FakeSubmission(
            is_gallery=True,
            gallery_data={"items": [{"media_id": "fallback"}]},
            media_metadata={
                "fallback": {"status": "valid", "e": "Image", "m": "image/jpeg", "s": {}}
            },
        )
    )

    asset = candidate.media_assets[0]
    assert asset.url == "https://i.redd.it/fallback.jpg"
    assert asset.source_metadata["url_selection"] == "gallery_id_fallback"
    assert asset.source_metadata["url_selection_reason"] == "metadata_s_u_missing"


def test_gallery_decodes_html_escaped_query_without_changing_percent_encoding() -> None:
    candidate = normalize_submission(
        FakeSubmission(
            is_gallery=True,
            gallery_data={"items": [{"media_id": "encoded"}]},
            media_metadata={
                "encoded": {
                    "status": "valid",
                    "e": "Image",
                    "m": "image/jpeg",
                    "s": {"u": "https://preview.redd.it/a%2Fb.jpg?width=640&amp;s=abc%2Fdef"},
                }
            },
        )
    )

    asset = candidate.media_assets[0]
    assert asset.url == "https://preview.redd.it/a%2Fb.jpg?width=640&s=abc%2Fdef"
    assert asset.source_metadata["url_selection"] == "reddit_metadata_s_u"


def test_gallery_distinguishes_missing_malformed_and_invalid_entries() -> None:
    candidate = normalize_submission(
        FakeSubmission(
            is_gallery=True,
            gallery_data={
                "items": [
                    {"media_id": "missing"},
                    {"media_id": "malformed"},
                    {"media_id": "bad_url"},
                    {},
                    {"media_id": "malformed_s"},
                ]
            },
            media_metadata={
                "malformed": "not-an-object",
                "bad_url": {
                    "status": "valid",
                    "e": "Image",
                    "m": "image/png",
                    "s": {"u": "javascript:alert(1)"},
                },
                "malformed_s": {
                    "status": "valid",
                    "e": "Image",
                    "m": "image/png",
                    "s": "not-an-object",
                },
            },
        )
    )

    assert len(candidate.media_assets) == 2
    asset = candidate.media_assets[0]
    assert asset.url == "https://i.redd.it/bad_url.png"
    assert asset.source_metadata["url_selection"] == "gallery_id_fallback"
    assert asset.source_metadata["url_selection_reason"] == "metadata_s_u_invalid_url"
    malformed_s_asset = candidate.media_assets[1]
    assert malformed_s_asset.url == "https://i.redd.it/malformed_s.png"
    assert malformed_s_asset.source_metadata["url_selection_reason"] == "metadata_s_malformed"
    assert candidate.media_metadata["skipped_gallery_items"] == [
        {"index": 0, "media_id": "missing", "reason": "gallery_item_metadata_missing"},
        {"index": 1, "media_id": "malformed", "reason": "gallery_item_metadata_malformed"},
        {"index": 3, "reason": "gallery_item_missing_media_id"},
    ]


def test_gallery_mixed_items_preserve_order_and_original_indexes() -> None:
    candidate = normalize_submission(
        FakeSubmission(
            is_gallery=True,
            gallery_data={
                "items": [
                    {"media_id": "first"},
                    {"media_id": "missing"},
                    {"media_id": "third"},
                    {"media_id": "fourth"},
                ]
            },
            media_metadata={
                "first": {
                    "status": "valid",
                    "e": "Image",
                    "m": "image/png",
                    "s": {"u": "https://i.redd.it/first.png"},
                },
                "third": {"status": "valid", "e": "Image", "m": "image/jpeg"},
                "fourth": {
                    "status": "valid",
                    "e": "Image",
                    "m": "image/jpeg",
                    "s": {"u": "https://preview.redd.it/fourth.jpg?width=800"},
                },
            },
        )
    )

    assert [asset.url for asset in candidate.media_assets] == [
        "https://i.redd.it/first.png",
        "https://i.redd.it/third.jpg",
        "https://preview.redd.it/fourth.jpg?width=800",
    ]
    assert [asset.source_metadata["url_selection"] for asset in candidate.media_assets] == [
        "reddit_metadata_s_u",
        "gallery_id_fallback",
        "reddit_metadata_s_u",
    ]
    assert [asset.gallery_index for asset in candidate.media_assets] == [0, 2, 3]
    assert [asset.filename for asset in candidate.media_assets] == [
        "reddit-abc123-1.png",
        "reddit-abc123-3.jpg",
        "reddit-abc123-4.jpg",
    ]


def test_reddit_hosted_video_uses_public_fallback_and_requires_download() -> None:
    submission = FakeSubmission(
        is_video=True,
        url="https://v.redd.it/abc123",
        media={
            "reddit_video": {
                "fallback_url": "https://v.redd.it/abc123/CMAF_720.mp4?source=fallback",
                "dash_url": "https://v.redd.it/abc123/DASHPlaylist.mpd",
                "hls_url": "https://v.redd.it/abc123/HLSPlaylist.m3u8",
                "width": 1280,
                "height": 720,
                "duration": 12,
                "is_gif": False,
            }
        },
    )

    candidate = normalize_submission(submission)

    assert candidate.media_url is None
    assert len(candidate.media_assets) == 1
    asset = candidate.media_assets[0]
    assert asset.url == "https://v.redd.it/abc123/CMAF_720.mp4?source=fallback"
    assert asset.download_url == "https://www.reddit.com/r/TifaLockhart/comments/abc123/a_post/"
    assert asset.requires_video_download is True
    assert asset.expected_media_type == "video"
    assert asset.source_media_id == "https://v.redd.it/abc123/DASHPlaylist.mpd"


def test_gallery_without_supported_items_reports_reason() -> None:
    candidate = normalize_submission(
        FakeSubmission(
            is_gallery=True,
            gallery_data={"items": [{"media_id": "one"}]},
            media_metadata={"one": {"status": "valid", "e": "Image", "m": "image/bmp"}},
        )
    )

    assert candidate.media_assets == ()
    assert candidate.extraction_reason == "no_supported_gallery_media"
    assert candidate.media_metadata["skipped_gallery_items"] == [
        {"index": 0, "media_id": "one", "reason": "gallery_item_unsupported_mime_type"}
    ]


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


async def test_broad_search_uses_r_all_new_sort_and_worker_thread(make_settings) -> None:
    calls = []

    class SearchSubreddit:
        def search(self, phrase, *, sort, limit):
            calls.append(("search", phrase, sort, limit))
            return [FakeSubmission()]

    class SearchReddit:
        def subreddit(self, name):
            calls.append(("subreddit", name))
            return SearchSubreddit()

    def worker(function, *args, **kwargs):
        calls.append(("worker", function.__name__))
        return immediate_thread(function, *args, **kwargs)

    connector = RedditConnector(
        make_settings(REDDIT_ACCESS_APPROVED="true", **REDDIT_ENV),
        reddit_factory=lambda **_kwargs: SearchReddit(),
        to_thread=worker,
    )
    candidates = await connector.search("Tifa cosplay", limit=17)
    assert candidates[0].submission_id == "abc123"
    assert ("subreddit", "all") in calls
    assert ("search", "Tifa cosplay", "new", 17) in calls
    assert ("worker", "_search_sync") in calls
