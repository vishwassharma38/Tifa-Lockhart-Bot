from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.discovery.http import (
    BLOCKED,
    NETWORK_ERROR,
    OK,
    RATE_LIMITED,
    SERVER_ERROR,
    TIMEOUT,
    FetchResult,
)
from app.discovery.robots import (
    RobotsCache,
    evaluate_robots_result,
    parse_robots,
    robots_fetch_max_bytes,
    robots_url_for,
)

USER_AGENT = "TifaLockhartBot/1.0 (responsible public-page inspection)"


def make_result(
    *,
    status: int | None,
    category: str,
    body: bytes = b"",
    retry_after: float | None = None,
) -> FetchResult:
    return FetchResult(
        requested_url="https://example.com/robots.txt",
        final_url="https://example.com/robots.txt",
        status=status,
        category=category,
        body=body,
        retry_after_seconds=retry_after,
    )


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://example.com/page", "https://example.com/robots.txt"),
        ("http://example.com/page", "http://example.com/robots.txt"),
        ("https://example.com:8443/page", "https://example.com:8443/robots.txt"),
        ("https://[2606:4700:4700::1111]/page", "https://[2606:4700:4700::1111]/robots.txt"),
    ],
)
def test_robots_url_uses_the_request_origin(url: str, expected: str) -> None:
    assert robots_url_for(url) == expected


def test_parsed_robots_honour_disallow_and_crawl_delay() -> None:
    parser = parse_robots(
        "User-agent: *\nDisallow: /private\nCrawl-delay: 3\n",
        USER_AGENT,
    )

    assert parser.can_fetch(USER_AGENT, "https://example.com/public") is True
    assert parser.can_fetch(USER_AGENT, "https://example.com/private") is False
    assert parser.crawl_delay(USER_AGENT) == 3


def test_robots_result_parses_a_successful_file() -> None:
    decision = evaluate_robots_result(
        make_result(
            status=200,
            category=OK,
            body=b"User-agent: *\nDisallow: /private\nCrawl-delay: 3\n",
        ),
        user_agent=USER_AGENT,
    )

    assert decision.allow_all is False
    assert decision.reason == "robots_parsed"
    assert decision.pause_domain is False
    assert decision.crawl_delay == 3.0
    assert decision.can_fetch(USER_AGENT, "https://example.com/private") is False
    assert decision.can_fetch(USER_AGENT, "https://example.com/public") is True


@pytest.mark.parametrize("status", [404, 410])
def test_missing_robots_file_allows_the_origin(status: int) -> None:
    decision = evaluate_robots_result(
        make_result(status=status, category=OK),
        user_agent=USER_AGENT,
    )

    assert decision.allow_all is True
    assert decision.reason == "robots_missing_allow"
    assert decision.can_fetch(USER_AGENT, "https://example.com/anything") is True


def test_empty_robots_file_allows_the_origin() -> None:
    decision = evaluate_robots_result(
        make_result(status=200, category=OK),
        user_agent=USER_AGENT,
    )

    assert decision.allow_all is True
    assert decision.reason == "robots_empty_allow"


def test_crawl_delay_is_capped_at_sixty_seconds() -> None:
    decision = evaluate_robots_result(
        make_result(status=200, category=OK, body=b"User-agent: *\nCrawl-delay: 600\n"),
        user_agent=USER_AGENT,
    )

    assert decision.crawl_delay == 60.0


def test_user_agent_specific_crawl_delay_is_used_first() -> None:
    text = b"User-agent: *\nCrawl-delay: 2\n\nUser-agent: TifaLockhartBot\nCrawl-delay: 9\n"

    decision = evaluate_robots_result(
        make_result(status=200, category=OK, body=text),
        user_agent=USER_AGENT,
    )

    assert decision.crawl_delay == 9.0


@pytest.mark.parametrize(
    ("status", "category"),
    [
        (401, BLOCKED),
        (403, BLOCKED),
        (500, SERVER_ERROR),
        (None, NETWORK_ERROR),
        (None, TIMEOUT),
    ],
)
def test_unreadable_robots_policy_pauses_the_origin(status: int | None, category: str) -> None:
    decision = evaluate_robots_result(
        make_result(status=status, category=category),
        user_agent=USER_AGENT,
    )

    assert decision.allow_all is False
    assert decision.pause_domain is True
    assert decision.can_fetch(USER_AGENT, "https://example.com/public") is False


def test_rate_limited_robots_response_preserves_retry_after() -> None:
    decision = evaluate_robots_result(
        make_result(status=429, category=RATE_LIMITED, retry_after=120.0),
        user_agent=USER_AGENT,
    )

    assert decision.reason == "robots_rate_limited"
    assert decision.pause_domain is True
    assert decision.retry_after_seconds == 120.0


def test_unexpected_robots_status_is_conservatively_paused() -> None:
    decision = evaluate_robots_result(
        make_result(status=400, category="http_error"),
        user_agent=USER_AGENT,
    )

    assert decision.reason == "robots_unexpected_status:400"
    assert decision.pause_domain is True


async def test_robots_cache_fetches_each_origin_once() -> None:
    calls: list[str] = []

    async def fetch(url: str) -> FetchResult:
        calls.append(url)
        return make_result(status=404, category=OK)

    cache = RobotsCache(user_agent=USER_AGENT)
    first = await cache.decision_for("https://example.com/one", fetch=fetch)
    second = await cache.decision_for("https://example.com/two", fetch=fetch)

    assert first is second
    assert calls == ["https://example.com/robots.txt"]
    assert cache.decision_for_url("https://example.com/three") is first


async def test_robots_cache_separates_origins() -> None:
    calls: list[str] = []

    async def fetch(url: str) -> FetchResult:
        calls.append(url)
        return make_result(status=404, category=OK)

    cache = RobotsCache(user_agent=USER_AGENT)
    await cache.decision_for("https://example.com/page", fetch=fetch)
    await cache.decision_for("https://other.example/page", fetch=fetch)

    assert calls == [
        "https://example.com/robots.txt",
        "https://other.example/robots.txt",
    ]


async def test_robots_cache_treats_fetch_failure_as_unavailable() -> None:
    async def fetch(_url: str) -> FetchResult:
        raise RuntimeError("unexpected transport failure")

    cache = RobotsCache(user_agent=USER_AGENT)
    decision = await cache.decision_for("https://example.com/page", fetch=fetch)

    assert decision.allow_all is False
    assert decision.pause_domain is True
    assert decision.reason == "robots_unavailable"


def test_robots_fetch_size_is_bounded() -> None:
    assert robots_fetch_max_bytes(SimpleNamespace(max_page_bytes=5_000_000)) == 512_000
    assert robots_fetch_max_bytes(SimpleNamespace(max_page_bytes=1000)) == 1000
