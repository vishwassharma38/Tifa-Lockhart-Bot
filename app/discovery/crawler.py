"""Bounded, SSRF-safe HTML crawling and manual public-URL saves."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import random
import time
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

from app.config import Settings
from app.discovery.challenges import detect_challenge
from app.discovery.classifier import DUMPSTER, VERIFIED, Classification, classify_page
from app.discovery.extractor import (
    ExtractedPage,
    extract_page,
    is_direct_media_url,
)
from app.discovery.http import (
    BLOCKED,
    CRAWLER_USER_AGENT,
    DOMAIN_BUDGET,
    DOMAIN_PAUSED,
    HTML_CONTENT_TYPES,
    HTTP_ERROR,
    INVALID_REDIRECT,
    NETWORK_ERROR,
    OK,
    OVERSIZE,
    RATE_LIMITED,
    ROBOTS_CONTENT_TYPES,
    SERVER_ERROR,
    TIMEOUT,
    TOO_MANY_REDIRECTS,
    UNSUPPORTED_CONTENT_TYPE,
    DomainThrottle,
    FetchResult,
    RequestBudget,
    SafeHttpClient,
    retry_delay_seconds,
)
from app.discovery.models import WebCandidate
from app.discovery.robots import RobotsCache, RobotsDecision, robots_fetch_max_bytes
from app.discovery.url_safety import (
    UrlSafetyError,
    normalize_url,
    validate_public_url,
    validate_url_literal,
)
from app.pipeline.phase2 import Phase2Result, Phase2Status
from app.reddit.models import MediaAsset

log = logging.getLogger(__name__)

HTML_ACCEPT = "text/html,application/xhtml+xml;q=0.9,*/*;q=0.1"
ROBOTS_ACCEPT = "text/plain,text/html;q=0.9,*/*;q=0.1"
_TRANSIENT_CATEGORIES = frozenset({SERVER_ERROR, NETWORK_ERROR, TIMEOUT})
_PERMANENT_HTTP_CATEGORIES = frozenset({HTTP_ERROR, INVALID_REDIRECT, TOO_MANY_REDIRECTS})
_CRAWLER_STATUSES = frozenset(
    {"queued", "processing", "verified", "dumpster", "rejected", "blocked", "failed", "oversize"}
)


@dataclass(frozen=True, slots=True)
class PageInspection:
    requested_url: str
    final_url: str | None
    status: int | None
    category: str
    reason: str | None = None
    extracted: ExtractedPage | None = None
    retry_after_seconds: float | None = None
    attempts: int = 0

    @property
    def ok(self) -> bool:
        return self.category == OK and self.extracted is not None


def _metadata(row: dict[str, Any]) -> dict[str, Any]:
    value = row.get("metadata_json")
    return dict(value) if isinstance(value, dict) else {}


def _now_iso(now: Callable[[], datetime]) -> str:
    return now().astimezone(UTC).isoformat()


def _retry_at(now: Callable[[], datetime], seconds: float) -> str:
    return (now().astimezone(UTC) + timedelta(seconds=max(0.0, seconds))).isoformat()


class PageInspector:
    """Fetch one page with robots, retries, challenge detection and extraction."""

    def __init__(
        self,
        settings: Settings,
        client: SafeHttpClient,
        *,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        jitter: Callable[[float, float], float] = random.uniform,
        now: Callable[[], datetime] | None = None,
        extractor: Callable[[str, str], ExtractedPage] = extract_page,
    ) -> None:
        self._settings = settings
        self._client = client
        self._sleep = sleep
        self._jitter = jitter
        self._now = now or (lambda: datetime.now(tz=UTC))
        self._extractor = extractor

    async def inspect(
        self,
        url: str,
        *,
        budget: RequestBudget,
        throttle: DomainThrottle,
        robots: RobotsCache,
    ) -> PageInspection:
        host = (urlsplit(url).hostname or "").lower()

        async def fetch_robots(robots_url: str) -> FetchResult:
            return await self._client.fetch(
                robots_url,
                budget=budget,
                throttle=throttle,
                accept=ROBOTS_ACCEPT,
                allowed_content_types=ROBOTS_CONTENT_TYPES,
                max_bytes=robots_fetch_max_bytes(self._settings),
                allow_missing_content_type=True,
            )

        decision = await robots.decision_for(url, fetch=fetch_robots)
        self._apply_robots_pause(budget, host, decision)
        if not decision.allow_all and decision.parser is not None:
            if not decision.can_fetch(CRAWLER_USER_AGENT, url):
                return PageInspection(
                    requested_url=url,
                    final_url=None,
                    status=None,
                    category="robots_disallowed",
                    reason="robots_disallowed",
                )
        elif not decision.allow_all:
            return PageInspection(
                requested_url=url,
                final_url=None,
                status=None,
                category="robots_disallowed",
                reason=decision.reason,
            )

        result = await self._fetch_with_retries(
            url,
            budget=budget,
            throttle=throttle,
            crawl_delay=decision.crawl_delay,
        )
        return self._interpret(result, url, budget, host)

    def _apply_robots_pause(
        self,
        budget: RequestBudget,
        host: str,
        decision: RobotsDecision,
    ) -> None:
        if not decision.pause_domain:
            return
        until = None
        if decision.retry_after_seconds is not None:
            until = self._now().astimezone(UTC) + timedelta(
                seconds=max(0.0, decision.retry_after_seconds)
            )
        budget.pause(host, decision.reason, until=until)

    async def _fetch_with_retries(
        self,
        url: str,
        *,
        budget: RequestBudget,
        throttle: DomainThrottle,
        crawl_delay: float | None,
    ) -> FetchResult:
        result = FetchResult(url, None, None, NETWORK_ERROR, error="not_attempted")
        for attempt in range(1, self._settings.crawl_max_attempts + 1):
            result = await self._client.fetch(
                url,
                budget=budget,
                throttle=throttle,
                accept=HTML_ACCEPT,
                allowed_content_types=HTML_CONTENT_TYPES,
                max_bytes=self._settings.max_page_bytes,
                crawl_delay=crawl_delay,
            )
            if result.category not in _TRANSIENT_CATEGORIES:
                return replace(result, attempts=attempt)
            if attempt >= self._settings.crawl_max_attempts:
                return replace(result, attempts=attempt)
            delay = retry_delay_seconds(attempt, jitter=self._jitter)
            log.info(
                "Crawl retry attempt=%s category=%s delay_seconds=%.2f",
                attempt,
                result.category,
                delay,
            )
            await self._sleep(delay)
        return replace(result, attempts=self._settings.crawl_max_attempts)

    def _interpret(
        self,
        result: FetchResult,
        url: str,
        budget: RequestBudget,
        host: str,
    ) -> PageInspection:
        if result.category == DOMAIN_PAUSED:
            return PageInspection(url, result.final_url, None, "domain_paused", result.error)
        if result.category == DOMAIN_BUDGET:
            return PageInspection(url, result.final_url, None, "domain_budget", result.error)
        if result.category == BLOCKED:
            reason = result.error or "blocked"
            if result.status in {401, 403} or reason.startswith(("http_401", "http_403")):
                budget.pause(host, reason)
            return PageInspection(
                url,
                result.final_url,
                result.status,
                "blocked",
                reason,
                attempts=result.attempts,
            )
        if result.category == RATE_LIMITED:
            delay = result.retry_after_seconds
            until = None
            if delay is not None:
                until = self._now().astimezone(UTC) + timedelta(seconds=max(0.0, delay))
            budget.pause(host, "http_429_rate_limited", until=until)
            return PageInspection(
                url,
                result.final_url,
                result.status,
                "rate_limited",
                "http_429_rate_limited",
                retry_after_seconds=delay,
                attempts=result.attempts,
            )
        if result.category in {SERVER_ERROR, NETWORK_ERROR, TIMEOUT}:
            return PageInspection(
                url,
                result.final_url,
                result.status,
                "failed",
                result.error or result.category,
                attempts=result.attempts,
            )
        if result.category in _PERMANENT_HTTP_CATEGORIES:
            return PageInspection(
                url,
                result.final_url,
                result.status,
                "failed",
                result.error or result.category,
                attempts=1,
            )
        if result.category == OVERSIZE:
            return PageInspection(
                url,
                result.final_url,
                result.status,
                "oversize",
                result.error or "oversize",
                attempts=1,
            )
        if result.category == UNSUPPORTED_CONTENT_TYPE:
            return PageInspection(
                url,
                result.final_url,
                result.status,
                "rejected",
                result.error or "unsupported_content_type",
                attempts=1,
            )
        if result.category != OK:
            return PageInspection(
                url,
                result.final_url,
                result.status,
                "failed",
                result.error or result.category,
                attempts=1,
            )

        html = result.body.decode("utf-8", errors="replace")
        if not html.strip():
            return PageInspection(
                url,
                result.final_url,
                result.status,
                "failed",
                "empty_response",
                attempts=result.attempts,
            )
        challenge = detect_challenge(html)
        if challenge:
            budget.pause(host, challenge)
            return PageInspection(
                url,
                result.final_url,
                result.status,
                "challenge",
                challenge,
                attempts=result.attempts,
            )
        final_url = result.final_url or url
        try:
            extracted = self._extractor(html, final_url)
        except Exception as exc:
            log.warning("Page extraction failed (%s)", type(exc).__name__)
            return PageInspection(
                url,
                result.final_url,
                result.status,
                "extraction_failed",
                f"extraction_failed:{type(exc).__name__}",
                attempts=result.attempts,
            )
        return PageInspection(
            url,
            final_url,
            result.status,
            OK,
            None,
            extracted,
            attempts=result.attempts,
        )


class _UrlAdmission:
    def __init__(self, limit: int) -> None:
        self._limit = max(0, limit)
        self.admitted = 0

    @property
    def remaining(self) -> int:
        return max(0, self._limit - self.admitted)

    def admit(self) -> bool:
        if self.admitted >= self._limit:
            return False
        self.admitted += 1
        return True


class DiscoveryCrawler:
    """Process queued discoveries in bounded batches through the shared pipeline."""

    def __init__(
        self,
        settings: Settings,
        repository: Any,
        pipeline: Any,
        *,
        client: SafeHttpClient | None = None,
        inspector: PageInspector | None = None,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        jitter: Callable[[float, float], float] = random.uniform,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._settings = settings
        self._repository = repository
        self._pipeline = pipeline
        self._client = client or SafeHttpClient(
            max_concurrent_requests=settings.max_concurrent_requests,
            max_redirects=settings.max_redirects,
            connect_timeout=settings.crawl_connect_timeout_seconds,
            read_timeout=settings.crawl_read_timeout_seconds,
        )
        self._owns_client = client is None
        self._now = now or (lambda: datetime.now(tz=UTC))
        self._inspector = inspector or PageInspector(
            settings,
            self._client,
            sleep=sleep,
            jitter=jitter,
            now=self._now,
        )
        self._sleep = sleep
        self._jitter = jitter
        self._clock = clock
        self._lock = asyncio.Lock()

    async def close(self) -> None:
        if self._owns_client:
            await self._client.close()

    async def run_once(self, *, scheduled: bool = True) -> dict[str, int]:
        counters = {
            key: 0
            for key in (
                "discovered_count",
                "examined_count",
                "duplicate_count",
                "processed_count",
                "stored_count",
                "skipped_count",
                "failed_count",
            )
        }
        if self._lock.locked():
            return counters
        async with self._lock:
            if scheduled and not await self._automation_enabled():
                return counters
            job_id = await self._repository.create_job_run(
                {
                    "batch_size": self._settings.crawl_batch_size,
                    "max_urls": self._settings.max_urls_per_job,
                    "max_pages_per_domain": self._settings.max_pages_per_domain,
                    "max_depth": self._settings.max_crawl_depth,
                },
                job_type="crawl",
            )
            budget = RequestBudget(max_domain_requests=self._settings.max_pages_per_domain)
            throttle = DomainThrottle(
                self._settings.per_domain_delay_seconds,
                sleep=self._sleep,
                clock=self._clock,
            )
            robots = RobotsCache(user_agent=CRAWLER_USER_AGENT)
            admission = _UrlAdmission(self._settings.max_urls_per_job)
            seen_edges: set[tuple[str, str]] = set()
            errors: Counter[str] = Counter()
            paused_for_collection = False
            try:
                claimed = await self._repository.claim_discoveries(
                    limit=self._settings.crawl_batch_size,
                    max_depth=self._settings.max_crawl_depth,
                )
                for row in claimed:
                    if scheduled and not paused_for_collection:
                        if not await self._automation_enabled():
                            paused_for_collection = True
                    if paused_for_collection:
                        await self._safe_requeue(row, "collection_paused")
                        counters["skipped_count"] += 1
                        continue
                    try:
                        outcome = await self._process_discovery(
                            row,
                            budget=budget,
                            throttle=throttle,
                            robots=robots,
                            admission=admission,
                            seen_edges=seen_edges,
                            scheduled=scheduled,
                        )
                    except asyncio.CancelledError:
                        await self._safe_requeue(row, "interrupted_by_shutdown")
                        raise
                    except Exception as exc:
                        counters["failed_count"] += 1
                        errors[f"discovery_processing_failed:{type(exc).__name__}"] += 1
                        log.warning(
                            "Discovery processing failed discovery_id=%s (%s)",
                            row.get("id"),
                            type(exc).__name__,
                        )
                        await self._safe_fail(row, f"processing_failed:{type(exc).__name__}")
                        continue
                    counters["discovered_count"] += outcome.get("discovered", 0)
                    counters["examined_count"] += outcome.get("examined", 0)
                    counters["duplicate_count"] += outcome.get("duplicate", 0)
                    counters["processed_count"] += outcome.get("processed", 0)
                    counters["stored_count"] += outcome.get("stored", 0)
                    counters["skipped_count"] += outcome.get("skipped", 0)
                    counters["failed_count"] += outcome.get("failed", 0)
                    for category, count in outcome.get("errors", {}).items():
                        errors[category] += count

                outcome = (
                    "failed"
                    if counters["failed_count"] and counters["processed_count"] == 0
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
                await self._repository.finish_job_run(job_id, counters, outcome, error_summary)
            except Exception as exc:
                counters["failed_count"] += 1
                await self._repository.finish_job_run(
                    job_id,
                    counters,
                    "failed",
                    f"crawl_job_failed:{type(exc).__name__}",
                )
                log.exception("Crawl job failed")
            return counters

    async def _automation_enabled(self) -> bool:
        state = await self._repository.get_runtime_state()
        return bool(state.get("collection_enabled"))

    async def _process_discovery(
        self,
        row: dict[str, Any],
        *,
        budget: RequestBudget,
        throttle: DomainThrottle,
        robots: RobotsCache,
        admission: _UrlAdmission,
        seen_edges: set[tuple[str, str]],
        scheduled: bool,
    ) -> dict[str, Any]:
        discovery_id = str(row.get("id") or "")
        url = str(row.get("normalized_url") or row.get("url") or "")
        depth = int(row.get("depth") or 0)
        if not discovery_id or not url:
            return {"failed": 1, "errors": {"invalid_discovery_record": 1}}
        if depth > self._settings.max_crawl_depth:
            await self._repository.update_discovery(
                discovery_id,
                {
                    "status": "failed",
                    "last_error": "depth_exceeds_limit",
                    "next_attempt_at": None,
                },
            )
            return {"failed": 1, "errors": {"depth_exceeds_limit": 1}}

        host = (urlsplit(url).hostname or "").lower()
        pause_reason = budget.pause_reason(host)
        if pause_reason:
            await self._requeue_from_pause(discovery_id, budget, host, pause_reason)
            return {"skipped": 1}

        inspection = await self._inspector.inspect(
            url, budget=budget, throttle=throttle, robots=robots
        )
        if inspection.category in {DOMAIN_PAUSED, DOMAIN_BUDGET}:
            await self._requeue_from_pause(
                discovery_id,
                budget,
                host,
                inspection.reason or inspection.category,
            )
            return {"skipped": 1}

        metadata = _metadata(row)
        metadata["fetch_category"] = inspection.category
        if inspection.final_url:
            metadata["final_url"] = inspection.final_url
        if inspection.reason:
            metadata["fetch_reason"] = inspection.reason
        fetched_at = _now_iso(self._now)

        if inspection.category == "robots_disallowed":
            await self._repository.update_discovery(
                discovery_id,
                {
                    "status": "blocked",
                    "last_error": inspection.reason or "robots_disallowed",
                    "metadata_json": metadata,
                    "next_attempt_at": None,
                },
            )
            return {"failed": 1, "errors": {"robots_disallowed": 1}}

        if inspection.category in {"blocked", "challenge"}:
            metadata["fetched_at"] = fetched_at
            await self._repository.update_discovery(
                discovery_id,
                {
                    "status": "blocked",
                    "http_status": inspection.status,
                    "last_error": inspection.reason or inspection.category,
                    "metadata_json": metadata,
                    "fetched_at": fetched_at,
                    "next_attempt_at": None,
                },
            )
            return {"failed": 1, "errors": {inspection.category: 1}}

        if inspection.category == "rate_limited":
            delay = inspection.retry_after_seconds
            if delay is None:
                delay = 300.0
            next_attempt = _retry_at(self._now, delay)
            if not budget.pause_reason(host):
                budget.pause(host, "http_429_rate_limited")
            metadata["retry_not_before"] = next_attempt
            await self._repository.update_discovery(
                discovery_id,
                {
                    "status": "queued",
                    "http_status": inspection.status,
                    "retry_count": int(row.get("retry_count") or 0) + 1,
                    "last_error": "http_429_rate_limited",
                    "metadata_json": metadata,
                    "fetched_at": fetched_at,
                    "next_attempt_at": next_attempt,
                },
            )
            return {"failed": 1, "errors": {"rate_limited": 1}}

        if inspection.category == "oversize":
            metadata["fetched_at"] = fetched_at
            await self._repository.update_discovery(
                discovery_id,
                {
                    "status": "oversize",
                    "http_status": inspection.status,
                    "last_error": inspection.reason or "oversize",
                    "metadata_json": metadata,
                    "fetched_at": fetched_at,
                    "next_attempt_at": None,
                },
            )
            return {"failed": 1, "errors": {"oversize": 1}}

        if inspection.category == "rejected":
            metadata["fetched_at"] = fetched_at
            await self._repository.update_discovery(
                discovery_id,
                {
                    "status": "rejected",
                    "http_status": inspection.status,
                    "last_error": inspection.reason or "unsupported_content_type",
                    "metadata_json": metadata,
                    "fetched_at": fetched_at,
                    "next_attempt_at": None,
                },
            )
            return {"skipped": 1}

        if inspection.category == "extraction_failed":
            metadata["fetched_at"] = fetched_at
            metadata["review_reason"] = inspection.reason or "extraction_failed"
            await self._repository.update_discovery(
                discovery_id,
                {
                    "status": "dumpster",
                    "http_status": inspection.status,
                    "last_error": inspection.reason or "extraction_failed",
                    "metadata_json": metadata,
                    "fetched_at": fetched_at,
                    "next_attempt_at": None,
                },
            )
            return {"processed": 1, "skipped": 1}

        if not inspection.ok:
            metadata["fetched_at"] = fetched_at
            await self._repository.update_discovery(
                discovery_id,
                {
                    "status": "failed",
                    "http_status": inspection.status,
                    "retry_count": int(row.get("retry_count") or 0) + max(1, inspection.attempts),
                    "last_error": inspection.reason or inspection.category,
                    "metadata_json": metadata,
                    "fetched_at": fetched_at,
                    "next_attempt_at": None,
                },
            )
            return {"failed": 1, "errors": {inspection.category: 1}}

        page = inspection.extracted
        if page is None:
            return {"failed": 1, "errors": {"missing_extraction": 1}}
        metadata["fetched_at"] = fetched_at
        metadata["page"] = page.to_metadata()
        classification = classify_page(
            title=page.classification_title,
            description=page.classification_description,
            text=page.text,
            tags=page.tags,
            author=page.author,
            search_title=metadata.get("search_result_title"),
            search_snippet=metadata.get("search_result_snippet"),
            search_phrase=metadata.get("search_phrase"),
            source_type=row.get("source_type"),
        )
        metadata["classification"] = classification.to_metadata()
        status = classification.status

        outcome: dict[str, Any] = {"examined": 1, "processed": 1}
        if status == VERIFIED and not page.original_media:
            review_reason = (
                "preview_only_original_unavailable" if page.media else "original_media_not_found"
            )
            status = DUMPSTER
            metadata["review_reason"] = review_reason
            classification = Classification(
                DUMPSTER,
                classification.score,
                classification.reasons + (review_reason,),
                classification.evidence,
            )
            metadata["classification"] = classification.to_metadata()
            outcome["skipped"] = 1

        update: dict[str, Any] = {
            "status": status,
            "http_status": inspection.status,
            "last_error": None,
            "metadata_json": metadata,
            "fetched_at": fetched_at,
            "next_attempt_at": None,
        }
        if status == VERIFIED:
            update["verified_at"] = fetched_at
        await self._repository.update_discovery(discovery_id, update)

        if status == VERIFIED:
            acquisition = await self._acquire_original_media(row, page, metadata)
            metadata["acquisition"] = acquisition["summary"]
            await self._repository.update_discovery_metadata(discovery_id, metadata)
            outcome["stored"] = acquisition["stored"]
            outcome["duplicate"] = acquisition["duplicate"]
            outcome["failed"] = acquisition["failed"]
            outcome["skipped"] = outcome.get("skipped", 0) + acquisition["skipped"]
            for category, count in acquisition["errors"].items():
                outcome.setdefault("errors", {})[category] = count

        if classification.allows_child_crawl:
            admitted = await self._admit_children(
                row,
                page,
                depth=depth,
                admission=admission,
                seen_edges=seen_edges,
                scheduled=scheduled,
            )
            outcome["discovered"] = admitted
        return outcome

    async def _acquire_original_media(
        self,
        row: dict[str, Any],
        page: ExtractedPage,
        metadata: dict[str, Any],
    ) -> dict[str, Any]:
        original_media = page.original_media
        assets = tuple(
            MediaAsset(
                url=media.url,
                source_media_id=f"web-{hashlib.sha256(media.url.encode('utf-8')).hexdigest()[:16]}",
                source_metadata={
                    "extraction": media.source,
                    "original": media.original,
                    "width": media.width,
                },
            )
            for media in original_media
        )
        candidate = WebCandidate(
            page_url=page.final_url,
            title=page.classification_title or row.get("title"),
            body=page.classification_description or row.get("description"),
            author_name=page.author or row.get("creator_name"),
            author_profile_url=row.get("creator_profile_url"),
            media_assets=assets,
            media_metadata={
                "page": page.to_metadata(),
                "classification": metadata.get("classification"),
                "search_phrase": metadata.get("search_phrase"),
            },
            extraction_reason="web_original_media",
            discovery_id=str(row.get("id")),
            source_type=str(row.get("source_type") or "brave_html"),
            source_platform="web",
            normalized_url=page.final_url,
            crawl_depth=int(row.get("depth") or 0),
        )
        summary: dict[str, Any] = {"asset_count": len(assets)}
        result = {
            "stored": 0,
            "duplicate": 0,
            "failed": 0,
            "skipped": 0,
            "errors": {},
            "summary": summary,
        }
        try:
            results = await self._pipeline.process_candidate(candidate, publish=False)
        except Exception as exc:
            result["failed"] = 1
            result["errors"] = {"acquisition_failed": 1}
            summary["error"] = f"acquisition_failed:{type(exc).__name__}"
            log.warning("Web acquisition failed (%s)", type(exc).__name__)
            return result
        summary["results"] = [item.to_dict() for item in results]
        for item in results:
            outcome = item.status.value
            if outcome == "archived":
                result["stored"] += 1
            elif outcome == "duplicate":
                result["duplicate"] += 1
            elif outcome in {"failed", "recoverable"}:
                result["failed"] += 1
                reason = str(item.reason)[:80]
                result["errors"][reason] = result["errors"].get(reason, 0) + 1
            elif outcome == "skipped":
                result["skipped"] += 1
        return result

    async def _admit_children(
        self,
        row: dict[str, Any],
        page: ExtractedPage,
        *,
        depth: int,
        admission: _UrlAdmission,
        seen_edges: set[tuple[str, str]],
        scheduled: bool,
    ) -> int:
        if depth >= self._settings.max_crawl_depth:
            return 0
        parent_id = str(row.get("id") or "")
        parent_url = page.final_url
        source_type = str(row.get("source_type") or "brave_html")
        metadata = _metadata(row)
        admitted = 0
        for index, link in enumerate(page.links):
            if admission.remaining <= 0:
                break
            child_depth = depth + 1
            if child_depth > self._settings.max_crawl_depth:
                break
            try:
                normalized = normalize_url(link.url).normalized
            except UrlSafetyError:
                continue
            if normalized in {parent_url, row.get("normalized_url")}:
                continue
            if is_direct_media_url(normalized):
                continue
            edge_key = (parent_id, normalized)
            if edge_key in seen_edges:
                continue
            seen_edges.add(edge_key)
            if scheduled and index % 10 == 0 and not await self._automation_enabled():
                break
            child_metadata: dict[str, Any] = {
                "discovered_via": "crawl_link",
                "parent_url": parent_url,
                "parent_discovery_id": parent_id,
                "origin_source_type": source_type,
                "link_text": link.text[:300],
                "discovered_at": _now_iso(self._now),
            }
            search_phrase = metadata.get("search_phrase")
            if search_phrase:
                child_metadata["search_phrase"] = search_phrase
            payload = {
                "url": link.original_url or normalized,
                "normalized_url": normalized,
                "source_type": source_type,
                "title": None,
                "description": link.text[:500] or None,
                "domain": urlsplit(normalized).hostname,
                "parent_discovery_id": parent_id,
                "depth": child_depth,
                "status": "queued",
                "metadata_json": child_metadata,
            }
            try:
                child_row, created = await self._repository.upsert_discovery_record(payload)
            except Exception as exc:
                log.warning("Child discovery insert failed (%s)", type(exc).__name__)
                continue
            if created and not admission.admit():
                break
            if created:
                admitted += 1
            child_id = child_row.get("id")
            try:
                await self._repository.record_discovery_edge(
                    parent_discovery_id=parent_id,
                    child_discovery_id=str(child_id) if child_id else None,
                    child_normalized_url=normalized,
                    discovered_from_url=parent_url,
                    depth=child_depth,
                )
            except Exception as exc:
                log.warning("Discovery edge insert failed (%s)", type(exc).__name__)
        return admitted

    async def _requeue_from_pause(
        self,
        discovery_id: str,
        budget: RequestBudget,
        host: str,
        reason: str,
    ) -> None:
        until = budget.pause_until(host)
        await self._repository.requeue_discovery(
            discovery_id,
            reason=reason,
            next_attempt_at=until.astimezone(UTC).isoformat() if until else None,
        )

    async def _safe_requeue(self, row: dict[str, Any], reason: str) -> None:
        discovery_id = row.get("id")
        if not discovery_id:
            return
        try:
            await self._repository.requeue_discovery(str(discovery_id), reason=reason)
        except Exception as exc:
            log.warning("Discovery requeue failed (%s)", type(exc).__name__)

    async def _safe_fail(self, row: dict[str, Any], reason: str) -> None:
        discovery_id = row.get("id")
        if not discovery_id:
            return
        try:
            await self._repository.update_discovery(
                str(discovery_id),
                {"status": "failed", "last_error": reason, "next_attempt_at": None},
            )
        except Exception as exc:
            log.warning("Discovery failure update failed (%s)", type(exc).__name__)


class WebSaveService:
    """Apply crawler safety rules to explicit public ``/save`` URLs."""

    def __init__(
        self,
        settings: Settings,
        repository: Any,
        pipeline: Any,
        *,
        client: SafeHttpClient | None = None,
        inspector: PageInspector | None = None,
        now: Callable[[], datetime] | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        validator: Callable[..., Awaitable[Any]] = validate_public_url,
    ) -> None:
        self._settings = settings
        self._repository = repository
        self._pipeline = pipeline
        self._client = client or SafeHttpClient(
            max_concurrent_requests=settings.max_concurrent_requests,
            max_redirects=settings.max_redirects,
            connect_timeout=settings.crawl_connect_timeout_seconds,
            read_timeout=settings.crawl_read_timeout_seconds,
        )
        self._owns_client = client is None
        self._now = now or (lambda: datetime.now(tz=UTC))
        self._inspector = inspector or PageInspector(
            settings, self._client, now=self._now, sleep=sleep
        )
        self._clock = clock
        self._sleep = sleep
        self._validator = validator

    async def close(self) -> None:
        if self._owns_client:
            await self._client.close()

    async def save(self, url: str) -> Phase2Result:
        try:
            normalized = validate_url_literal(url.strip())
        except UrlSafetyError as exc:
            return Phase2Result(Phase2Status.FAILED, f"invalid_url:{exc.reason}")
        except Exception:
            return Phase2Result(Phase2Status.FAILED, "invalid_url")

        if is_direct_media_url(normalized.normalized):
            try:
                await self._validator(normalized.normalized)
            except UrlSafetyError as exc:
                return Phase2Result(Phase2Status.FAILED, f"invalid_url:{exc.reason}")
            except Exception:
                return Phase2Result(Phase2Status.FAILED, "url_validation_failed")
            candidate = WebCandidate(
                page_url=normalized.normalized,
                title=normalized.hostname,
                media_assets=(
                    MediaAsset(
                        url=normalized.normalized,
                        source_media_id=f"web-{hashlib.sha256(normalized.normalized.encode('utf-8')).hexdigest()[:16]}",
                    ),
                ),
                media_metadata={"discovered_via": "manual_save", "direct_media": True},
                extraction_reason="manual_direct_media",
                source_type="manual_save",
                source_platform="web",
                normalized_url=normalized.normalized,
            )
            discovery = await self._record_manual_discovery(candidate, status="verified")
            if discovery is None:
                return Phase2Result(Phase2Status.FAILED, "discovery_write_failed")
            candidate = WebCandidate(
                page_url=candidate.page_url,
                title=candidate.title,
                media_assets=candidate.media_assets,
                media_metadata=candidate.media_metadata,
                extraction_reason=candidate.extraction_reason,
                discovery_id=str(discovery.get("id")) if discovery.get("id") else None,
                source_type="manual_save",
                source_platform="web",
                normalized_url=candidate.normalized_source_url,
            )
            return await self._process_and_publish(candidate)

        budget = RequestBudget(max_domain_requests=self._settings.max_pages_per_domain)
        throttle = DomainThrottle(
            self._settings.per_domain_delay_seconds,
            sleep=self._sleep,
            clock=self._clock,
        )
        robots = RobotsCache(user_agent=CRAWLER_USER_AGENT)
        inspection = await self._inspector.inspect(
            normalized.normalized, budget=budget, throttle=throttle, robots=robots
        )
        if not inspection.ok:
            if inspection.category == OK:
                return Phase2Result(Phase2Status.SKIPPED, "page_extraction_failed")
            return Phase2Result(
                Phase2Status.SKIPPED,
                inspection.reason or inspection.category,
            )
        page = inspection.extracted
        if page is None:
            return Phase2Result(Phase2Status.SKIPPED, "page_extraction_failed")
        if not page.original_media:
            reason = (
                "preview_only_original_unavailable" if page.media else "original_media_not_found"
            )
            candidate = self._candidate_from_page(page, discovery_id=None)
            await self._record_manual_discovery(candidate, status="dumpster")
            return Phase2Result(Phase2Status.SKIPPED, reason)

        candidate = self._candidate_from_page(page, discovery_id=None)
        discovery = await self._record_manual_discovery(candidate, status="verified")
        if discovery is None:
            return Phase2Result(Phase2Status.FAILED, "discovery_write_failed")
        candidate = self._candidate_from_page(
            page,
            discovery_id=str(discovery.get("id")) if discovery.get("id") else None,
        )
        return await self._process_and_publish(candidate)

    def _candidate_from_page(
        self, page: ExtractedPage, *, discovery_id: str | None
    ) -> WebCandidate:
        assets = tuple(
            MediaAsset(
                url=media.url,
                source_media_id=f"web-{hashlib.sha256(media.url.encode('utf-8')).hexdigest()[:16]}",
                source_metadata={"extraction": media.source, "original": media.original},
            )
            for media in page.original_media
        )
        return WebCandidate(
            page_url=page.final_url,
            title=page.classification_title or page.final_url,
            body=page.classification_description,
            author_name=page.author,
            media_assets=assets,
            media_metadata={"page": page.to_metadata(), "discovered_via": "manual_save"},
            extraction_reason="manual_web_original_media",
            discovery_id=discovery_id,
            source_type="manual_save",
            source_platform="web",
            normalized_url=page.final_url,
        )

    async def _record_manual_discovery(
        self, candidate: WebCandidate, *, status: str
    ) -> dict[str, Any] | None:
        now = _now_iso(self._now)
        metadata = {
            "discovered_via": "manual_save",
            "manual_requested_at": now,
            **dict(candidate.media_metadata),
        }
        payload = {
            "url": candidate.source_url,
            "normalized_url": candidate.normalized_source_url,
            "source_type": "manual_save",
            "source_platform_id": candidate.submission_id,
            "title": candidate.title or None,
            "description": candidate.body,
            "creator_name": candidate.author_name,
            "domain": urlsplit(candidate.source_url).hostname,
            "depth": 0,
            "status": status,
            "verified_at": now if status == "verified" else None,
            "metadata_json": metadata,
        }
        try:
            row, _created = await self._repository.upsert_discovery_record(payload)
        except Exception as exc:
            log.warning("Manual discovery write failed (%s)", type(exc).__name__)
            return None
        if row.get("id") is None:
            return None
        if str(row.get("status")) != status:
            try:
                await self._repository.update_discovery(
                    str(row["id"]),
                    {
                        "status": status,
                        "verified_at": now if status == "verified" else None,
                        "metadata_json": metadata,
                    },
                )
            except Exception as exc:
                log.warning("Manual discovery update failed (%s)", type(exc).__name__)
        return row

    async def _process_and_publish(self, candidate: WebCandidate) -> Phase2Result:
        try:
            results = await self._pipeline.process_candidate(candidate, publish=True)
        except Exception as exc:
            log.warning("Manual web save failed (%s)", type(exc).__name__)
            return Phase2Result(Phase2Status.FAILED, "manual_save_failed")
        if not results:
            return Phase2Result(Phase2Status.SKIPPED, "no_supported_media")
        if len(results) == 1:
            return results[0]
        statuses = {item.status for item in results}
        if Phase2Status.RECOVERABLE in statuses:
            status, reason = Phase2Status.RECOVERABLE, "partial_failure_recoverable"
        elif Phase2Status.FAILED in statuses:
            status, reason = Phase2Status.FAILED, "partial_failure"
        elif Phase2Status.PUBLISHED in statuses:
            status, reason = Phase2Status.PUBLISHED, "published_multiple_assets"
        elif Phase2Status.DUPLICATE in statuses:
            status, reason = Phase2Status.DUPLICATE, "duplicates_recorded"
        else:
            status, reason = Phase2Status.SKIPPED, "no_supported_media"
        return Phase2Result(
            status,
            reason,
            submission_id=candidate.submission_id,
            details={"asset_results": [item.to_dict() for item in results]},
        )
