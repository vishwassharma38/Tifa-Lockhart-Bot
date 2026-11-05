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
from app.media.validation import normalize_extension
from app.reddit.models import (
    MediaAsset,
    RedditCandidate,
    normalize_reddit_source_url,
)

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


def _http_url(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    cleaned = value.strip()
    parsed = urlparse(cleaned)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return cleaned


def _extension_for(url: str) -> str | None:
    parsed = urlparse(url)
    return normalize_extension(PurePosixPath(parsed.path).suffix)


def _direct_asset(submission: Any) -> tuple[MediaAsset | None, str | None]:
    url = getattr(submission, "url", None)
    if not isinstance(url, str) or not url.strip():
        return None, "submission_has_no_media_url"

    parsed = urlparse(url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None, "media_url_is_not_public_http"

    host = (parsed.hostname or "").lower()
    if host in _PREVIEW_HOSTS:
        return None, "reddit_preview_is_not_the_original"
    if host in _REDDIT_HOSTS or "/comments/" in parsed.path:
        return None, "submission_is_not_a_direct_media_url"

    extension = PurePosixPath(parsed.path).suffix.lower().lstrip(".")
    if extension not in _SUPPORTED_MEDIA_EXTENSIONS:
        return None, "media_url_extension_not_supported"
    if host in _REDDIT_VIDEO_HOSTS:
        return None, "reddit_video_metadata_missing"

    normalized = normalize_extension(extension) or extension
    return (
        MediaAsset(
            url=url.strip(),
            expected_media_type="video" if normalized in {"mp4", "webm"} else "image",
            source_metadata={"extraction": "direct_url"},
        ),
        None,
    )


def _original_gallery_url(
    metadata: dict[str, Any],
    media_id: str,
    extension: str | None,
) -> str | None:
    original = metadata.get("s")
    if isinstance(original, dict):
        for key in ("u", "gif", "mp4"):
            url = _http_url(original.get(key))
            if url is not None and urlparse(url).hostname not in _PREVIEW_HOSTS:
                return url

    # Reddit gallery entries expose preview URLs in ``s`` but the original is
    # addressable by media ID. Construct it instead of accepting a thumbnail.
    if extension in _SUPPORTED_MEDIA_EXTENSIONS:
        return f"https://i.redd.it/{media_id}.{extension}"
    return None


def _extract_gallery(
    submission: Any,
    submission_id: str,
) -> tuple[list[MediaAsset], list[dict[str, Any]], str | None]:
    gallery_data = getattr(submission, "gallery_data", None)
    media_metadata = getattr(submission, "media_metadata", None)

    if not isinstance(gallery_data, dict):
        return [], [{"reason": "gallery_metadata_missing"}], "gallery_metadata_missing"
    items = gallery_data.get("items")
    if not isinstance(items, list) or not items:
        return [], [{"reason": "gallery_items_missing"}], "gallery_items_missing"
    if not isinstance(media_metadata, dict):
        return (
            [],
            [{"reason": "gallery_media_metadata_missing"}],
            ("gallery_media_metadata_missing"),
        )

    assets: list[MediaAsset] = []
    skipped: list[dict[str, Any]] = []
    seen: set[str] = set()

    for index, item in enumerate(items):
        if not isinstance(item, dict):
            skipped.append({"index": index, "reason": "gallery_item_malformed"})
            continue

        media_id = item.get("media_id") or item.get("id")
        if not isinstance(media_id, str) or not media_id.strip():
            skipped.append({"index": index, "reason": "gallery_item_missing_media_id"})
            continue
        media_id = media_id.strip()

        metadata = media_metadata.get(media_id)
        if not isinstance(metadata, dict):
            skipped.append(
                {"index": index, "media_id": media_id, "reason": "gallery_item_unavailable"}
            )
            continue
        status = metadata.get("status")
        if isinstance(status, str) and status.lower() not in {"valid", "unprocessed"}:
            skipped.append(
                {"index": index, "media_id": media_id, "reason": "gallery_item_not_valid"}
            )
            continue

        declared_extension = normalize_extension(metadata.get("e"))
        url = _original_gallery_url(metadata, media_id, declared_extension)
        extension = declared_extension or (_extension_for(url) if url else None)
        if extension is None or extension not in _SUPPORTED_MEDIA_EXTENSIONS:
            skipped.append(
                {"index": index, "media_id": media_id, "reason": "gallery_item_unsupported"}
            )
            continue
        if url is None:
            skipped.append(
                {
                    "index": index,
                    "media_id": media_id,
                    "reason": "gallery_item_original_unavailable",
                }
            )
            continue

        if media_id in seen or url in seen:
            skipped.append(
                {"index": index, "media_id": media_id, "reason": "gallery_item_duplicate"}
            )
            continue
        seen.add(media_id)
        seen.add(url)

        mime_type = metadata.get("m") if isinstance(metadata.get("m"), str) else None
        assets.append(
            MediaAsset(
                url=url,
                source_media_id=media_id,
                filename=f"reddit-{submission_id}-{index + 1}.{extension}",
                expected_media_type="video" if extension in {"mp4", "webm"} else "image",
                gallery_index=index,
                source_metadata={
                    "media_id": media_id,
                    "gallery_index": index,
                    "declared_extension": metadata.get("e"),
                    "declared_mime_type": mime_type,
                    "caption": item.get("caption"),
                },
            )
        )

    reason = None if assets else "no_supported_gallery_media"
    return assets, skipped, reason


def _reddit_video_metadata(submission: Any) -> dict[str, Any] | None:
    for attribute in ("media", "secure_media"):
        media = getattr(submission, attribute, None)
        if isinstance(media, dict):
            reddit_video = media.get("reddit_video")
            if isinstance(reddit_video, dict):
                return reddit_video
    return None


def _extract_video(
    submission: Any,
    submission_id: str,
    permalink: str,
) -> tuple[MediaAsset | None, str | None]:
    reddit_video = _reddit_video_metadata(submission)
    if reddit_video is None:
        return None, "reddit_video_metadata_missing"

    url = _http_url(reddit_video.get("fallback_url")) or _http_url(permalink)
    if url is None:
        return None, "reddit_video_not_publicly_accessible"

    metadata = {
        "is_gif": bool(reddit_video.get("is_gif", False)),
        "width": reddit_video.get("width"),
        "height": reddit_video.get("height"),
        "duration": reddit_video.get("duration"),
        "has_hls": bool(reddit_video.get("hls_url")),
        "has_dash": bool(reddit_video.get("dash_url")),
        "fallback_url": _http_url(reddit_video.get("fallback_url")),
    }
    # yt-dlp needs the Reddit post to select the separate audio and video
    # streams; the fallback URL is often a silent video-only DASH stream.
    download_url = _http_url(permalink) or url
    return (
        MediaAsset(
            url=url,
            source_media_id=str(reddit_video.get("dash_url") or url),
            filename=f"reddit-{submission_id}.mp4",
            expected_media_type="video",
            source_metadata=metadata,
            requires_video_download=True,
            download_url=download_url,
        ),
        None,
    )


def _extract_assets(
    submission: Any,
    submission_id: str,
    permalink: str,
) -> tuple[list[MediaAsset], list[dict[str, Any]], str | None, str | None]:
    """Return assets, skipped gallery entries, reason, and the Phase 2 media URL."""
    if bool(getattr(submission, "is_gallery", False)):
        assets, skipped, reason = _extract_gallery(submission, submission_id)
        return assets, skipped, reason, None

    if bool(getattr(submission, "is_video", False)):
        asset, reason = _extract_video(submission, submission_id, permalink)
        return ([asset] if asset else []), [], reason, None

    raw_url = getattr(submission, "url", None)
    host = ""
    if isinstance(raw_url, str):
        host = (urlparse(raw_url).hostname or "").lower()
    if host in _REDDIT_VIDEO_HOSTS:
        asset, reason = _extract_video(submission, submission_id, permalink)
        return ([asset] if asset else []), [], reason, None

    asset, reason = _direct_asset(submission)
    return ([asset] if asset else []), [], reason, (asset.url if asset else None)


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

    assets, skipped_gallery, extraction_reason, media_url = _extract_assets(
        submission, submission_id, permalink
    )
    raw_url = getattr(submission, "url", None)
    media_metadata = {
        "submission_url": raw_url if isinstance(raw_url, str) else None,
        "is_self": bool(getattr(submission, "is_self", False)),
        "is_video": bool(getattr(submission, "is_video", False)),
        "is_gallery": bool(getattr(submission, "is_gallery", False)),
        "post_hint": getattr(submission, "post_hint", None),
        "reddit_created_utc": created_utc if isinstance(created_utc, (int, float)) else None,
        "extraction_reason": extraction_reason,
        "asset_count": len(assets),
    }
    if skipped_gallery:
        media_metadata["skipped_gallery_items"] = skipped_gallery
    if bool(getattr(submission, "is_video", False)):
        reddit_video = _reddit_video_metadata(submission)
        if reddit_video is not None:
            media_metadata["reddit_video"] = {
                "width": reddit_video.get("width"),
                "height": reddit_video.get("height"),
                "duration": reddit_video.get("duration"),
                "is_gif": bool(reddit_video.get("is_gif", False)),
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
        media_assets=tuple(assets),
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
