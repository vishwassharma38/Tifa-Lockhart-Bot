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
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from app.discovery.domains import domain_identity
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
class _Rule:
    pattern: str
    allow: bool


@dataclass(slots=True)
class _Group:
    agents: list[str] = field(default_factory=list)
    rules: list[_Rule] = field(default_factory=list)
    crawl_delay: float | None = None
    has_directives: bool = False


class RobotsRules:
    """RFC 9309 path matching rather than :mod:`urllib.robotparser`'s first match.

    Python's standard parser returns the first matching rule, so a broad
    ``Disallow: /`` hides a later ``Allow: /gallery``. RFC 9309 requires the
    longest match to win, with ``Allow`` winning ties. This small matcher keeps
    that rule, supports ``*``/``$`` patterns, and combines groups by the most
    specific matching user-agent token.
    """

    def __init__(self, groups: list[_Group], sitemap_urls: tuple[str, ...]) -> None:
        merged: dict[str, _Group] = {}
        for group in groups:
            for agent in group.agents or ["*"]:
                target = merged.setdefault(agent, _Group(agents=[agent]))
                target.rules.extend(group.rules)
                if group.crawl_delay is not None:
                    target.crawl_delay = max(target.crawl_delay or 0.0, group.crawl_delay)
        self._groups = merged
        self._sitemap_urls = sitemap_urls

    def _groups_for(self, user_agent: str) -> list[_Group]:
        lowered = user_agent.lower()
        specific = [
            (len(agent), group)
            for agent, group in self._groups.items()
            if agent != "*" and lowered.startswith(agent)
        ]
        if specific:
            best = max(length for length, _group in specific)
            return [group for length, group in specific if length == best]
        wildcard = self._groups.get("*")
        return [wildcard] if wildcard is not None else []

    def _rules_for(self, user_agent: str) -> list[_Rule]:
        groups = self._groups_for(user_agent)
        return [rule for group in groups for rule in group.rules]

    @staticmethod
    def _matches(pattern: str, path: str) -> bool:
        if not pattern:
            return False
        anchored = pattern.endswith("$")
        body = pattern[:-1] if anchored else pattern
        regex = "".join(".*" if char == "*" else re.escape(char) for char in body)
        return re.match(regex + ("$" if anchored else ""), path) is not None

    def can_fetch(self, user_agent: str, url: str) -> bool:
        parsed = urlsplit(url)
        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"
        best: _Rule | None = None
        for rule in self._rules_for(user_agent):
            if not self._matches(rule.pattern, path):
                continue
            if (
                best is None
                or len(rule.pattern) > len(best.pattern)
                or (len(rule.pattern) == len(best.pattern) and rule.allow and not best.allow)
            ):
                best = rule
        return True if best is None else best.allow

    def crawl_delay(self, user_agent: str) -> float | None:
        delays = [
            group.crawl_delay
            for group in self._groups_for(user_agent)
            if group.crawl_delay is not None
        ]
        return max(delays) if delays else None

    def site_maps(self) -> list[str]:
        return list(self._sitemap_urls)


@dataclass(frozen=True, slots=True)
class RobotsDecision:
    allow_all: bool
    parser: RobotsRules | None
    crawl_delay: float | None
    reason: str
    pause_domain: bool = False
    retry_after_seconds: float | None = None
    sitemap_urls: tuple[str, ...] = ()

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


def parse_robots(text: str, user_agent: str) -> RobotsRules:
    """Parse RFC 9309 groups without the standard library's first-match bug."""
    del user_agent  # Kept for the existing call contract; matching is per query.
    groups: list[_Group] = []
    current: _Group | None = None
    sitemap_urls: list[str] = []
    for raw in text.lstrip("\ufeff").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip().lower()
        value = value.strip()
        if key == "user-agent":
            if current is None or current.has_directives:
                current = _Group()
                groups.append(current)
            current.agents.append(value.lower() or "*")
        elif key in {"allow", "disallow"}:
            if current is None:
                current = _Group(agents=["*"])
                groups.append(current)
            current.rules.append(_Rule(value, key == "allow"))
            current.has_directives = True
        elif key == "crawl-delay":
            if current is None:
                current = _Group(agents=["*"])
                groups.append(current)
            try:
                delay = max(0.0, float(value))
            except ValueError:
                continue
            current.crawl_delay = max(current.crawl_delay or 0.0, delay)
            current.has_directives = True
        elif key == "sitemap" and value:
            sitemap_urls.append(value)
    return RobotsRules(groups, tuple(dict.fromkeys(sitemap_urls)))


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
        try:
            sitemap_urls = tuple(
                value for value in (parser.site_maps() or []) if isinstance(value, str)
            )
        except Exception:
            sitemap_urls = ()
        return RobotsDecision(
            False, parser, crawl_delay, "robots_parsed", sitemap_urls=sitemap_urls
        )

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
    """Cache robots decisions per origin, optionally persisting them in Supabase."""

    def __init__(
        self,
        *,
        user_agent: str,
        load_policy: Callable[[str], Awaitable[dict[str, Any] | None]] | None = None,
        store_policy: Callable[[str, str, str, dict[str, Any]], Awaitable[Any]] | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._user_agent = user_agent
        self._load_policy = load_policy
        self._store_policy = store_policy
        self._now = now or (lambda: datetime.now(tz=UTC))
        self._decisions: dict[str, RobotsDecision] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    @staticmethod
    def _origin(url: str) -> str:
        return domain_identity(url).origin

    async def _persistent_result(self, origin: str) -> FetchResult | None:
        if self._load_policy is None:
            return None
        try:
            cached = await self._load_policy(origin)
            if not cached:
                return None
            expires = cached.get("robots_expires_at")
            if not isinstance(expires, str):
                return None
            try:
                expires_at = datetime.fromisoformat(expires.replace("Z", "+00:00"))
            except ValueError:
                return None
            if expires_at <= self._now().astimezone(UTC):
                return None
            status = cached.get("robots_status")
            category = str(cached.get("robots_category") or "unknown")
            body = cached.get("robots_body")
            return FetchResult(
                requested_url=origin + "/robots.txt",
                final_url=origin + "/robots.txt",
                status=int(status) if status is not None else None,
                category=category,
                content_type="text/plain" if body else None,
                body=body.encode("utf-8") if isinstance(body, str) else b"",
            )
        except Exception as exc:
            log.warning("Persistent robots cache read failed (%s)", type(exc).__name__)
            return None

    async def _persist(self, url: str, origin: str, result: FetchResult) -> None:
        if self._store_policy is None:
            return
        try:
            identity = domain_identity(url)
            cache_seconds = (
                24 * 60 * 60
                if result.status in {*_ROBOTS_ALLOWED_STATUSES, *_ROBOTS_MISSING_STATUSES}
                else 10 * 60
            )
            expires_at = self._now().astimezone(UTC) + timedelta(seconds=cache_seconds)
            body = result.body.decode("utf-8", errors="replace") if result.body else None
            await self._store_policy(
                origin,
                identity.hostname,
                identity.registrable_domain,
                {
                    "robots_body": body,
                    "robots_status": result.status,
                    "robots_category": result.category,
                    "robots_checked_at": self._now().astimezone(UTC).isoformat(),
                    "robots_expires_at": expires_at.isoformat(),
                },
            )
        except Exception as exc:
            log.warning("Persistent robots cache write failed (%s)", type(exc).__name__)

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
                result = await self._persistent_result(origin)
                if result is None:
                    result = await fetch(robots_url_for(url))
                    await self._persist(url, origin, result)
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
