"""Shared bounded HTTP transport for discovery and crawling.

Every request is validated immediately before it is sent, every redirect is
validated independently, and connections use a public-only resolver so a
hostname cannot pass an earlier check and then rebind to a private address.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urljoin

import aiohttp

from app.discovery.domains import domain_identity
from app.discovery.url_safety import (
    PublicResolver,
    UrlSafetyError,
    is_redirect_status,
    redirect_location,
    validate_public_url,
)

log = logging.getLogger(__name__)

CRAWLER_USER_AGENT = "TifaLockhartBot/1.0 (responsible public-page inspection)"

OK = "ok"
BLOCKED = "blocked"
RATE_LIMITED = "rate_limited"
SERVER_ERROR = "server_error"
NETWORK_ERROR = "network_error"
TIMEOUT = "timeout"
HTTP_ERROR = "http_error"
TOO_MANY_REDIRECTS = "too_many_redirects"
INVALID_REDIRECT = "invalid_redirect"
OVERSIZE = "oversize"
UNSUPPORTED_CONTENT_TYPE = "unsupported_content_type"
DOMAIN_PAUSED = "domain_paused"
DOMAIN_BUDGET = "domain_budget"
ROBOTS_DISALLOWED = "robots_disallowed"

HTML_CONTENT_TYPES = frozenset({"text/html", "application/xhtml+xml"})
ROBOTS_CONTENT_TYPES = frozenset({"text/plain", "text/html", "application/xhtml+xml"})
_REDIRECT_CODES = frozenset({301, 302, 303, 307, 308})
_CHUNK_SIZE = 64 * 1024


def _header(headers: Any, name: str) -> str | None:
    if headers is None:
        return None
    getter = getattr(headers, "get", None)
    if callable(getter):
        value = getter(name)
        if value is None:
            value = getter(name.lower())
        if value is not None:
            return str(value)
    try:
        items = headers.items()
    except (AttributeError, TypeError):
        return None
    lowered = name.lower()
    for key, value in items:
        if str(key).lower() == lowered:
            return str(value)
    return None


def parse_retry_after(headers: Any, *, now: datetime | None = None) -> float | None:
    """Parse Retry-After delay-seconds or HTTP-date into a non-negative delay."""
    raw = _header(headers, "Retry-After")
    if not raw:
        return None
    value = raw.strip()
    try:
        delay = float(value)
        if not math.isfinite(delay):
            return None
        current = now or datetime.now(tz=UTC)
        if current.tzinfo is None:
            current = current.replace(tzinfo=UTC)
        maximum = (datetime.max.replace(tzinfo=UTC) - current.astimezone(UTC)).total_seconds()
        return min(max(0.0, delay), max(0.0, maximum - 1.0))
    except ValueError:
        pass
    try:
        retry_at = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if retry_at.tzinfo is None:
        retry_at = retry_at.replace(tzinfo=UTC)
    current = now or datetime.now(tz=UTC)
    return max(0.0, (retry_at - current).total_seconds())


def _content_type(headers: Any) -> str | None:
    raw = _header(headers, "Content-Type")
    if not raw:
        return None
    return raw.split(";", 1)[0].strip().lower() or None


def _content_length(headers: Any) -> int | None:
    raw = _header(headers, "Content-Length")
    if raw is None:
        return None
    try:
        value = int(raw.strip())
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


@dataclass(frozen=True, slots=True)
class FetchResult:
    requested_url: str
    final_url: str | None
    status: int | None
    category: str
    content_type: str | None = None
    body: bytes = b""
    error: str | None = None
    retry_after_seconds: float | None = None
    redirects: int = 0
    attempts: int = 1
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.category == OK


@dataclass(frozen=True, slots=True)
class RedirectPolicyResult:
    allowed: bool
    crawl_delay: float | None = None
    reason: str | None = None
    retry_after_seconds: float | None = None


@dataclass(slots=True)
class _DomainState:
    requests: int = 0
    pause_reason: str | None = None
    pause_until: datetime | None = None


def _active_pause(state: _DomainState, now: datetime) -> bool:
    if state.pause_reason is None:
        return False
    if state.pause_until is not None and state.pause_until <= now:
        state.pause_reason = None
        state.pause_until = None
        return False
    return True


class RequestBudget:
    """Per-job domain request accounting and restriction pauses."""

    def __init__(self, *, max_domain_requests: int) -> None:
        if max_domain_requests < 1:
            raise ValueError("max_domain_requests must be positive")
        self._max_domain_requests = max_domain_requests
        self._domains: dict[str, _DomainState] = {}
        self._sites: dict[str, _DomainState] = {}
        self._origins: dict[str, _DomainState] = {}

    def _state(self, host: str) -> _DomainState:
        return self._domains.setdefault(host.lower(), _DomainState())

    def try_acquire(
        self,
        host: str,
        *,
        registrable_domain: str | None = None,
        origin: str | None = None,
        now: datetime | None = None,
    ) -> bool:
        current = now or datetime.now(tz=UTC)
        state = self._state(host)
        site = self._sites.setdefault((registrable_domain or host).lower(), _DomainState())
        origin_state = self._origins.setdefault((origin or host).lower(), _DomainState())
        if any(_active_pause(item, current) for item in (state, site, origin_state)):
            return False
        if any(item.requests >= self._max_domain_requests for item in (state, site, origin_state)):
            return False
        for item in (state, site, origin_state):
            item.requests += 1
        return True

    def pause(
        self,
        host: str,
        reason: str,
        *,
        until: datetime | None = None,
        registrable_domain: str | None = None,
        origin: str | None = None,
        site_wide: bool = False,
    ) -> None:
        targets = [self._state(host)]
        if origin:
            targets.append(self._origins.setdefault(origin.lower(), _DomainState()))
        if site_wide and registrable_domain:
            targets.append(self._sites.setdefault(registrable_domain.lower(), _DomainState()))
        for state in targets:
            state.pause_reason = reason
            state.pause_until = until

    def pause_reason(
        self,
        host: str,
        *,
        registrable_domain: str | None = None,
        origin: str | None = None,
        now: datetime | None = None,
    ) -> str | None:
        current = now or datetime.now(tz=UTC)
        states = [self._state(host)]
        if registrable_domain:
            states.append(self._sites.setdefault(registrable_domain.lower(), _DomainState()))
        if origin:
            states.append(self._origins.setdefault(origin.lower(), _DomainState()))
        return next((state.pause_reason for state in states if _active_pause(state, current)), None)

    def pause_until(self, host: str) -> datetime | None:
        return self._state(host).pause_until

    def request_count(self, host: str) -> int:
        return self._state(host).requests

    def budget_exhausted(self, host: str) -> bool:
        return self._state(host).requests >= self._max_domain_requests


class DomainThrottle:
    """Enforce a minimum start-to-start interval per normalized hostname."""

    def __init__(
        self,
        delay_seconds: float,
        *,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if delay_seconds < 0:
            raise ValueError("delay_seconds must not be negative")
        self._delay = delay_seconds
        self._sleep = sleep
        self._clock = clock
        self._locks: dict[str, asyncio.Lock] = {}
        self._next_start: dict[str, float] = {}

    async def wait(
        self,
        host: str,
        *,
        delay_seconds: float | None = None,
        registrable_domain: str | None = None,
        origin: str | None = None,
    ) -> None:
        # The site bucket prevents subdomains from bypassing fairness and
        # politeness. Host and origin buckets preserve finer-grained limits.
        keys = [key.lower() for key in (registrable_domain, host, origin) if key]
        effective_delay = max(self._delay, delay_seconds or 0.0)
        for normalized in dict.fromkeys(keys):
            lock = self._locks.setdefault(normalized, asyncio.Lock())
            async with lock:
                now = self._clock()
                target = max(now, self._next_start.get(normalized, now))
                if target > now:
                    await self._sleep(target - now)
                self._next_start[normalized] = max(self._clock(), target) + effective_delay


class SafeHttpClient:
    """Small aiohttp wrapper with redirect, SSRF, and size protections."""

    def __init__(
        self,
        *,
        session: Any | None = None,
        max_concurrent_requests: int = 5,
        max_redirects: int = 5,
        connect_timeout: float = 10.0,
        read_timeout: float = 20.0,
        validator: Callable[..., Awaitable[Any]] = validate_public_url,
        user_agent: str = CRAWLER_USER_AGENT,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if max_concurrent_requests < 1:
            raise ValueError("max_concurrent_requests must be positive")
        if max_redirects < 0:
            raise ValueError("max_redirects must not be negative")
        self._session = session
        self._owns_session = session is None
        self._max_redirects = max_redirects
        self._connect_timeout = connect_timeout
        self._read_timeout = read_timeout
        self._validator = validator
        self._user_agent = user_agent
        self._semaphore = asyncio.Semaphore(max_concurrent_requests)
        self._closed = False

    async def __aenter__(self) -> SafeHttpClient:
        self._get_session()
        return self

    async def __aexit__(self, *_exc_info: object) -> bool:
        await self.close()
        return False

    def _get_session(self) -> Any:
        if self._closed:
            raise RuntimeError("HTTP client is closed")
        if self._session is None:
            connector = aiohttp.TCPConnector(resolver=PublicResolver())
            self._session = aiohttp.ClientSession(
                connector=connector,
                cookie_jar=aiohttp.DummyCookieJar(),
                trust_env=False,
            )
            self._owns_session = True
        return self._session

    def _timeout(self) -> Any:
        return aiohttp.ClientTimeout(
            total=None,
            connect=self._connect_timeout,
            sock_read=self._read_timeout,
        )

    async def close(self) -> None:
        session = self._session
        owns = self._owns_session
        self._session = None
        self._owns_session = False
        self._closed = True
        if owns and session is not None:
            try:
                await session.close()
            except Exception as exc:
                log.debug("Ignoring HTTP session close error (%s)", type(exc).__name__)

    async def fetch(
        self,
        url: str,
        *,
        budget: RequestBudget,
        throttle: DomainThrottle,
        accept: str,
        allowed_content_types: frozenset[str] | None,
        max_bytes: int,
        crawl_delay: float | None = None,
        allow_missing_content_type: bool = False,
        redirect_policy: Callable[[str], Awaitable[Any]] | None = None,
    ) -> FetchResult:
        if max_bytes < 1:
            raise ValueError("max_bytes must be positive")
        current_url = url
        redirects = 0

        for _hop in range(self._max_redirects + 1):
            try:
                destination = await self._validator(current_url)
            except UrlSafetyError as exc:
                return FetchResult(
                    requested_url=url,
                    final_url=current_url,
                    status=None,
                    category=BLOCKED,
                    error=exc.reason,
                    redirects=redirects,
                )
            except Exception as exc:
                return FetchResult(
                    requested_url=url,
                    final_url=current_url,
                    status=None,
                    category=NETWORK_ERROR,
                    error=f"validation_error:{type(exc).__name__}",
                    redirects=redirects,
                )

            normalized = destination.url
            identity = domain_identity(normalized.normalized)
            host = identity.hostname
            pause_reason = budget.pause_reason(
                host,
                registrable_domain=identity.registrable_domain,
                origin=identity.origin,
            )
            if pause_reason:
                return FetchResult(
                    requested_url=url,
                    final_url=normalized.normalized,
                    status=None,
                    category=DOMAIN_PAUSED,
                    error=pause_reason,
                    redirects=redirects,
                )
            if not budget.try_acquire(
                host,
                registrable_domain=identity.registrable_domain,
                origin=identity.origin,
            ):
                return FetchResult(
                    requested_url=url,
                    final_url=normalized.normalized,
                    status=None,
                    category=DOMAIN_BUDGET,
                    error="domain_request_budget_exhausted",
                    redirects=redirects,
                )

            try:
                await throttle.wait(
                    host,
                    delay_seconds=crawl_delay,
                    registrable_domain=identity.registrable_domain,
                    origin=identity.origin,
                )
            except asyncio.CancelledError:
                raise

            redirect_target: str | None = None
            try:
                async with self._semaphore:
                    request = self._get_session().get(
                        normalized.normalized,
                        timeout=self._timeout(),
                        allow_redirects=False,
                        headers={
                            "User-Agent": self._user_agent,
                            "Accept": accept,
                            "Accept-Language": "en;q=0.8,*;q=0.5",
                        },
                    )
                    async with request as response:
                        status = int(getattr(response, "status", 0) or 0)
                        headers = getattr(response, "headers", None) or {}
                        content_type = _content_type(headers)

                        if is_redirect_status(status):
                            location = redirect_location(headers)
                            if not location:
                                return FetchResult(
                                    requested_url=url,
                                    final_url=normalized.normalized,
                                    status=status,
                                    category=HTTP_ERROR,
                                    content_type=content_type,
                                    error="redirect_without_location",
                                    redirects=redirects,
                                )
                            if redirects >= self._max_redirects:
                                return FetchResult(
                                    requested_url=url,
                                    final_url=normalized.normalized,
                                    status=status,
                                    category=TOO_MANY_REDIRECTS,
                                    content_type=content_type,
                                    error="too_many_redirects",
                                    redirects=redirects,
                                )
                            next_url = urljoin(normalized.normalized, location)
                            try:
                                next_destination = await self._validator(next_url)
                            except UrlSafetyError as exc:
                                return FetchResult(
                                    requested_url=url,
                                    final_url=next_url,
                                    status=status,
                                    category=INVALID_REDIRECT,
                                    content_type=content_type,
                                    error=exc.reason,
                                    redirects=redirects,
                                )
                            except Exception as exc:
                                return FetchResult(
                                    requested_url=url,
                                    final_url=next_url,
                                    status=status,
                                    category=NETWORK_ERROR,
                                    error=f"redirect_validation_error:{type(exc).__name__}",
                                    redirects=redirects,
                                )
                            redirect_target = next_destination.url.normalized
                            redirects += 1
                        elif not 200 <= status < 300:
                            if status in {401, 403}:
                                category = BLOCKED
                            elif status == 429:
                                category = RATE_LIMITED
                            elif status >= 500:
                                category = SERVER_ERROR
                            else:
                                category = HTTP_ERROR
                            return FetchResult(
                                requested_url=url,
                                final_url=normalized.normalized,
                                status=status,
                                category=category,
                                content_type=content_type,
                                error=f"http_{status}",
                                retry_after_seconds=parse_retry_after(headers),
                                redirects=redirects,
                            )

                        elif (
                            allowed_content_types is not None
                            and content_type is not None
                            and content_type not in allowed_content_types
                        ):
                            return FetchResult(
                                requested_url=url,
                                final_url=normalized.normalized,
                                status=status,
                                category=UNSUPPORTED_CONTENT_TYPE,
                                content_type=content_type,
                                error=f"unsupported_content_type:{content_type}",
                                redirects=redirects,
                            )
                        elif (
                            allowed_content_types is not None
                            and content_type is None
                            and not allow_missing_content_type
                        ):
                            return FetchResult(
                                requested_url=url,
                                final_url=normalized.normalized,
                                status=status,
                                category=UNSUPPORTED_CONTENT_TYPE,
                                content_type=None,
                                error="missing_content_type",
                                redirects=redirects,
                            )

                        content_length = _content_length(headers)
                        if (
                            redirect_target is None
                            and content_length is not None
                            and content_length > max_bytes
                        ):
                            return FetchResult(
                                requested_url=url,
                                final_url=normalized.normalized,
                                status=status,
                                category=OVERSIZE,
                                content_type=content_type,
                                error="content_length_exceeds_limit",
                                redirects=redirects,
                            )

                        if redirect_target is None:
                            chunks: list[bytes] = []
                            total = 0
                            async for chunk in response.content.iter_chunked(_CHUNK_SIZE):
                                if not chunk:
                                    continue
                                total += len(chunk)
                                if total > max_bytes:
                                    return FetchResult(
                                        requested_url=url,
                                        final_url=normalized.normalized,
                                        status=status,
                                        category=OVERSIZE,
                                        content_type=content_type,
                                        error="response_body_exceeds_limit",
                                        redirects=redirects,
                                    )
                                chunks.append(bytes(chunk))
                            return FetchResult(
                                requested_url=url,
                                final_url=normalized.normalized,
                                status=status,
                                category=OK,
                                content_type=content_type,
                                body=b"".join(chunks),
                                redirects=redirects,
                                headers={
                                    name: value
                                    for name in ("Link",)
                                    if (value := _header(headers, name)) is not None
                                },
                            )
            except TimeoutError as exc:
                return FetchResult(
                    requested_url=url,
                    final_url=normalized.normalized,
                    status=None,
                    category=TIMEOUT,
                    error=type(exc).__name__,
                    redirects=redirects,
                )
            except (aiohttp.ClientError, OSError) as exc:
                return FetchResult(
                    requested_url=url,
                    final_url=normalized.normalized,
                    status=None,
                    category=NETWORK_ERROR,
                    error=f"{type(exc).__name__}: {str(exc)[:180]}",
                    redirects=redirects,
                )

            if redirect_target is not None:
                if redirect_policy is not None:
                    try:
                        policy = await redirect_policy(redirect_target)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        return FetchResult(
                            requested_url=url,
                            final_url=redirect_target,
                            status=None,
                            category=ROBOTS_DISALLOWED,
                            error=f"redirect_robots_check_failed:{type(exc).__name__}",
                            redirects=redirects,
                        )
                    if policy is not None:
                        if not bool(getattr(policy, "allowed", False)):
                            return FetchResult(
                                requested_url=url,
                                final_url=redirect_target,
                                status=None,
                                category=ROBOTS_DISALLOWED,
                                error=getattr(policy, "reason", None) or "robots_disallowed",
                                retry_after_seconds=getattr(policy, "retry_after_seconds", None),
                                redirects=redirects,
                            )
                        crawl_delay = getattr(policy, "crawl_delay", None)
                current_url = redirect_target
                continue

        return FetchResult(
            requested_url=url,
            final_url=current_url,
            status=None,
            category=TOO_MANY_REDIRECTS,
            error="too_many_redirects",
            redirects=redirects,
        )


def retry_delay_seconds(
    attempt: int,
    *,
    retry_after: float | None = None,
    jitter: Callable[[float, float], float] | None = None,
    maximum: float = 30.0,
) -> float:
    """Return a bounded exponential backoff or the server's explicit delay."""
    if retry_after is not None:
        # Retry-After is an operator instruction from the server. Do not retry
        # earlier than requested; the queue stores long delays durably.
        return max(0.0, retry_after)
    base = min(8.0, 0.5 * (2 ** max(0, attempt - 1)))
    spread = (jitter or (lambda low, high: (low + high) / 2))(0, 0.25)
    return min(maximum, base + spread)
