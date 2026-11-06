"""Shared normalized candidate models for external web discoveries."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.discovery.url_safety import normalize_url
from app.reddit.models import MediaAsset


def web_submission_id(normalized_url: str) -> str:
    """Return a stable, filename-safe identity for one external page or file."""
    digest = hashlib.sha256(normalized_url.encode("utf-8", errors="replace")).hexdigest()
    return f"web-{digest[:20]}"


@dataclass(frozen=True, slots=True)
class WebCandidate:
    """External page candidate shaped for the existing Phase 2 pipeline.

    ``RedditCandidate`` remains untouched. This model supplies the same fields
    the shared pipeline consumes while adding truthful web provenance fields
    that the repository reads dynamically.
    """

    page_url: str
    title: str | None = None
    body: str | None = None
    author_name: str | None = None
    author_profile_url: str | None = None
    media_assets: tuple[MediaAsset, ...] = ()
    media_metadata: dict[str, Any] = field(default_factory=dict)
    extraction_reason: str | None = None
    discovery_id: str | None = None
    source_type: str = "brave_html"
    source_platform: str = "web"
    created_at: datetime = field(default_factory=lambda: datetime.now(tz=UTC))
    normalized_url: str | None = None
    crawl_depth: int = 0

    def __post_init__(self) -> None:
        if self.normalized_url is None:
            object.__setattr__(self, "normalized_url", normalize_url(self.page_url).normalized)

    @property
    def submission_id(self) -> str:
        return web_submission_id(self.normalized_source_url)

    @property
    def source_post_id(self) -> str:
        return self.submission_id

    @property
    def source_url(self) -> str:
        return self.page_url

    @property
    def permalink(self) -> str:
        return self.page_url

    @property
    def normalized_source_url(self) -> str:
        return self.normalized_url or normalize_url(self.page_url).normalized

    @property
    def subreddit(self) -> str:
        return ""

    @property
    def media_url(self) -> str | None:
        return self.media_assets[0].url if self.media_assets else None

    @property
    def assets(self) -> tuple[MediaAsset, ...]:
        return self.media_assets

    @property
    def caption(self) -> str | None:
        return self.body
