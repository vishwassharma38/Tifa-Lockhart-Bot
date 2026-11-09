"""Bounded hourly Reddit collection through the shared media pipeline."""

from __future__ import annotations

import asyncio
import html
import inspect
import logging
import re
from collections import Counter
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from app.config import Settings
from app.discovery.extractor import is_direct_media_url
from app.discovery.url_safety import UrlSafetyError, normalize_url
from app.reddit.connector import (
    RedditConnector,
    normalize_submission,
)
from app.reddit.models import RedditCandidate

log = logging.getLogger(__name__)
_MARKDOWN_LINK = re.compile(r"\[([^\]\n]+)\]\((https?://[^\s<>]+)\)", re.IGNORECASE)
_PLAIN_URL = re.compile(r"https?://[^\s<>\[\]{}\"']+", re.IGNORECASE)
_REDDIT_OWNED_HOSTS = frozenset(
    {
        "reddit.com",
        "redd.it",
        "redditmedia.com",
        "redditstatic.com",
        "reddituploads.com",
        "reddit.map.fastly.net",
        "reddit-uploaded-media.s3-accelerate.amazonaws.com",
        "reddit-uploaded-video.s3-accelerate.amazonaws.com",
        "reddit-uploaded-media.s3.amazonaws.com",
        "reddit-uploaded-video.s3.amazonaws.com",
    }
)


@dataclass(frozen=True, slots=True)
class RedditSeedLink:
    original_url: str
    normalized_url: str
    link_source: str
    link_text: str | None = None


def _trim_url_punctuation(value: str) -> str:
    url = value.rstrip(".,!?;:")
    for opening, closing in (("(", ")"), ("[", "]"), ("{", "}")):
        while url.endswith(closing) and url.count(closing) > url.count(opening):
            url = url[:-1]
    return url


def _reddit_owned(hostname: str) -> bool:
    host = hostname.lower().rstrip(".")
    return any(host == domain or host.endswith("." + domain) for domain in _REDDIT_OWNED_HOSTS)


def extract_reddit_seed_links(candidate: RedditCandidate) -> list[RedditSeedLink]:
    """Extract normalized external HTML destinations from a normalized submission."""
    found: list[tuple[str, str, str | None]] = []
    submission_url = candidate.media_metadata.get("submission_url")
    if isinstance(submission_url, str) and submission_url.strip():
        found.append((submission_url.strip(), "submission_url", None))

    body = candidate.body or ""
    markdown_spans: list[tuple[int, int]] = []
    for match in _MARKDOWN_LINK.finditer(body):
        markdown_spans.append(match.span(2))
        found.append((match.group(2), "self_text", html.unescape(match.group(1)).strip() or None))
    for match in _PLAIN_URL.finditer(body):
        if any(start <= match.start() < end for start, end in markdown_spans):
            continue
        found.append((_trim_url_punctuation(match.group()), "self_text", None))

    links: list[RedditSeedLink] = []
    seen: set[str] = set()
    for raw_url, source, link_text in found:
        try:
            normalized = normalize_url(html.unescape(_trim_url_punctuation(raw_url))).normalized
            hostname = urlsplit(normalized).hostname or ""
        except (UrlSafetyError, ValueError):
            continue
        if _reddit_owned(hostname) or is_direct_media_url(normalized) or normalized in seen:
            continue
        if normalized == candidate.source_url or normalized == candidate.normalized_source_url:
            continue
        seen.add(normalized)
        links.append(RedditSeedLink(raw_url, normalized, source, link_text))
    return links


def _reddit_seed_payload(
    candidate: RedditCandidate, link: RedditSeedLink, *, discovered_at: str
) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "discovered_via": "reddit_link",
        "origin_source_type": "reddit",
        "reddit_submission_id": candidate.submission_id,
        "reddit_submission_url": candidate.source_url,
        "reddit_submission_title": candidate.title,
        "subreddit": candidate.subreddit,
        "link_source": link.link_source,
        "discovered_at": discovered_at,
    }
    search_phrase = candidate.media_metadata.get("search_phrase")
    if search_phrase:
        metadata["search_phrase"] = search_phrase
    if link.link_text:
        metadata["link_text"] = link.link_text
    return {
        "url": link.original_url,
        "normalized_url": link.normalized_url,
        "source_type": "reddit",
        "source_platform_id": candidate.submission_id,
        "title": candidate.title or None,
        "description": link.link_text[:500] or None if link.link_text else None,
        "domain": urlsplit(link.normalized_url).hostname,
        "depth": 0,
        "status": "queued",
        "metadata_json": metadata,
    }


def _accepts_keyword(function: Any, name: str) -> bool:
    try:
        parameters = inspect.signature(function).parameters
    except (TypeError, ValueError):
        return False
    return name in parameters or any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters.values()
    )


class RedditCollector:
    def __init__(
        self, settings: Settings, repository: Any, pipeline: Any, connector: RedditConnector
    ) -> None:
        self.settings, self.repository, self.pipeline, self.connector = (
            settings,
            repository,
            pipeline,
            connector,
        )
        self._lock = asyncio.Lock()

    @property
    def block_reason(self) -> str | None:
        if not self.settings.reddit_enabled:
            return "REDDIT_ENABLED is false"
        if not self.settings.reddit_access_approved:
            return "Reddit access approval has not been confirmed"
        if not all(
            (
                self.settings.reddit_client_id,
                self.settings.reddit_client_secret,
                self.settings.reddit_user_agent,
            )
        ):
            return "Reddit credentials are not configured"
        if getattr(self.repository, "configured", True) is False:
            return "Supabase is not configured"
        storage = getattr(self.pipeline, "_storage", None)
        if storage is not None and getattr(storage, "configured", True) is False:
            return "Private B2 storage is not configured"
        return None

    async def run(self, *, scheduled: bool = True) -> dict[str, int]:
        """Collect up to the configured unique submissions and return operational counts.

        Discovery, examined, and processed are submission-level counters. Stored,
        skipped, and media failures are asset-level (duplicates are separate); retries
        are attempts and never increment a result counter.
        """
        if self._lock.locked():
            return {
                "discovered_count": 0,
                "examined_count": 0,
                "duplicate_count": 0,
                "processed_count": 0,
                "stored_count": 0,
                "skipped_count": 0,
                "failed_count": 0,
            }
        async with self._lock:
            state = await self.repository.get_runtime_state()
            counters = {
                k: 0
                for k in (
                    "discovered_count",
                    "examined_count",
                    "duplicate_count",
                    "processed_count",
                    "stored_count",
                    "skipped_count",
                    "failed_count",
                )
            }
            if scheduled and not state.get("collection_enabled", False):
                return counters
            reason = self.block_reason
            job_id = await self.repository.create_job_run(
                {
                    "phrases": list(self.settings.reddit_search_phrases),
                    "limit": self.settings.reddit_max_items_per_run,
                    "reddit_seed_discovery_enabled": self.settings.reddit_seed_discovery_enabled,
                    "url_budget": self.settings.max_urls_per_job,
                    "blocked_reason": reason,
                }
            )
            errors: Counter[str] = Counter()
            if reason:
                await self.repository.finish_job_run(job_id, counters, "skipped", reason)
                log.warning("Reddit collection blocked: %s", reason)
                return counters
            seen: set[str] = set()
            seen_seed_urls: set[str] = set()
            seed_admitted = 0
            try:
                for phrase in self.settings.reddit_search_phrases:
                    remaining = self.settings.reddit_max_items_per_run - counters["examined_count"]
                    if remaining <= 0:
                        break
                    try:
                        submissions = await self.connector.search(phrase, limit=remaining)
                    except Exception as exc:
                        errors[f"query_failed:{type(exc).__name__}"] += 1
                        log.warning("Reddit query failed (%s)", type(exc).__name__)
                        continue
                    for submission in submissions:
                        if counters["examined_count"] >= self.settings.reddit_max_items_per_run:
                            break
                        counters["discovered_count"] += 1
                        sid = (
                            submission.submission_id
                            if isinstance(submission, RedditCandidate)
                            else str(getattr(submission, "id", "") or "").lower()
                        )
                        if not sid:
                            counters["failed_count"] += 1
                            errors["submission_id_missing"] += 1
                            continue
                        if sid in seen:
                            counters["duplicate_count"] += 1
                            continue
                        seen.add(sid)
                        counters["examined_count"] += 1
                        try:
                            candidate = (
                                submission
                                if isinstance(submission, RedditCandidate)
                                else normalize_submission(submission)
                            )
                            metadata = dict(candidate.media_metadata)
                            metadata.update(
                                {
                                    "search_phrase": phrase,
                                    "discovered_at": datetime.now(UTC).isoformat(),
                                }
                            )
                            candidate = replace(candidate, media_metadata=metadata)
                            await self.repository.upsert_discovery(candidate)
                            counters["processed_count"] += 1
                            if self.settings.reddit_seed_discovery_enabled:
                                try:
                                    seed_links = extract_reddit_seed_links(candidate)
                                except Exception as exc:
                                    log.warning(
                                        "Reddit seed extraction failed for %s (%s)",
                                        candidate.submission_id,
                                        type(exc).__name__,
                                    )
                                    seed_links = []
                                for link in seed_links:
                                    if link.normalized_url in seen_seed_urls:
                                        continue
                                    seen_seed_urls.add(link.normalized_url)
                                    if seed_admitted >= self.settings.max_urls_per_job:
                                        break
                                    try:
                                        payload = _reddit_seed_payload(
                                            candidate,
                                            link,
                                            discovered_at=datetime.now(UTC).isoformat(),
                                        )
                                        (
                                            _row,
                                            created,
                                        ) = await self.repository.upsert_discovery_record(payload)
                                    except Exception as exc:
                                        log.warning(
                                            "Reddit seed insert failed for %s (%s)",
                                            link.normalized_url,
                                            type(exc).__name__,
                                        )
                                        continue
                                    if created:
                                        seed_admitted += 1
                                        log.info(
                                            "Reddit crawler seed admitted: %s",
                                            link.normalized_url,
                                        )
                                    else:
                                        log.debug(
                                            "Reddit crawler seed already exists: %s",
                                            link.normalized_url,
                                        )
                            # yt-dlp performs its own segmented HTTP requests and
                            # does not use MediaFetcher's public-only resolver.
                            # Keep that legacy path available to manual /save,
                            # but do not allow it in unattended collection.
                            if any(asset.requires_video_download for asset in candidate.assets):
                                counters["skipped_count"] += len(candidate.assets)
                                continue
                            process = self.pipeline.process_candidate
                            kwargs = (
                                {"collection_run_id": job_id}
                                if _accepts_keyword(process, "collection_run_id")
                                else {}
                            )
                            results = await process(candidate, publish=False, **kwargs)
                            for result in results:
                                if result.status.value == "archived":
                                    counters["stored_count"] += 1
                                elif result.status.value == "skipped":
                                    counters["skipped_count"] += 1
                                elif result.status.value in {"failed", "recoverable"}:
                                    counters["failed_count"] += 1
                                    category = (
                                        result.details.get("failure_category") or result.reason
                                    )
                                    errors[str(category)[:80]] += 1
                        except Exception as exc:
                            counters["failed_count"] += 1
                            errors[f"candidate_failed:{type(exc).__name__}"] += 1
                outcome = (
                    "failed"
                    if errors
                    and (
                        counters["processed_count"] == 0
                        or (counters["stored_count"] == 0 and counters["failed_count"] > 0)
                    )
                    else "partial"
                    if errors or counters["failed_count"]
                    else "succeeded"
                )
                error_summary = (
                    "; ".join(f"{category}: {count}" for category, count in sorted(errors.items()))[
                        :500
                    ]
                    or None
                )
                await self.repository.finish_job_run(job_id, counters, outcome, error_summary)
                if outcome == "succeeded":
                    await self.repository.mark_reddit_success()
            except Exception as exc:
                counters["failed_count"] += 1
                await self.repository.finish_job_run(
                    job_id, counters, "failed", f"collection_failed:{type(exc).__name__}"
                )
                log.exception("Reddit collection failed")
            return counters
