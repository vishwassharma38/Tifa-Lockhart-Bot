"""Bounded hourly Reddit collection through the shared media pipeline."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from app.config import Settings
from app.reddit.connector import (
    RedditConnector,
    normalize_submission,
)
from app.reddit.models import RedditCandidate

log = logging.getLogger(__name__)


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
                    "blocked_reason": reason,
                }
            )
            errors: list[str] = []
            if reason:
                await self.repository.finish_job_run(job_id, counters, "skipped", reason)
                log.warning("Reddit collection blocked: %s", reason)
                return counters
            seen: set[str] = set()
            try:
                for phrase in self.settings.reddit_search_phrases:
                    remaining = self.settings.reddit_max_items_per_run - counters["examined_count"]
                    if remaining <= 0:
                        break
                    try:
                        submissions = await self.connector.search(phrase, limit=remaining)
                    except Exception as exc:
                        errors.append(f"query_failed:{type(exc).__name__}")
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
                            # yt-dlp performs its own segmented HTTP requests and
                            # does not use MediaFetcher's public-only resolver.
                            # Keep that legacy path available to manual /save,
                            # but do not allow it in unattended collection.
                            if any(asset.requires_video_download for asset in candidate.assets):
                                counters["skipped_count"] += len(candidate.assets)
                                continue
                            results = await self.pipeline.process_candidate(
                                candidate, publish=False
                            )
                            for result in results:
                                if result.status.value == "archived":
                                    counters["stored_count"] += 1
                                elif result.status.value == "skipped":
                                    counters["skipped_count"] += 1
                                elif result.status.value in {"failed", "recoverable"}:
                                    counters["failed_count"] += 1
                        except Exception as exc:
                            counters["failed_count"] += 1
                            errors.append(f"candidate_failed:{type(exc).__name__}")
                outcome = (
                    "failed"
                    if errors and counters["processed_count"] == 0
                    else "partial"
                    if errors or counters["failed_count"]
                    else "succeeded"
                )
                await self.repository.finish_job_run(
                    job_id, counters, outcome, "; ".join(errors)[:500] or None
                )
                if outcome == "succeeded":
                    await self.repository.mark_reddit_success()
            except Exception as exc:
                counters["failed_count"] += 1
                await self.repository.finish_job_run(
                    job_id, counters, "failed", f"collection_failed:{type(exc).__name__}"
                )
                log.exception("Reddit collection failed")
            return counters
