"""Conservative robots.txt evaluation and per-job caching.

Policy choices documented for operators:

* ``2xx``: parse the file and honour ``Disallow`` and ``Crawl-delay``.
* ``404``/``410``: treat as no restrictions (RFC 9309 unavailable policy).
* ``401``/``403``/``429``/``5xx``/network failure/unreadable body: treat the
  whole origin as unavailable for the remainder of the job. This is stricter
  than the RFC but ensures an unreadable policy can never be used to ignore an
  explicit restriction.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

from app.discovery.http import (
    BLOCKED,
    DOMAIN_BUDGET,
    DOMAIN_PAUSED,
    NETWORK_ERROR,
    OK,
    RATE_LIMITED,
    SERVER_ERROR,
    TIMEOUT,
    FetchResult,
)

log = logging.getLogger(__name__)

_ROBOTS_ALLOWED_STATUSES = frozenset({200, 203, 204, 206})
_ROBOTS_MISSING_STATUSES = frozenset({404, 410})


@dataclass(frozen=True, slots=True)
class RobotsDecision:
    allow_all: bool
    parser: RobotFileParser | None
    crawl_delay: float | None
    reason: str
    pause_domain: bool = False
    retry_after_seconds: float | None = None

    def can_fetch(self, user_agent: str, url: str) -> bool:
        if self.allow_all:
            return True
        if self.parser is None:
            return False
        try:
            return self.parser.can_fetch(user_agent, url)
        except Exception:
            return False


def robots_url_for(url: str) -> str:
    parsed = urlsplit(url)
    host = parsed.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    netloc = host if parsed.port is None else f"{host}:{parsed.port}"
    return urlunsplit((parsed.scheme or "https", netloc, "/robots.txt", "", ""))


def parse_robots(text: str, user_agent: str) -> RobotFileParser:
    parser = RobotFileParser()
    parser.parse(text.splitlines())
    # Calling crawl_delay primes no state but documents that the parser is
    # queried through the same user-agent identity used for page requests.
    parser.crawl_delay(user_agent)
    return parser


def evaluate_robots_result(result: FetchResult, *, user_agent: str) -> RobotsDecision:
    status = result.status
    if status in _ROBOTS_MISSING_STATUSES:
        return RobotsDecision(True, None, None, "robots_missing_allow")

    if result.category == OK or status in _ROBOTS_ALLOWED_STATUSES:
        if not result.body:
            return RobotsDecision(True, None, None, "robots_empty_allow")
        try:
            text = result.body.decode("utf-8", errors="replace")
        except Exception:
            return RobotsDecision(False, None, None, "robots_unreadable", True)
        parser = parse_robots(text, user_agent)
        delay = parser.crawl_delay(user_agent)
        if delay is None:
            delay = parser.crawl_delay("*")
        try:
            crawl_delay = float(delay) if delay is not None else None
        except (TypeError, ValueError):
            crawl_delay = None
        if crawl_delay is not None:
            crawl_delay = max(0.0, min(crawl_delay, 60.0))
        return RobotsDecision(False, parser, crawl_delay, "robots_parsed")

    if result.category == RATE_LIMITED or status == 429:
        return RobotsDecision(
            False,
            None,
            None,
            "robots_rate_limited",
            True,
            result.retry_after_seconds,
        )
    if result.category == BLOCKED or status in {401, 403}:
        return RobotsDecision(False, None, None, "robots_access_denied", True)
    if result.category in {SERVER_ERROR, NETWORK_ERROR, TIMEOUT, DOMAIN_PAUSED, DOMAIN_BUDGET}:
        return RobotsDecision(False, None, None, f"robots_unavailable:{result.category}", True)
    if status is None:
        return RobotsDecision(False, None, None, "robots_unavailable", True)
    return RobotsDecision(False, None, None, f"robots_unexpected_status:{status}", True)


class RobotsCache:
    """Cache robots decisions per origin for one crawl job."""

    def __init__(self, *, user_agent: str) -> None:
        self._user_agent = user_agent
        self._decisions: dict[str, RobotsDecision] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    @staticmethod
    def _origin(url: str) -> str:
        parsed = urlsplit(url)
        return f"{parsed.scheme}://{parsed.netloc}".lower()

    async def decision_for(
        self,
        url: str,
        *,
        fetch: Callable[[str], Awaitable[FetchResult]],
    ) -> RobotsDecision:
        origin = self._origin(url)
        cached = self._decisions.get(origin)
        if cached is not None:
            return cached
        lock = self._locks.setdefault(origin, asyncio.Lock())
        async with lock:
            cached = self._decisions.get(origin)
            if cached is not None:
                return cached
            try:
                result = await fetch(robots_url_for(url))
                decision = evaluate_robots_result(result, user_agent=self._user_agent)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("Robots evaluation failed (%s)", type(exc).__name__)
                decision = RobotsDecision(False, None, None, "robots_unavailable", True)
            self._decisions[origin] = decision
            return decision

    def decision_for_url(self, url: str) -> RobotsDecision | None:
        return self._decisions.get(self._origin(url))


def robots_fetch_max_bytes(settings: Any) -> int:
    return min(512_000, int(getattr(settings, "max_page_bytes", 5_000_000)))
