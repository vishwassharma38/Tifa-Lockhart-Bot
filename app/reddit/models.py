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
class RedditCandidate:
    """Normalized Reddit submission fields used by the Phase 2 pipeline."""

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

    @property
    def caption(self) -> str | None:
        return self.body

    @property
    def normalized_source_url(self) -> str:
        return normalize_reddit_source_url(self.source_url)
