"""Typed internal candidate representation for a Reddit submission."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit, urlunsplit


def normalize_reddit_source_url(url: str) -> str:
    """Return a stable Reddit permalink for provenance and deduplication."""
    parsed = urlsplit(url.strip())
    scheme = "https"
    host = (parsed.hostname or "www.reddit.com").lower()
    if host in {"reddit.com", "old.reddit.com"}:
        host = "www.reddit.com"
    path = parsed.path.rstrip("/") or "/"
    return urlunsplit((scheme, host, path, "", ""))


@dataclass(frozen=True, slots=True)
class MediaAsset:
    """One original media file extracted from a Reddit submission."""

    url: str
    source_media_id: str | None = None
    filename: str | None = None
    expected_media_type: str | None = None
    gallery_index: int | None = None
    source_metadata: dict[str, Any] = field(default_factory=dict)
    requires_video_download: bool = False
    download_url: str | None = None

    @property
    def asset_key(self) -> str:
        """Stable per-submission identity used to avoid duplicate downloads."""
        return self.source_media_id or self.url


@dataclass(frozen=True, slots=True)
class RedditCandidate:
    """Normalized Reddit submission fields used by the media pipeline."""

    submission_id: str
    permalink: str
    source_url: str
    title: str
    body: str | None
    subreddit: str
    author_name: str | None
    author_profile_url: str | None
    created_at: datetime
    media_url: str | None
    media_metadata: dict[str, Any] = field(default_factory=dict)
    extraction_reason: str | None = None
    media_assets: tuple[MediaAsset, ...] = ()

    @property
    def caption(self) -> str | None:
        return self.body

    @property
    def normalized_source_url(self) -> str:
        return normalize_reddit_source_url(self.source_url)

    @property
    def assets(self) -> tuple[MediaAsset, ...]:
        """Return typed assets, falling back to the Phase 2 single-image field."""
        if self.media_assets:
            return self.media_assets
        if self.media_url:
            return (MediaAsset(url=self.media_url),)
        return ()
