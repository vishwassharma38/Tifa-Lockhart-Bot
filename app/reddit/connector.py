"""PRAW-backed Reddit connector with an explicit access gate."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import urlparse

import praw

from app.config import Settings
from app.reddit.models import RedditCandidate, normalize_reddit_source_url

_SUPPORTED_MEDIA_EXTENSIONS = frozenset({"jpg", "jpeg", "png", "webp", "gif", "mp4", "webm"})
_SUBMISSION_ID_PATTERN = re.compile(r"^[a-z0-9]+$", re.IGNORECASE)
_SUBMISSION_URL_PATTERN = re.compile(
    r"(?:reddit\.com/(?:r/[^/]+/)?comments/|redd\.it/)([a-z0-9]+)",
    re.IGNORECASE,
)
_REDDIT_HOSTS = frozenset({"reddit.com", "www.reddit.com", "old.reddit.com", "redd.it"})
_PREVIEW_HOSTS = frozenset({"preview.redd.it", "external-preview.redd.it"})
_REDDIT_VIDEO_HOSTS = frozenset({"v.redd.it"})


class RedditAccessNotApprovedError(RuntimeError):
    """Raised when production Reddit access is not explicitly approved."""


class RedditConfigurationError(RuntimeError):
    """Raised when Reddit is approved but its credentials are incomplete."""


class RedditFetchError(RuntimeError):
    """Raised when a Reddit submission cannot be read."""


def parse_submission_id(value: str) -> str:
    """Accept a bare Reddit ID or a submission URL and return the native ID."""
    candidate = value.strip()
    if _SUBMISSION_ID_PATTERN.fullmatch(candidate):
        return candidate.lower()

    match = _SUBMISSION_URL_PATTERN.search(candidate)
    if match:
        return match.group(1).lower()
    raise ValueError("A Reddit submission ID or permalink is required")


def _author_name(author: Any) -> str | None:
    if author is None:
        return None
    name = getattr(author, "name", None)
    if not isinstance(name, str):
        return None
    cleaned = name.strip()
    if not cleaned or cleaned.lower() == "[deleted]":
        return None
    return cleaned


def _extract_media_url(submission: Any) -> tuple[str | None, str | None]:
    if bool(getattr(submission, "is_gallery", False)):
        return None, "reddit_gallery_not_supported_in_phase2"

    url = getattr(submission, "url", None)
    if not isinstance(url, str) or not url.strip():
        return None, "submission_has_no_media_url"

    parsed = urlparse(url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None, "media_url_is_not_public_http"

    host = (parsed.hostname or "").lower()
    if host in _REDDIT_VIDEO_HOSTS:
        return None, "reddit_hosted_video_not_supported_in_phase2"
    if host in _PREVIEW_HOSTS:
        return None, "reddit_preview_is_not_the_original"
    if host in _REDDIT_HOSTS or "/comments/" in parsed.path:
        return None, "submission_is_not_a_direct_media_url"

    extension = PurePosixPath(parsed.path).suffix.lower().lstrip(".")
    if extension not in _SUPPORTED_MEDIA_EXTENSIONS:
        return None, "media_url_extension_not_supported"
    return url.strip(), None


def normalize_submission(submission: Any) -> RedditCandidate:
    """Normalize a PRAW submission into the internal candidate model."""
    submission_id = str(getattr(submission, "id", "") or "").strip().lower()
    if not submission_id:
        raise RedditFetchError("Reddit submission did not include a native ID")

    raw_permalink = str(getattr(submission, "permalink", "") or "").strip()
    if raw_permalink.startswith("/"):
        permalink = f"https://www.reddit.com{raw_permalink}"
    else:
        permalink = raw_permalink
    if not permalink:
        raise RedditFetchError("Reddit submission did not include a permalink")
    source_url = normalize_reddit_source_url(permalink)

    author_name = _author_name(getattr(submission, "author", None))
    author_profile_url = f"https://www.reddit.com/user/{author_name}" if author_name else None

    subreddit = getattr(getattr(submission, "subreddit", None), "display_name", None)
    if not isinstance(subreddit, str) or not subreddit.strip():
        subreddit = ""

    selftext = getattr(submission, "selftext", None)
    body = selftext.strip() if isinstance(selftext, str) and selftext.strip() else None

    created_utc = getattr(submission, "created_utc", None)
    try:
        created_at = datetime.fromtimestamp(float(created_utc), tz=UTC)
    except (TypeError, ValueError, OSError, OverflowError):
        created_at = datetime.now(tz=UTC)

    media_url, extraction_reason = _extract_media_url(submission)
    raw_url = getattr(submission, "url", None)
    media_metadata = {
        "submission_url": raw_url if isinstance(raw_url, str) else None,
        "is_self": bool(getattr(submission, "is_self", False)),
        "is_video": bool(getattr(submission, "is_video", False)),
        "is_gallery": bool(getattr(submission, "is_gallery", False)),
        "post_hint": getattr(submission, "post_hint", None),
        "reddit_created_utc": created_utc if isinstance(created_utc, (int, float)) else None,
        "extraction_reason": extraction_reason,
    }

    return RedditCandidate(
        submission_id=submission_id,
        permalink=permalink,
        source_url=source_url,
        title=str(getattr(submission, "title", "") or "").strip(),
        body=body,
        subreddit=subreddit,
        author_name=author_name,
        author_profile_url=author_profile_url,
        created_at=created_at,
        media_url=media_url,
        media_metadata=media_metadata,
        extraction_reason=extraction_reason,
    )


class RedditConnector:
    """Read one Reddit submission at a time through a gated PRAW client."""

    def __init__(
        self,
        settings: Settings,
        *,
        reddit_factory: Callable[..., Any] = praw.Reddit,
        to_thread: Callable[..., Awaitable[Any]] = asyncio.to_thread,
    ) -> None:
        self._settings = settings
        self._reddit_factory = reddit_factory
        self._to_thread = to_thread
        self._reddit: Any | None = None

    @property
    def access_approved(self) -> bool:
        return self._settings.reddit_access_approved

    def _ensure_access(self) -> None:
        if not self._settings.reddit_access_approved:
            raise RedditAccessNotApprovedError(
                "Reddit access is not approved; set REDDIT_ACCESS_APPROVED only after "
                "authorized access has been confirmed"
            )

        missing = [
            name
            for name, value in (
                ("REDDIT_CLIENT_ID", self._settings.reddit_client_id),
                ("REDDIT_CLIENT_SECRET", self._settings.reddit_client_secret),
                ("REDDIT_USER_AGENT", self._settings.reddit_user_agent),
            )
            if not value
        ]
        if missing:
            raise RedditConfigurationError(
                "Reddit access is approved but required credentials are missing: "
                + ", ".join(missing)
            )

    def _client(self) -> Any:
        self._ensure_access()
        if self._reddit is None:
            try:
                self._reddit = self._reddit_factory(
                    client_id=self._settings.reddit_client_id,
                    client_secret=self._settings.reddit_client_secret,
                    user_agent=self._settings.reddit_user_agent,
                )
            except Exception:
                raise RedditConfigurationError("Reddit client initialization failed") from None
        return self._reddit

    async def fetch_candidate(self, submission_reference: str) -> RedditCandidate:
        """Fetch and normalize one submission without blocking the event loop."""
        self._ensure_access()
        submission_id = parse_submission_id(submission_reference)
        return await self._to_thread(self._fetch_sync, submission_id)

    def _fetch_sync(self, submission_id: str) -> RedditCandidate:
        client = self._client()
        try:
            submission = client.submission(id=submission_id)
            return normalize_submission(submission)
        except (RedditAccessNotApprovedError, RedditConfigurationError):
            raise
        except Exception as exc:
            raise RedditFetchError(
                f"Reddit submission fetch failed ({type(exc).__name__})"
            ) from None
