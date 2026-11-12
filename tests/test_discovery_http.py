from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from types import SimpleNamespace

import aiohttp
import pytest

from app.discovery.http import (
    BLOCKED,
    DOMAIN_BUDGET,
    DOMAIN_PAUSED,
    HTTP_ERROR,
    INVALID_REDIRECT,
    NETWORK_ERROR,
    OK,
    OVERSIZE,
    RATE_LIMITED,
    ROBOTS_DISALLOWED,
    SERVER_ERROR,
    TIMEOUT,
    TOO_MANY_REDIRECTS,
    UNSUPPORTED_CONTENT_TYPE,
    DomainThrottle,
    RedirectPolicyResult,
    RequestBudget,
    SafeHttpClient,
    parse_retry_after,
    retry_delay_seconds,
)
from app.discovery.url_safety import (
    UrlSafetyError,
    validate_url_literal,
)


class FakeContent:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    async def iter_chunked(self, _size: int):
        for chunk in self._chunks:
            yield chunk


class FakeResponse:
    def __init__(
        self,
        *,
        status: int = 200,
        headers: dict[str, str] | None = None,
        chunks: list[bytes] | None = None,
    ) -> None:
        self.status = status
        self.headers = headers or {}
        self.content = FakeContent(chunks or [])


class FakeRequest:
    def __init__(
        self,
        response: FakeResponse | None = None,
        *,
        raises: BaseException | None = None,
    ) -> None:
        self._response = response
        self._raises = raises

    async def __aenter__(self) -> FakeResponse:
        if self._raises is not None:
            raise self._raises
        assert self._response is not None
        return self._response

    async def __aexit__(self, *_exc_info: object) -> bool:
        return False


class FakeSession:
    def __init__(
        self,
        responses: list[FakeResponse] | None = None,
        *,
        raises: BaseException | None = None,
    ) -> None:
        self._responses = list(responses or [])
        self._raises = raises
        self.calls: list[tuple[str, dict[str, object]]] = []

    def get(self, url: str, **kwargs: object) -> FakeRequest:
        self.calls.append((url, kwargs))
        if self._raises is not None:
            raise self._raises
        if not self._responses:
            raise AssertionError(f"Unexpected request for {url}")
        return FakeRequest(self._responses.pop(0))


async def validating(url: str) -> SimpleNamespace:
    normalized = validate_url_literal(url)
    return SimpleNamespace(url=normalized, addresses=("8.8.8.8",))


def budget(limit: int = 20) -> RequestBudget:
    return RequestBudget(max_domain_requests=limit)


def throttle(delay: float = 0.0) -> DomainThrottle:
    async def no_sleep(_seconds: float) -> None:
        return None

    return DomainThrottle(delay, sleep=no_sleep)


def html_response(
    *,
    status: int = 200,
    body: bytes = b"<html><body>ok</body></html>",
    headers: dict[str, str] | None = None,
    chunks: list[bytes] | None = None,
) -> FakeResponse:
    merged = {"Content-Type": "text/html; charset=utf-8", **(headers or {})}
    return FakeResponse(
        status=status,
        headers=merged,
        chunks=chunks if chunks is not None else [body],
    )


def make_client(session: FakeSession, **overrides: object) -> SafeHttpClient:
    options: dict[str, object] = {
        "session": session,
        "max_concurrent_requests": 5,
        "max_redirects": 5,
        "validator": validating,
    }
    options.update(overrides)
    return SafeHttpClient(**options)


async def fetch(
    client: SafeHttpClient,
    url: str = "https://example.com/page",
    **overrides: object,
):
    options: dict[str, object] = {
        "budget": budget(),
        "throttle": throttle(),
        "accept": "text/html",
        "allowed_content_types": frozenset({"text/html"}),
        "max_bytes": 1_000_000,
    }
    options.update(overrides)
    return await client.fetch(url, **options)


def test_parse_retry_after_supports_seconds_and_http_dates() -> None:
    now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    future = format_datetime(now + timedelta(seconds=45), usegmt=True)

    assert parse_retry_after({"Retry-After": "120"}, now=now) == 120.0
    assert parse_retry_after({"retry-after": "0.5"}, now=now) == 0.5
    assert parse_retry_after({"Retry-After": "-4"}, now=now) == 0.0
    assert parse_retry_after({"Retry-After": "NaN"}, now=now) is None
    assert parse_retry_after({"Retry-After": "1e100"}, now=now) < 300_000_000_000
    assert parse_retry_after({"Retry-After": future}, now=now) == pytest.approx(45.0)
    assert parse_retry_after({"Retry-After": "not-a-delay"}, now=now) is None
    assert parse_retry_after({}, now=now) is None


def test_request_budget_counts_and_exhausts_per_domain() -> None:
    budget_ = budget(2)

    assert budget_.try_acquire("Example.com") is True
    assert budget_.try_acquire("example.com") is True
    assert budget_.try_acquire("example.com") is False
    assert budget_.request_count("EXAMPLE.COM") == 2
    assert budget_.budget_exhausted("example.com") is True
    assert budget_.try_acquire("other.example") is True


def test_request_budget_shares_a_registrable_domain_limit_across_subdomains() -> None:
    budget_ = budget(1)

    assert (
        budget_.try_acquire(
            "www.example.com",
            registrable_domain="example.com",
            origin="https://www.example.com",
        )
        is True
    )
    assert (
        budget_.try_acquire(
            "artist.example.com",
            registrable_domain="example.com",
            origin="https://artist.example.com",
        )
        is False
    )


def test_request_budget_pause_can_expire() -> None:
    budget_ = budget(5)
    now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    budget_.pause("example.com", "http_403_access_denied", until=now + timedelta(seconds=60))

    assert budget_.try_acquire("example.com", now=now) is False
    assert budget_.pause_reason("example.com", now=now) == "http_403_access_denied"
    assert budget_.pause_until("example.com") == now + timedelta(seconds=60)

    later = now + timedelta(seconds=61)
    assert budget_.pause_reason("example.com", now=later) is None
    assert budget_.try_acquire("example.com", now=later) is True


def test_request_budget_requires_a_positive_limit() -> None:
    with pytest.raises(ValueError, match="positive"):
        RequestBudget(max_domain_requests=0)


async def test_domain_throttle_serializes_start_times() -> None:
    clock = [100.0]
    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock[0] += seconds

    throttle_ = DomainThrottle(2.0, sleep=sleep, clock=lambda: clock[0])

    await throttle_.wait("example.com")
    await throttle_.wait("example.com")

    assert sleeps == [2.0]


async def test_concurrent_requests_reserve_distinct_domain_start_slots() -> None:
    clock = [0.0]
    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        await asyncio.sleep(0)
        clock[0] += seconds

    throttle_ = DomainThrottle(2.0, sleep=sleep, clock=lambda: clock[0])
    await asyncio.gather(*(throttle_.wait("example.com") for _ in range(3)))

    assert sorted(sleeps) == [2.0, 4.0]


async def test_domain_throttle_uses_the_larger_crawl_delay() -> None:
    clock = [0.0]
    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    throttle_ = DomainThrottle(2.0, sleep=sleep, clock=lambda: clock[0])

    await throttle_.wait("example.com", delay_seconds=10.0)
    await throttle_.wait("example.com")

    assert sleeps == [10.0]


async def test_domain_throttle_shares_delay_across_subdomains() -> None:
    clock = [0.0]
    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock[0] += seconds

    throttle_ = DomainThrottle(2.0, sleep=sleep, clock=lambda: clock[0])

    await throttle_.wait(
        "www.example.com",
        registrable_domain="example.com",
        origin="https://www.example.com",
    )
    await throttle_.wait(
        "artist.example.com",
        registrable_domain="example.com",
        origin="https://artist.example.com",
    )

    assert sleeps == [2.0]


async def test_domain_throttle_adapts_to_failures_retry_after_and_recovers() -> None:
    clock = [0.0]
    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock[0] += seconds

    throttle_ = DomainThrottle(1.0, sleep=sleep, clock=lambda: clock[0])
    throttle_.observe(
        "a.example.com",
        latency_seconds=0.2,
        status=429,
        category=RATE_LIMITED,
        retry_after_seconds=5.0,
        registrable_domain="example.com",
        origin="https://a.example.com",
    )

    await throttle_.wait("a.example.com", registrable_domain="example.com")
    await throttle_.wait("a.example.com", registrable_domain="example.com")
    assert sleeps[:2] == [5.0, 2.0]

    throttle_.observe(
        "a.example.com",
        latency_seconds=0.1,
        status=200,
        category=OK,
        registrable_domain="example.com",
        origin="https://a.example.com",
    )
    previous_delay = throttle_._adaptive_delay["example.com"]
    throttle_.observe(
        "a.example.com",
        latency_seconds=0.1,
        status=200,
        category=OK,
        registrable_domain="example.com",
        origin="https://a.example.com",
    )
    throttle_.observe(
        "other.example",
        latency_seconds=0.1,
        status=200,
        category=OK,
        registrable_domain="other.example",
        origin="https://other.example",
    )

    assert throttle_._adaptive_delay["example.com"] < previous_delay
    assert throttle_._adaptive_delay["other.example"] == 1.0


def test_domain_throttle_increases_after_repeated_slow_successes() -> None:
    throttle_ = DomainThrottle(1.0, clock=lambda: 10.0)

    for _ in range(2):
        throttle_.observe(
            "slow.example",
            latency_seconds=3.0,
            status=200,
            category=OK,
            registrable_domain="slow.example",
        )

    assert throttle_._adaptive_delay["slow.example"] == 1.25


async def test_fetch_returns_a_successful_bounded_response() -> None:
    session = FakeSession([html_response(body=b"<html>hello</html>")])
    client = make_client(session)

    result = await fetch(client)

    assert result.ok is True
    assert result.category == OK
    assert result.status == 200
    assert result.body == b"<html>hello</html>"
    assert result.content_type == "text/html"
    assert result.redirects == 0
    assert session.calls[0][0] == "https://example.com/page"
    assert session.calls[0][1]["allow_redirects"] is False


async def test_fetch_rejects_missing_or_wrong_content_types() -> None:
    missing = FakeSession([FakeResponse(headers={}, chunks=[b"x"])])
    missing_result = await fetch(make_client(missing))
    assert missing_result.category == UNSUPPORTED_CONTENT_TYPE
    assert missing_result.error == "missing_content_type"

    allowed_missing = FakeSession([FakeResponse(headers={}, chunks=[b"<html>x</html>"])])
    allowed_result = await fetch(make_client(allowed_missing), allow_missing_content_type=True)
    assert allowed_result.category == OK

    wrong = FakeSession([html_response(headers={"Content-Type": "application/pdf"})])
    wrong_result = await fetch(make_client(wrong))
    assert wrong_result.category == UNSUPPORTED_CONTENT_TYPE
    assert wrong_result.content_type == "application/pdf"


async def test_fetch_rejects_oversize_content_length_and_streams() -> None:
    by_header = FakeSession([html_response(headers={"Content-Length": "999999"})])
    header_result = await fetch(make_client(by_header), max_bytes=10)
    assert header_result.category == OVERSIZE
    assert header_result.error == "content_length_exceeds_limit"

    streamed = FakeSession([html_response(chunks=[b"a" * 8, b"b" * 8])])
    stream_result = await fetch(make_client(streamed), max_bytes=10)
    assert stream_result.category == OVERSIZE
    assert stream_result.error == "response_body_exceeds_limit"


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (401, BLOCKED),
        (403, BLOCKED),
        (429, RATE_LIMITED),
        (500, SERVER_ERROR),
        (503, SERVER_ERROR),
        (404, HTTP_ERROR),
    ],
)
async def test_fetch_maps_http_statuses_to_stable_categories(status: int, expected: str) -> None:
    session = FakeSession([html_response(status=status)])
    result = await fetch(make_client(session))

    assert result.category == expected
    assert result.status == status


async def test_fetch_preserves_retry_after_on_rate_limits() -> None:
    session = FakeSession([html_response(status=429, headers={"Retry-After": "30"})])
    result = await fetch(make_client(session))

    assert result.category == RATE_LIMITED
    assert result.retry_after_seconds == 30.0


async def test_fetch_follows_and_revalidates_redirects() -> None:
    session = FakeSession(
        [
            html_response(status=302, headers={"Location": "/next"}),
            html_response(body=b"final"),
        ]
    )
    client = make_client(session)

    result = await fetch(client)

    assert result.category == OK
    assert result.final_url == "https://example.com/next"
    assert result.redirects == 1
    assert result.body == b"final"
    assert [call[0] for call in session.calls] == [
        "https://example.com/page",
        "https://example.com/next",
    ]


async def test_cross_domain_redirect_checks_robots_before_each_hop() -> None:
    session = FakeSession(
        [
            html_response(status=302, headers={"Location": "https://other.example/page"}),
            html_response(status=302, headers={"Location": "https://third.example/final"}),
            html_response(body=b"final"),
        ]
    )
    checked: list[str] = []

    async def redirect_policy(url: str):
        checked.append(url)
        return None

    result = await fetch(
        make_client(session),
        "https://example.com/start",
        redirect_policy=redirect_policy,
    )

    assert result.category == OK
    assert result.final_url == "https://third.example/final"
    assert checked == ["https://other.example/page", "https://third.example/final"]


async def test_disallowed_redirect_destination_is_not_requested() -> None:
    session = FakeSession(
        [html_response(status=302, headers={"Location": "https://other.example/private"})]
    )

    async def redirect_policy(_url: str):
        return RedirectPolicyResult(False, reason="robots_disallowed")

    result = await fetch(
        make_client(session),
        "https://example.com/start",
        redirect_policy=redirect_policy,
    )

    assert result.category == ROBOTS_DISALLOWED
    assert result.error == "robots_disallowed"
    assert len(session.calls) == 1


async def test_redirect_uses_destination_crawl_delay() -> None:
    session = FakeSession(
        [
            html_response(status=302, headers={"Location": "https://other.example/page"}),
            html_response(body=b"final"),
        ]
    )
    waits: list[tuple[str, float | None]] = []

    class RecordingThrottle:
        async def wait(self, host: str, *, delay_seconds: float | None = None, **_kwargs: object):
            waits.append((host, delay_seconds))

    async def redirect_policy(_url: str):
        return RedirectPolicyResult(True, crawl_delay=7.0)

    result = await fetch(
        make_client(session),
        "https://example.com/start",
        throttle=RecordingThrottle(),
        redirect_policy=redirect_policy,
    )

    assert result.category == OK
    assert waits == [("example.com", None), ("other.example", 7.0)]


async def test_fetch_rejects_a_redirect_to_a_private_destination() -> None:
    session = FakeSession([html_response(status=302, headers={"Location": "http://127.0.0.1/"})])
    result = await fetch(make_client(session))

    assert result.category == INVALID_REDIRECT
    assert len(session.calls) == 1


async def test_fetch_limits_redirects() -> None:
    session = FakeSession(
        [
            html_response(status=301, headers={"Location": "/one"}),
            html_response(status=301, headers={"Location": "/two"}),
        ]
    )
    result = await fetch(make_client(session, max_redirects=1))

    assert result.category == TOO_MANY_REDIRECTS
    assert result.redirects == 1
    assert len(session.calls) == 2


async def test_fetch_treats_a_redirect_without_location_as_an_error() -> None:
    session = FakeSession([html_response(status=302)])
    result = await fetch(make_client(session))

    assert result.category == HTTP_ERROR
    assert result.error == "redirect_without_location"


async def test_fetch_blocks_a_destination_rejected_by_the_validator() -> None:
    async def blocking_validator(_url: str) -> SimpleNamespace:
        raise UrlSafetyError("blocked_destination", "not public")

    session = FakeSession([])
    result = await fetch(make_client(session, validator=blocking_validator))

    assert result.category == BLOCKED
    assert result.error == "blocked_destination"
    assert session.calls == []


async def test_fetch_honours_domain_pause_and_request_budget() -> None:
    session = FakeSession([html_response()])
    budget_ = budget(1)
    budget_.pause("example.com", "http_403_access_denied")
    paused = await fetch(make_client(session), budget=budget_)
    assert paused.category == DOMAIN_PAUSED
    assert session.calls == []

    exhausted_budget = budget(1)
    assert exhausted_budget.try_acquire("example.com") is True
    exhausted = await fetch(make_client(session), budget=exhausted_budget)
    assert exhausted.category == DOMAIN_BUDGET
    assert session.calls == []


async def test_fetch_maps_transport_errors_and_timeouts() -> None:
    network = FakeSession(raises=aiohttp.ClientConnectionError("refused"))
    network_result = await fetch(make_client(network))
    assert network_result.category == NETWORK_ERROR
    assert "ClientConnectionError" in network_result.error

    timed_out = SafeHttpClient(session=FakeSession(), validator=validating)

    async def raise_timeout(_url: str):
        raise TimeoutError

    timed_out._get_session = lambda: SimpleNamespace(  # type: ignore[method-assign]
        get=lambda *_args, **_kwargs: FakeRequest(raises=TimeoutError())
    )
    timeout_result = await fetch(timed_out)
    assert timeout_result.category == TIMEOUT


async def test_client_close_prevents_further_requests() -> None:
    client = make_client(FakeSession([html_response()]))
    await client.close()

    with pytest.raises(RuntimeError, match="closed"):
        await fetch(client)


def test_retry_delay_uses_exponential_backoff_and_respects_server_delay() -> None:
    def jitter(low: float, high: float) -> float:
        return (low + high) / 2

    assert retry_delay_seconds(1, jitter=jitter) == pytest.approx(0.625)
    assert retry_delay_seconds(2, jitter=jitter) == pytest.approx(1.125)
    assert retry_delay_seconds(20, jitter=jitter) == pytest.approx(8.125)
    assert retry_delay_seconds(1, retry_after=100.0) == 100.0
    assert retry_delay_seconds(1, retry_after=-5.0) == 0.0
