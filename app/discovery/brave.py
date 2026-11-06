"""Isolated Brave Search HTML parser and gated discovery service."""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections import Counter
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from bs4 import BeautifulSoup

from app.config import Settings
from app.discovery.challenges import detect_challenge
from app.discovery.http import (
    HTML_CONTENT_TYPES,
    OK,
    RATE_LIMITED,
    DomainThrottle,
    FetchResult,
    RequestBudget,
    SafeHttpClient,
)
from app.discovery.url_safety import UrlSafetyError, normalize_url

log = logging.getLogger(__name__)

_BRAVE_INTERNAL_HOSTS = frozenset({"search.brave.com", "brave.com", "www.brave.com"})
_BRAVE_INTERNAL_PREFIXES = (
    "/search",
    "/settings",
    "/preferences",
    "/account",
    "/help",
    "/about",
)
_NO_RESULTS_MARKERS = (
    "no results found",
    "no results",
    "we couldn't find any results",
    "did not match any documents",
)
_ACCESS_DENIAL_MARKERS = (
    "access denied",
    "request blocked",
    "unusual traffic",
    "automated queries",
    "too many requests",
    "not available in your region",
    "you have been blocked",
    "forbidden",
)


class BraveParseError(ValueError):
    """Raised when Brave result markup cannot be identified safely."""


class BraveBarrierError(RuntimeError):
    """Raised for a challenge or explicit access restriction."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class BraveRequestError(RuntimeError):
    """Raised for an ordinary transport or HTTP failure."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class BraveAccessNotApprovedError(RuntimeError):
    """Raised when either explicit Brave access gate is closed."""


@dataclass(frozen=True, slots=True)
class BraveResult:
    title: str
    snippet: str
    url: str
    original_url: str
    search_phrase: str
    result_page_url: str

    def to_metadata(self) -> dict[str, str]:
        return {
            "search_phrase": self.search_phrase,
            "search_result_title": self.title,
            "search_result_snippet": self.snippet,
            "search_result_page_url": self.result_page_url,
            "search_result_url": self.original_url,
            "discovered_via": "brave_html",
        }


@dataclass(frozen=True, slots=True)
class BraveSearchPage:
    phrase: str
    page_url: str
    results: tuple[BraveResult, ...] = field(default_factory=tuple)


def _clean(value: str | None, limit: int = 2_000) -> str:
    if not value:
        return ""
    return " ".join(value.split())[:limit]


def _is_ad_container(container: Any) -> bool:
    data_type = str(container.get("data-type") or "").lower()
    if data_type in {"ad", "ads", "advertisement"}:
        return True
    classes = container.get("class") or []
    if isinstance(classes, str):
        tokens = {part.lower() for part in classes.split()}
    else:
        tokens = {str(part).lower() for part in classes}
    return bool(tokens & {"ad", "ads", "advertisement", "sponsored"})


def _is_navigation_url(url: str) -> bool:
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    if host in _BRAVE_INTERNAL_HOSTS or host.endswith(".brave.com"):
        return True
    if not host:
        return True
    return False


def _first_text(container: Any, selectors: tuple[str, ...]) -> str:
    for selector in selectors:
        match = container.select_one(selector)
        if match is not None:
            value = _clean(match.get_text(" ", strip=True))
            if value:
                return value
    return ""


class BraveSearchParser:
    """Parse one Brave HTML result page without knowledge of the database."""

    _CONTAINER_SELECTORS = (
        "div.snippet",
        "article.result",
        "div.result",
        "div[data-type='web']",
        "main#results div.web-result",
    )
    _TITLE_SELECTORS = (
        ".title",
        ".snippet-title",
        "h2",
        "h3",
        ".result-header",
    )
    _SNIPPET_SELECTORS = (
        ".snippet-description",
        ".snippet-content",
        ".description",
        ".snippet-description-text",
        "p",
    )

    def __init__(
        self,
        *,
        soup_factory: Callable[[str], BeautifulSoup] | None = None,
    ) -> None:
        self._soup_factory = soup_factory or (lambda html: BeautifulSoup(html, "lxml"))

    def parse(
        self,
        html: str,
        *,
        search_phrase: str,
        result_page_url: str,
    ) -> tuple[BraveResult, ...]:
        if not html or not html.strip():
            return ()
        try:
            soup = self._soup_factory(html)
        except Exception as exc:
            raise BraveParseError("Brave HTML could not be parsed") from exc

        containers: list[Any] = []
        for selector in self._CONTAINER_SELECTORS:
            for container in soup.select(selector):
                if container not in containers and not _is_ad_container(container):
                    containers.append(container)

        visible_text = _clean(soup.get_text(" ", strip=True), 20_000).lower()
        if not containers:
            if any(marker in visible_text for marker in _NO_RESULTS_MARKERS):
                return ()
            if soup.select_one("main#results, #results"):
                return ()
            raise BraveParseError("Brave result markup was not recognized")

        results: list[BraveResult] = []
        seen: set[str] = set()
        for container in containers:
            title = _first_text(container, self._TITLE_SELECTORS)
            snippet = _first_text(container, self._SNIPPET_SELECTORS)
            destination: str | None = None
            original: str | None = None
            for anchor in container.select("a[href]"):
                href = anchor.get("href")
                if not isinstance(href, str) or not href.strip():
                    continue
                candidate = href.strip()
                if candidate.startswith(("#", "javascript:", "mailto:", "data:")):
                    continue
                try:
                    normalized = normalize_url(candidate).normalized
                except UrlSafetyError:
                    continue
                if _is_navigation_url(normalized):
                    continue
                destination = normalized
                original = candidate
                if not title:
                    title = _clean(anchor.get_text(" ", strip=True))
                break
            if destination is None or original is None:
                continue
            if destination in seen:
                continue
            seen.add(destination)
            if not title:
                title = destination
            if snippet == title:
                snippet = ""
            results.append(
                BraveResult(
                    title=title,
                    snippet=snippet,
                    url=destination,
                    original_url=original,
                    search_phrase=search_phrase,
                    result_page_url=result_page_url,
                )
            )
            if len(results) >= 50:
                break
        return tuple(results)


def detect_brave_barrier(status: int | None, html: str | None) -> str | None:
    """Return a stable restriction reason for a Brave response, if any."""
    if status == 401:
        return "http_401_access_denied"
    if status == 403:
        return "http_403_access_denied"
    if status == 429:
        return "http_429_rate_limited"
    if not html:
        return None
    challenge = detect_challenge(html)
    if challenge:
        return challenge
    lowered = " ".join(html[:50_000].lower().split())
    matches = sum(1 for marker in _ACCESS_DENIAL_MARKERS if marker in lowered)
    if matches >= 1 and ("brave" in lowered or "<title>" in lowered):
        return "explicit_access_denial_page"
    if matches >= 2:
        return "explicit_access_denial_page"
    return None


class BraveSearchAdapter:
    """Perform gated HTML searches and return parser-independent results."""

    def __init__(
        self,
        settings: Settings,
        *,
        client: SafeHttpClient | None = None,
        parser: BraveSearchParser | None = None,
        request_override: Callable[[str], Awaitable[FetchResult]] | None = None,
    ) -> None:
        self._settings = settings
        self._client = client
        self._owns_client = client is None
        self._parser = parser or BraveSearchParser()
        self._request_override = request_override

    @property
    def access_permitted(self) -> bool:
        return bool(self._settings.brave_html_enabled and self._settings.brave_html_access_approved)

    def ensure_access(self) -> None:
        if not self._settings.brave_html_enabled:
            raise BraveAccessNotApprovedError("BRAVE_HTML_ENABLED is false")
        if not self._settings.brave_html_access_approved:
            raise BraveAccessNotApprovedError("BRAVE_HTML_ACCESS_APPROVED has not been confirmed")

    def search_url(self, phrase: str, *, page: int = 0) -> str:
        parsed = urlsplit(self._settings.brave_search_base_url)
        query = [
            (key, value)
            for key, value in parse_qsl(parsed.query, keep_blank_values=True)
            if key.lower() not in {"q", "offset"}
        ]
        query.append(("q", phrase))
        if page > 0:
            query.append(("offset", str(page * 10)))
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path or "/", urlencode(query), ""))

    async def fetch_page(
        self,
        phrase: str,
        *,
        page: int,
        budget: RequestBudget,
        throttle: DomainThrottle,
    ) -> BraveSearchPage:
        self.ensure_access()
        url = self.search_url(phrase, page=page)
        if self._request_override is not None:
            result = await self._request_override(url)
        else:
            if self._client is None:
                self._client = SafeHttpClient(
                    max_concurrent_requests=self._settings.max_concurrent_requests,
                    max_redirects=self._settings.max_redirects,
                    connect_timeout=self._settings.crawl_connect_timeout_seconds,
                    read_timeout=self._settings.crawl_read_timeout_seconds,
                )
                self._owns_client = True
            result = await self._client.fetch(
                url,
                budget=budget,
                throttle=throttle,
                accept="text/html,application/xhtml+xml;q=0.9",
                allowed_content_types=HTML_CONTENT_TYPES,
                max_bytes=self._settings.max_page_bytes,
            )

        if result.category == RATE_LIMITED or result.status == 429:
            raise BraveBarrierError("http_429_rate_limited")
        html = result.body.decode("utf-8", errors="replace") if result.body else None
        barrier = detect_brave_barrier(result.status, html)
        if barrier:
            raise BraveBarrierError(barrier)
        if result.category != OK or result.status is None:
            raise BraveRequestError(result.error or result.category)
        try:
            results = self._parser.parse(
                html or "",
                search_phrase=phrase,
                result_page_url=result.final_url or url,
            )
        except BraveParseError:
            raise
        except Exception as exc:
            raise BraveParseError("Brave parser failed unexpectedly") from exc
        return BraveSearchPage(
            phrase=phrase,
            page_url=result.final_url or url,
            results=results,
        )

    async def close(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.close()
            self._client = None


def _stable_result_id(normalized_url: str) -> str:
    return hashlib.sha256(normalized_url.encode("utf-8")).hexdigest()[:24]


class BraveDiscoveryService:
    """Persist unique Brave results into the existing discovery queue."""

    def __init__(
        self,
        settings: Settings,
        repository: Any,
        adapter: BraveSearchAdapter,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._settings = settings
        self._repository = repository
        self._adapter = adapter
        self._now = now or (lambda: datetime.now(tz=UTC))
        self._lock = asyncio.Lock()

    def block_reason(self, state: Mapping[str, Any] | None = None) -> str | None:
        if not self._settings.brave_html_enabled:
            return "BRAVE_HTML_ENABLED is false"
        if not self._settings.brave_html_access_approved:
            return "Brave HTML access approval has not been confirmed"
        if state and state.get("brave_paused"):
            reason = state.get("brave_pause_reason") or "unspecified restriction"
            return f"Brave adapter persistently paused: {reason}"
        if getattr(self._repository, "configured", True) is False:
            return "Supabase is not configured"
        return None

    async def run(self, *, scheduled: bool = True) -> dict[str, int]:
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
            state = await self._repository.get_runtime_state()
            if scheduled and not state.get("collection_enabled", False):
                return counters

            reason = self.block_reason(state)
            job_id = await self._repository.create_job_run(
                {
                    "phrases": list(self._settings.brave_search_phrases),
                    "interval_hours": self._settings.brave_search_interval_hours,
                    "url_budget": self._settings.max_urls_per_job,
                    "blocked_reason": reason,
                },
                job_type="brave_discovery",
            )
            if reason:
                await self._repository.finish_job_run(job_id, counters, "skipped", reason)
                log.warning("Brave discovery blocked: %s", reason)
                return counters

            budget = RequestBudget(max_domain_requests=self._settings.max_pages_per_domain)
            throttle = DomainThrottle(
                self._settings.per_domain_delay_seconds,
            )
            admitted = 0
            seen: set[str] = set()
            errors: Counter[str] = Counter()
            try:
                for phrase in self._settings.brave_search_phrases:
                    if admitted >= self._settings.max_urls_per_job:
                        break
                    for page in range(self._settings.brave_search_max_pages_per_phrase):
                        if admitted >= self._settings.max_urls_per_job:
                            break
                        try:
                            result_page = await self._adapter.fetch_page(
                                phrase,
                                page=page,
                                budget=budget,
                                throttle=throttle,
                            )
                        except BraveBarrierError as exc:
                            await self._repository.pause_brave(exc.reason)
                            await self._repository.finish_job_run(
                                job_id,
                                counters,
                                "failed",
                                f"brave_access_restriction:{exc.reason}",
                            )
                            log.warning(
                                "Brave access restriction detected; adapter paused: %s",
                                exc.reason,
                            )
                            return counters
                        except BraveParseError as exc:
                            await self._repository.finish_job_run(
                                job_id,
                                counters,
                                "failed",
                                f"brave_parse_failed:{type(exc).__name__}",
                            )
                            log.warning("Brave parser failure (%s); no retry attempted", exc)
                            return counters
                        except BraveAccessNotApprovedError as exc:
                            await self._repository.finish_job_run(
                                job_id, counters, "failed", f"brave_access_gate:{exc}"
                            )
                            return counters
                        except brave_safe_exceptions() as exc:
                            errors[f"brave_request_failed:{type(exc).__name__}"] += 1
                            await self._repository.finish_job_run(
                                job_id,
                                counters,
                                "failed",
                                f"brave_request_failed:{type(exc).__name__}",
                            )
                            log.warning("Brave request failed (%s)", type(exc).__name__)
                            return counters

                        if not result_page.results:
                            break
                        for result in result_page.results:
                            counters["discovered_count"] += 1
                            if result.url in seen:
                                counters["duplicate_count"] += 1
                                continue
                            seen.add(result.url)
                            counters["examined_count"] += 1
                            if admitted >= self._settings.max_urls_per_job:
                                break
                            try:
                                payload = self._discovery_payload(result)
                                _row, created = await self._repository.upsert_discovery_record(
                                    payload
                                )
                            except Exception as exc:
                                counters["failed_count"] += 1
                                errors[f"discovery_insert_failed:{type(exc).__name__}"] += 1
                                log.warning(
                                    "Brave discovery insert failed (%s)",
                                    type(exc).__name__,
                                )
                                continue
                            if created:
                                admitted += 1
                                counters["processed_count"] += 1
                            else:
                                counters["duplicate_count"] += 1

                outcome = "partial" if errors else "succeeded"
                error_summary = (
                    "; ".join(f"{name}: {count}" for name, count in sorted(errors.items()))[:500]
                    or None
                )
                await self._repository.finish_job_run(job_id, counters, outcome, error_summary)
                if outcome == "succeeded":
                    await self._repository.mark_brave_success()
            except Exception as exc:
                counters["failed_count"] += 1
                await self._repository.finish_job_run(
                    job_id,
                    counters,
                    "failed",
                    f"brave_discovery_failed:{type(exc).__name__}",
                )
                log.exception("Brave discovery failed")
            return counters

    def _discovery_payload(self, result: BraveResult) -> dict[str, Any]:
        discovered_at = self._now().isoformat()
        metadata = result.to_metadata()
        metadata["discovered_at"] = discovered_at
        metadata["origin_source_type"] = "brave_html"
        return {
            "url": result.original_url,
            "normalized_url": result.url,
            "source_type": "brave_html",
            "source_platform_id": _stable_result_id(result.url),
            "title": result.title or None,
            "description": result.snippet or None,
            "domain": urlsplit(result.url).hostname,
            "depth": 0,
            "status": "queued",
            "metadata_json": metadata,
        }


def brave_safe_exceptions() -> tuple[type[BaseException], ...]:
    """Ordinary Brave transport failures that must not be mistaken for barriers."""
    return (BraveRequestError, OSError, TimeoutError)
