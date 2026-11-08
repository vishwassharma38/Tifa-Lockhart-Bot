from __future__ import annotations

import asyncio
from typing import Any
from urllib.parse import urlsplit

import pytest

from app.discovery.crawler import (
    DiscoveryCrawler,
    PageInspection,
    PageInspector,
    WebSaveService,
)
from app.discovery.extractor import extract_page
from app.discovery.http import (
    BLOCKED,
    DOMAIN_BUDGET,
    DOMAIN_PAUSED,
    HTTP_ERROR,
    OK,
    RATE_LIMITED,
    SERVER_ERROR,
    DomainThrottle,
    FetchResult,
    RequestBudget,
)
from app.discovery.robots import RobotsCache
from app.pipeline.phase2 import Phase2Result, Phase2Status


def make_result(
    url: str,
    *,
    status: int | None = 200,
    category: str = OK,
    body: bytes = b"",
    final_url: str | None = None,
    retry_after: float | None = None,
    attempts: int = 1,
    error: str | None = None,
) -> FetchResult:
    return FetchResult(
        requested_url=url,
        final_url=final_url if final_url is not None else url,
        status=status,
        category=category,
        body=body,
        retry_after_seconds=retry_after,
        attempts=attempts,
        error=error,
    )


class FakeHttpClient:
    def __init__(
        self,
        responses: dict[str, list[FetchResult] | FetchResult] | None = None,
        *,
        robots: FetchResult | None = None,
    ) -> None:
        self.responses = responses or {}
        self.robots = robots or make_result("https://example.com/robots.txt", status=404, body=b"")
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def fetch(self, url: str, **kwargs: object) -> FetchResult:
        self.calls.append((url, kwargs))
        budget = kwargs.get("budget")
        if budget is not None:
            host = urlsplit(url).hostname or ""
            if budget.pause_reason(host):
                return FetchResult(url, url, None, DOMAIN_PAUSED, error="paused")
            if not budget.try_acquire(host):
                return FetchResult(url, url, None, DOMAIN_BUDGET, error="budget")
        if url.endswith("/robots.txt"):
            return self.robots
        value = self.responses.get(url)
        if value is None:
            raise AssertionError(f"Unexpected fetch for {url}")
        if isinstance(value, list):
            if not value:
                raise AssertionError(f"No remaining responses for {url}")
            return value.pop(0)
        return value

    async def close(self) -> None:
        return None


async def no_sleep(_seconds: float) -> None:
    return None


def make_throttle() -> DomainThrottle:
    return DomainThrottle(0, sleep=no_sleep)


def make_inspector(
    settings,
    client: FakeHttpClient,
    *,
    jitter=None,
    extractor=None,
) -> PageInspector:
    options: dict[str, Any] = {"sleep": no_sleep}
    if jitter is not None:
        options["jitter"] = jitter
    if extractor is not None:
        options["extractor"] = extractor
    return PageInspector(settings, client, **options)


def page_with_html(
    url: str = "https://example.com/page",
    *,
    title: str = "Tifa Lockhart",
    body: str = "A gallery.",
    media: tuple[str, ...] = ("/full.jpg",),
    links: tuple[str, ...] = ("/child",),
) -> str:
    anchors = "".join(f'<a href="{link}">{link}</a>' for link in (*media, *links))
    return (
        f"<html><head><title>{title}</title></head>"
        f"<body><main><p>{body}</p>{anchors}</main></body></html>"
    )


def extracted_page(**kwargs: object):
    return extract_page(page_with_html(**kwargs), "https://example.com/page")


@pytest.mark.parametrize(
    ("category", "status", "body", "expected"),
    [
        ("blocked", 403, b"", "blocked"),
        ("rate_limited", 429, b"", "rate_limited"),
        ("server_error", 500, b"", "failed"),
        ("network_error", None, b"", "failed"),
        ("timeout", None, b"", "failed"),
        ("http_error", 404, b"", "failed"),
        ("oversize", 200, b"", "oversize"),
        ("unsupported_content_type", 200, b"", "rejected"),
    ],
)
def test_inspector_maps_fetch_categories_to_page_outcomes(
    make_settings, category: str, status: int | None, body: bytes, expected: str
) -> None:
    settings = make_settings()
    client = FakeHttpClient(
        {
            "https://example.com/page": make_result(
                "https://example.com/page",
                status=status,
                category=category,
                body=body,
                error=f"error:{category}",
            )
        }
    )
    inspector = make_inspector(settings, client)

    import asyncio

    inspection = asyncio.run(
        inspector.inspect(
            "https://example.com/page",
            budget=RequestBudget(max_domain_requests=20),
            throttle=make_throttle(),
            robots=RobotsCache(user_agent="TifaLockhartBot/1.0"),
        )
    )

    assert inspection.category == expected
    assert inspection.ok is False


async def test_inspector_returns_extracted_metadata_for_html(make_settings) -> None:
    settings = make_settings()
    html = page_with_html()
    client = FakeHttpClient(
        {"https://example.com/page": make_result("https://example.com/page", body=html.encode())}
    )
    inspector = make_inspector(settings, client)

    inspection = await inspector.inspect(
        "https://example.com/page",
        budget=RequestBudget(max_domain_requests=20),
        throttle=make_throttle(),
        robots=RobotsCache(user_agent="TifaLockhartBot/1.0"),
    )

    assert inspection.ok is True
    assert inspection.extracted is not None
    assert inspection.extracted.classification_title == "Tifa Lockhart"
    assert [call[0] for call in client.calls] == [
        "https://example.com/robots.txt",
        "https://example.com/page",
    ]
    assert client.calls[1][1]["max_bytes"] == settings.max_page_bytes
    assert client.calls[1][1]["allowed_content_types"] is not None


async def test_inspector_obeys_robots_disallow_without_fetching_the_page(make_settings) -> None:
    settings = make_settings()
    client = FakeHttpClient(
        robots=make_result(
            "https://example.com/robots.txt",
            status=200,
            body=b"User-agent: *\nDisallow: /private\n",
        )
    )
    inspector = make_inspector(settings, client)

    inspection = await inspector.inspect(
        "https://example.com/private/page",
        budget=RequestBudget(max_domain_requests=20),
        throttle=make_throttle(),
        robots=RobotsCache(user_agent="TifaLockhartBot/1.0"),
    )

    assert inspection.category == "robots_disallowed"
    assert [call[0] for call in client.calls] == ["https://example.com/robots.txt"]


async def test_inspector_pauses_a_domain_when_robots_is_unreadable(make_settings) -> None:
    settings = make_settings()
    client = FakeHttpClient(
        robots=make_result(
            "https://example.com/robots.txt",
            status=403,
            category=BLOCKED,
            body=b"",
            error="http_403",
        )
    )
    budget = RequestBudget(max_domain_requests=20)
    inspector = make_inspector(settings, client)

    inspection = await inspector.inspect(
        "https://example.com/page",
        budget=budget,
        throttle=make_throttle(),
        robots=RobotsCache(user_agent="TifaLockhartBot/1.0"),
    )

    assert inspection.category == "robots_disallowed"
    assert budget.pause_reason("example.com") == "robots_access_denied"
    assert len(client.calls) == 1


async def test_inspector_retries_transient_failures_with_bounded_attempts(
    make_settings,
) -> None:
    settings = make_settings(CRAWL_MAX_ATTEMPTS="3")
    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    client = FakeHttpClient(
        {
            "https://example.com/page": [
                make_result(
                    "https://example.com/page",
                    status=503,
                    category=SERVER_ERROR,
                    error="http_503",
                ),
                make_result(
                    "https://example.com/page",
                    status=502,
                    category=SERVER_ERROR,
                    error="http_502",
                ),
                make_result(
                    "https://example.com/page",
                    status=200,
                    body=page_with_html().encode(),
                ),
            ]
        }
    )
    inspector = PageInspector(
        settings,
        client,
        sleep=sleep,
        jitter=lambda _low, _high: 0.0,
    )

    inspection = await inspector.inspect(
        "https://example.com/page",
        budget=RequestBudget(max_domain_requests=20),
        throttle=make_throttle(),
        robots=RobotsCache(user_agent="TifaLockhartBot/1.0"),
    )

    assert inspection.ok is True
    assert inspection.attempts == 3
    assert len(sleeps) == 2


async def test_inspector_stops_after_the_configured_attempt_limit(make_settings) -> None:
    settings = make_settings(CRAWL_MAX_ATTEMPTS="2")
    client = FakeHttpClient(
        {
            "https://example.com/page": [
                make_result(
                    "https://example.com/page",
                    status=500,
                    category=SERVER_ERROR,
                    error="http_500",
                ),
                make_result(
                    "https://example.com/page",
                    status=500,
                    category=SERVER_ERROR,
                    error="http_500",
                ),
                make_result(
                    "https://example.com/page",
                    status=200,
                    body=page_with_html().encode(),
                ),
            ]
        }
    )
    inspector = make_inspector(settings, client)

    inspection = await inspector.inspect(
        "https://example.com/page",
        budget=RequestBudget(max_domain_requests=20),
        throttle=make_throttle(),
        robots=RobotsCache(user_agent="TifaLockhartBot/1.0"),
    )

    assert inspection.category == "failed"
    assert inspection.attempts == 2
    assert len([call for call in client.calls if not call[0].endswith("robots.txt")]) == 2


async def test_inspector_does_not_retry_permanent_http_errors(make_settings) -> None:
    settings = make_settings(CRAWL_MAX_ATTEMPTS="3")
    client = FakeHttpClient(
        {
            "https://example.com/page": make_result(
                "https://example.com/page",
                status=404,
                category=HTTP_ERROR,
                error="http_404",
            )
        }
    )
    inspector = make_inspector(settings, client)

    inspection = await inspector.inspect(
        "https://example.com/page",
        budget=RequestBudget(max_domain_requests=20),
        throttle=make_throttle(),
        robots=RobotsCache(user_agent="TifaLockhartBot/1.0"),
    )

    assert inspection.category == "failed"
    assert inspection.attempts == 1
    assert len([call for call in client.calls if not call[0].endswith("robots.txt")]) == 1


async def test_inspector_pauses_on_rate_limits_and_preserves_retry_after(make_settings) -> None:
    settings = make_settings()
    budget = RequestBudget(max_domain_requests=20)
    client = FakeHttpClient(
        {
            "https://example.com/page": make_result(
                "https://example.com/page",
                status=429,
                category=RATE_LIMITED,
                retry_after=90.0,
            )
        }
    )
    inspector = make_inspector(settings, client)

    inspection = await inspector.inspect(
        "https://example.com/page",
        budget=budget,
        throttle=make_throttle(),
        robots=RobotsCache(user_agent="TifaLockhartBot/1.0"),
    )

    assert inspection.category == "rate_limited"
    assert inspection.retry_after_seconds == 90.0
    assert budget.pause_reason("example.com") == "http_429_rate_limited"


async def test_inspector_pauses_on_a_page_level_access_denial(make_settings) -> None:
    settings = make_settings()
    budget = RequestBudget(max_domain_requests=20)
    client = FakeHttpClient(
        {
            "https://example.com/page": make_result(
                "https://example.com/page",
                status=403,
                category=BLOCKED,
                error="http_403",
            )
        }
    )
    inspector = make_inspector(settings, client)

    inspection = await inspector.inspect(
        "https://example.com/page",
        budget=budget,
        throttle=make_throttle(),
        robots=RobotsCache(user_agent="TifaLockhartBot/1.0"),
    )

    assert inspection.category == "blocked"
    assert budget.pause_reason("example.com") == "http_403"


async def test_inspector_detects_challenges_and_pauses_the_domain(make_settings) -> None:
    settings = make_settings()
    budget = RequestBudget(max_domain_requests=20)
    html = "<html><body><div class='g-recaptcha'></div><p>Verify you are human</p></body></html>"
    client = FakeHttpClient(
        {"https://example.com/page": make_result("https://example.com/page", body=html.encode())}
    )
    inspector = make_inspector(settings, client)

    inspection = await inspector.inspect(
        "https://example.com/page",
        budget=budget,
        throttle=make_throttle(),
        robots=RobotsCache(user_agent="TifaLockhartBot/1.0"),
    )

    assert inspection.category == "challenge"
    assert budget.pause_reason("example.com") == "anti_automation_challenge"


async def test_inspector_reports_empty_and_failed_extraction(make_settings) -> None:
    settings = make_settings()
    empty_client = FakeHttpClient(
        {"https://example.com/page": make_result("https://example.com/page", body=b"   ")}
    )
    empty = await make_inspector(settings, empty_client).inspect(
        "https://example.com/page",
        budget=RequestBudget(max_domain_requests=20),
        throttle=make_throttle(),
        robots=RobotsCache(user_agent="TifaLockhartBot/1.0"),
    )
    assert empty.category == "failed"
    assert empty.reason == "empty_response"

    def failing_extractor(_html: str, _url: str):
        raise RuntimeError("extractor exploded")

    client = FakeHttpClient(
        {
            "https://example.com/page": make_result(
                "https://example.com/page", body=b"<html><body>x</body></html>"
            )
        }
    )
    failed = await make_inspector(settings, client, extractor=failing_extractor).inspect(
        "https://example.com/page",
        budget=RequestBudget(max_domain_requests=20),
        throttle=make_throttle(),
        robots=RobotsCache(user_agent="TifaLockhartBot/1.0"),
    )
    assert failed.category == "extraction_failed"
    assert failed.reason == "extraction_failed:RuntimeError"


async def test_inspector_reports_domain_budget_exhaustion(make_settings) -> None:
    settings = make_settings()
    budget = RequestBudget(max_domain_requests=1)
    client = FakeHttpClient(
        {
            "https://example.com/page": make_result(
                "https://example.com/page", body=page_with_html().encode()
            )
        }
    )

    inspection = await make_inspector(settings, client).inspect(
        "https://example.com/page",
        budget=budget,
        throttle=make_throttle(),
        robots=RobotsCache(user_agent="TifaLockhartBot/1.0"),
    )

    assert inspection.category == "domain_budget"


class FakeRepository:
    configured = True

    def __init__(
        self,
        *,
        rows: list[dict[str, Any]] | None = None,
        state: dict[str, Any] | None = None,
        created: bool | list[bool] = True,
    ) -> None:
        self.rows = rows or []
        self.state = {"collection_enabled": True, **(state or {})}
        self.created = created
        self.created_calls = 0
        self.jobs: list[tuple[str, dict[str, Any], str]] = []
        self.finished: list[tuple[str, dict[str, Any], str, str | None]] = []
        self.updates: list[tuple[str, dict[str, Any]]] = []
        self.metadata_updates: list[tuple[str, dict[str, Any]]] = []
        self.upserts: list[dict[str, Any]] = []
        self.edges: list[dict[str, Any]] = []
        self.requeues: list[tuple[str, str | None, str | None]] = []
        self.claims: list[tuple[int, int]] = []

    async def get_runtime_state(self) -> dict[str, Any]:
        return self.state

    async def create_job_run(
        self, config: dict[str, Any], job_type: str = "reddit_collection"
    ) -> str:
        job_id = f"job-{len(self.jobs) + 1}"
        self.jobs.append((job_id, config, job_type))
        return job_id

    async def finish_job_run(
        self,
        job_id: str,
        counters: dict[str, Any],
        outcome: str,
        error: str | None = None,
    ) -> None:
        self.finished.append((job_id, dict(counters), outcome, error))

    async def claim_discoveries(self, *, limit: int, max_depth: int):
        self.claims.append((limit, max_depth))
        return self.rows

    async def update_discovery(self, discovery_id: str, fields: dict[str, Any]):
        self.updates.append((discovery_id, dict(fields)))
        return {"id": discovery_id, **fields}

    async def update_discovery_metadata(self, discovery_id: str, metadata: dict[str, Any]):
        self.metadata_updates.append((discovery_id, dict(metadata)))
        return {"id": discovery_id}

    async def upsert_discovery_record(self, payload: dict[str, Any]):
        self.upserts.append(payload)
        if isinstance(self.created, list):
            created = self.created[self.created_calls]
        else:
            created = self.created
        self.created_calls += 1
        return {
            "id": payload.get("parent_discovery_id")
            and f"child-{self.created_calls}"
            or f"discovery-{self.created_calls}",
            "status": "queued",
            **payload,
        }, created

    async def record_discovery_edge(self, **kwargs: Any):
        self.edges.append(kwargs)
        return {"id": f"edge-{len(self.edges)}", **kwargs}

    async def requeue_discovery(
        self,
        discovery_id: str,
        *,
        reason: str | None = None,
        next_attempt_at: str | None = None,
    ):
        self.requeues.append((discovery_id, reason, next_attempt_at))
        return {"id": discovery_id, "status": "queued"}


class FakePipeline:
    def __init__(self, results: list[Phase2Result] | None = None, error: Exception | None = None):
        self.results = results or [Phase2Result(Phase2Status.ARCHIVED, "archived")]
        self.error = error
        self.calls: list[tuple[Any, bool]] = []

    async def process_candidate(self, candidate, *, publish: bool = True):
        self.calls.append((candidate, publish))
        if self.error is not None:
            raise self.error
        return self.results


class FakeInspector:
    def __init__(
        self, inspections: list[PageInspection] | PageInspection, error: Exception | None = None
    ):
        self.inspections = inspections if isinstance(inspections, list) else [inspections]
        self.error = error
        self.calls: list[str] = []

    async def inspect(self, url, *, budget, throttle, robots):
        self.calls.append(url)
        if self.error is not None:
            raise self.error
        return self.inspections.pop(0)


def ok_inspection(page, url: str = "https://example.com/page") -> PageInspection:
    return PageInspection(url, page.final_url, 200, OK, None, page, attempts=1)


def row(
    *,
    discovery_id: str = "discovery-1",
    url: str = "https://example.com/page",
    depth: int = 0,
    status: str = "queued",
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "id": discovery_id,
        "url": url,
        "normalized_url": url,
        "depth": depth,
        "status": status,
        "retry_count": 0,
        "source_type": "brave_html",
        "metadata_json": metadata or {},
    }


def crawler(
    make_settings,
    repository: FakeRepository,
    pipeline: FakePipeline,
    inspector: FakeInspector,
    **settings_overrides: object,
):
    settings = make_settings(**settings_overrides)
    return DiscoveryCrawler(
        settings,
        repository,
        pipeline,
        client=FakeHttpClient(),
        inspector=inspector,
    )


async def test_crawler_verifies_acquires_and_admits_bounded_children(make_settings) -> None:
    page = extracted_page(
        media=("/full.jpg",),
        links=("/child", "https://other.example/page"),
    )
    repository = FakeRepository(rows=[row(metadata={"search_phrase": "Tifa Lockhart"})])
    pipeline = FakePipeline()
    inspector = FakeInspector(ok_inspection(page))
    crawler_ = crawler(
        make_settings,
        repository,
        pipeline,
        inspector,
        MAX_URLS_PER_JOB="2",
        MAX_CRAWL_DEPTH="2",
    )

    counters = await crawler_.run_once(scheduled=True)

    assert counters["discovered_count"] == 2
    assert counters["processed_count"] == 1
    assert counters["stored_count"] == 1
    assert len(pipeline.calls) == 1
    candidate, publish = pipeline.calls[0]
    assert publish is False
    assert candidate.discovery_id == "discovery-1"
    assert candidate.source_type == "brave_html"
    assert [asset.url for asset in candidate.media_assets] == ["https://example.com/full.jpg"]
    verified_update = repository.updates[0][1]
    assert verified_update["status"] == "verified"
    assert verified_update["next_attempt_at"] is None
    assert verified_update["metadata_json"]["classification"]["status"] == "verified"
    assert len(repository.upserts) == 2
    first_child = repository.upserts[0]
    assert first_child["parent_discovery_id"] == "discovery-1"
    assert first_child["depth"] == 1
    assert first_child["metadata_json"]["discovered_via"] == "crawl_link"
    assert first_child["metadata_json"]["search_phrase"] == "Tifa Lockhart"
    assert all(edge["parent_discovery_id"] == "discovery-1" for edge in repository.edges)
    assert repository.finished[-1][2] == "succeeded"


async def test_reddit_seed_root_is_crawled_and_children_keep_reddit_provenance(
    make_settings,
) -> None:
    page = extracted_page(media=("/full.jpg",), links=("https://other.example/page",))
    root = row(metadata={"reddit_submission_id": "reddit-post"})
    root["source_type"] = "reddit"
    repository = FakeRepository(rows=[root])
    pipeline = FakePipeline()
    crawler_ = crawler(
        make_settings,
        repository,
        pipeline,
        FakeInspector(ok_inspection(page)),
    )

    await crawler_.run_once(scheduled=True)

    candidate, publish = pipeline.calls[0]
    assert publish is False
    assert candidate.source_type == "reddit"
    assert repository.upserts[0]["source_type"] == "reddit"
    assert repository.upserts[0]["metadata_json"]["origin_source_type"] == "reddit"


async def test_crawler_stages_verified_pages_without_original_media(make_settings) -> None:
    page = extracted_page(media=(), links=("/child",), body="Tifa Lockhart text")
    repository = FakeRepository(rows=[row()])
    pipeline = FakePipeline()
    crawler_ = crawler(make_settings, repository, pipeline, FakeInspector(ok_inspection(page)))

    counters = await crawler_.run_once(scheduled=True)

    update = repository.updates[0][1]
    assert update["status"] == "dumpster"
    assert update["metadata_json"]["review_reason"] == "original_media_not_found"
    assert pipeline.calls == []
    assert counters["stored_count"] == 0
    assert counters["skipped_count"] == 1
    assert repository.upserts[0]["depth"] == 1


async def test_crawler_stages_preview_only_pages(make_settings) -> None:
    page = extract_page(
        "<html><head><title>Tifa Lockhart</title></head><body><img src='/thumb.jpg'></body></html>",
        "https://example.com/page",
    )
    repository = FakeRepository(rows=[row()])
    pipeline = FakePipeline()
    crawler_ = crawler(make_settings, repository, pipeline, FakeInspector(ok_inspection(page)))

    await crawler_.run_once(scheduled=True)

    assert repository.updates[0][1]["status"] == "dumpster"
    assert (
        repository.updates[0][1]["metadata_json"]["review_reason"]
        == "preview_only_original_unavailable"
    )
    assert pipeline.calls == []


async def test_crawler_crawls_children_from_dumpster_but_not_rejected_pages(
    make_settings,
) -> None:
    dumpster_page = extracted_page(title="Tifa", media=(), links=("/child",))
    repository = FakeRepository(rows=[row()])
    crawler_ = crawler(
        make_settings, repository, FakePipeline(), FakeInspector(ok_inspection(dumpster_page))
    )

    await crawler_.run_once(scheduled=True)

    assert repository.updates[0][1]["status"] == "dumpster"
    assert len(repository.upserts) == 1

    rejected_page = extract_page(
        "<html><head><title>Cloud Strife</title></head><body>"
        "<p>Aerith gallery</p><a href='/child'>child</a></body></html>",
        "https://example.com/page",
    )
    rejected_repository = FakeRepository(rows=[row()])
    rejected_crawler = crawler(
        make_settings,
        rejected_repository,
        FakePipeline(),
        FakeInspector(ok_inspection(rejected_page)),
    )

    await rejected_crawler.run_once(scheduled=True)

    assert rejected_repository.updates[0][1]["status"] == "rejected"
    assert rejected_repository.upserts == []


async def test_crawler_respects_max_depth_and_does_not_crawl_children(make_settings) -> None:
    page = extracted_page(media=("/full.jpg",), links=("/child",))
    repository = FakeRepository(rows=[row(depth=6)])
    crawler_ = crawler(
        make_settings,
        repository,
        FakePipeline(),
        FakeInspector(ok_inspection(page)),
        MAX_CRAWL_DEPTH="6",
    )

    await crawler_.run_once(scheduled=True)

    assert repository.upserts == []
    assert repository.edges == []


async def test_crawler_fails_rows_beyond_the_configured_depth(make_settings) -> None:
    repository = FakeRepository(rows=[row(depth=7)])
    inspector = FakeInspector([])
    crawler_ = crawler(
        make_settings,
        repository,
        FakePipeline(),
        inspector,
        MAX_CRAWL_DEPTH="6",
    )

    counters = await crawler_.run_once(scheduled=True)

    assert inspector.calls == []
    assert repository.updates[0][1]["status"] == "failed"
    assert repository.updates[0][1]["last_error"] == "depth_exceeds_limit"
    assert counters["failed_count"] == 1


@pytest.mark.parametrize(
    ("inspection", "expected_status", "expected_error"),
    [
        (
            PageInspection(
                "https://example.com/page", None, 403, "blocked", "http_403", attempts=1
            ),
            "blocked",
            "http_403",
        ),
        (
            PageInspection(
                "https://example.com/page", None, 403, "challenge", "anti_automation_challenge"
            ),
            "blocked",
            "anti_automation_challenge",
        ),
        (
            PageInspection(
                "https://example.com/page", None, 200, "oversize", "content_length_exceeds_limit"
            ),
            "oversize",
            "content_length_exceeds_limit",
        ),
        (
            PageInspection(
                "https://example.com/page",
                None,
                400,
                "rejected",
                "unsupported_content_type:text/plain",
            ),
            "rejected",
            "unsupported_content_type:text/plain",
        ),
    ],
)
async def test_crawler_records_terminal_page_failures(
    make_settings, inspection: PageInspection, expected_status: str, expected_error: str
) -> None:
    repository = FakeRepository(rows=[row()])
    crawler_ = crawler(make_settings, repository, FakePipeline(), FakeInspector([inspection]))

    await crawler_.run_once(scheduled=True)

    update = repository.updates[0][1]
    assert update["status"] == expected_status
    assert update["last_error"] == expected_error
    assert update["next_attempt_at"] is None


async def test_crawler_requeues_rate_limited_pages_with_a_retry_time(make_settings) -> None:
    inspection = PageInspection(
        "https://example.com/page",
        None,
        429,
        "rate_limited",
        "http_429_rate_limited",
        retry_after_seconds=120.0,
        attempts=1,
    )
    repository = FakeRepository(rows=[row()])
    crawler_ = crawler(make_settings, repository, FakePipeline(), FakeInspector([inspection]))

    counters = await crawler_.run_once(scheduled=True)

    update = repository.updates[0][1]
    assert update["status"] == "queued"
    assert update["retry_count"] == 1
    assert update["next_attempt_at"]
    assert update["metadata_json"]["retry_not_before"] == update["next_attempt_at"]
    assert counters["failed_count"] == 1


async def test_crawler_requeues_domain_pauses_without_marking_failure(make_settings) -> None:
    inspection = PageInspection(
        "https://example.com/page",
        None,
        None,
        DOMAIN_PAUSED,
        "http_403",
    )
    repository = FakeRepository(rows=[row()])
    crawler_ = crawler(make_settings, repository, FakePipeline(), FakeInspector([inspection]))

    counters = await crawler_.run_once(scheduled=True)

    assert repository.requeues == [("discovery-1", "http_403", None)]
    assert counters["skipped_count"] == 1


async def test_crawler_records_retry_attempts_for_exhausted_failures(make_settings) -> None:
    inspection = PageInspection(
        "https://example.com/page", None, 503, "failed", "http_503", attempts=3
    )
    repository = FakeRepository(rows=[row()])
    crawler_ = crawler(make_settings, repository, FakePipeline(), FakeInspector([inspection]))

    await crawler_.run_once(scheduled=True)

    update = repository.updates[0][1]
    assert update["status"] == "failed"
    assert update["retry_count"] == 3


async def test_crawler_stages_extraction_failures_in_the_dumpster(make_settings) -> None:
    inspection = PageInspection(
        "https://example.com/page",
        None,
        200,
        "extraction_failed",
        "extraction_failed:ValueError",
        attempts=1,
    )
    repository = FakeRepository(rows=[row()])
    crawler_ = crawler(make_settings, repository, FakePipeline(), FakeInspector([inspection]))

    counters = await crawler_.run_once(scheduled=True)

    update = repository.updates[0][1]
    assert update["status"] == "dumpster"
    assert update["metadata_json"]["review_reason"] == "extraction_failed:ValueError"
    assert counters["processed_count"] == 1
    assert counters["skipped_count"] == 1


async def test_crawler_requeues_rows_interrupted_by_shutdown(make_settings) -> None:
    repository = FakeRepository(rows=[row()])
    crawler_ = crawler(
        make_settings,
        repository,
        FakePipeline(),
        FakeInspector([], error=asyncio.CancelledError()),
    )

    with pytest.raises(asyncio.CancelledError):
        await crawler_.run_once(scheduled=True)

    assert repository.requeues == [("discovery-1", "interrupted_by_shutdown", None)]
    assert repository.finished == []


async def test_crawler_marks_unexpected_processing_errors_as_failed(make_settings) -> None:
    repository = FakeRepository(rows=[row()])
    crawler_ = crawler(
        make_settings,
        repository,
        FakePipeline(),
        FakeInspector([], error=RuntimeError("boom")),
    )

    counters = await crawler_.run_once(scheduled=True)

    assert repository.updates[-1][1]["status"] == "failed"
    assert repository.updates[-1][1]["last_error"] == "processing_failed:RuntimeError"
    assert counters["failed_count"] == 1
    assert repository.finished[-1][2] == "failed"


async def test_crawler_does_not_run_when_collection_is_paused(make_settings) -> None:
    repository = FakeRepository(rows=[row()], state={"collection_enabled": False})
    crawler_ = crawler(make_settings, repository, FakePipeline(), FakeInspector([]))

    counters = await crawler_.run_once(scheduled=True)

    assert counters["processed_count"] == 0
    assert repository.jobs == []
    assert repository.claims == []


async def test_crawler_requeues_remaining_rows_if_collection_is_paused_mid_job(
    make_settings,
) -> None:
    repository = FakeRepository(rows=[row(discovery_id="one"), row(discovery_id="two")])
    repository.state["collection_enabled"] = True
    original_get = repository.get_runtime_state
    calls = {"count": 0}

    async def state_sequence():
        calls["count"] += 1
        repository.state["collection_enabled"] = calls["count"] < 2
        return await original_get()

    repository.get_runtime_state = state_sequence  # type: ignore[method-assign]
    inspector = FakeInspector([])
    crawler_ = crawler(make_settings, repository, FakePipeline(), inspector)

    counters = await crawler_.run_once(scheduled=True)

    assert inspector.calls == []
    assert repository.requeues == [
        ("one", "collection_paused", None),
        ("two", "collection_paused", None),
    ]
    assert counters["skipped_count"] == 2


async def test_crawler_aggregates_acquisition_results(make_settings) -> None:
    page = extracted_page(media=("/full.jpg",), links=())
    repository = FakeRepository(rows=[row()])
    pipeline = FakePipeline(
        [
            Phase2Result(Phase2Status.ARCHIVED, "archived"),
            Phase2Result(Phase2Status.DUPLICATE, "duplicate"),
            Phase2Result(Phase2Status.FAILED, "download_failed"),
            Phase2Result(Phase2Status.SKIPPED, "unsupported"),
        ]
    )
    crawler_ = crawler(make_settings, repository, pipeline, FakeInspector(ok_inspection(page)))

    counters = await crawler_.run_once(scheduled=True)

    assert counters["stored_count"] == 1
    assert counters["duplicate_count"] == 1
    assert counters["failed_count"] == 1
    assert counters["skipped_count"] == 1
    summary = repository.metadata_updates[0][1]["acquisition"]
    assert summary["asset_count"] == 1
    assert len(summary["results"]) == 4


async def test_crawler_records_pipeline_exceptions_without_losing_the_page(make_settings) -> None:
    page = extracted_page(media=("/full.jpg",), links=())
    repository = FakeRepository(rows=[row()])
    crawler_ = crawler(
        make_settings,
        repository,
        FakePipeline(error=RuntimeError("pipeline exploded")),
        FakeInspector(ok_inspection(page)),
    )

    counters = await crawler_.run_once(scheduled=True)

    assert repository.updates[0][1]["status"] == "verified"
    summary = repository.metadata_updates[0][1]["acquisition"]
    assert summary["error"] == "acquisition_failed:RuntimeError"
    assert counters["failed_count"] == 1
    assert repository.finished[-1][2] == "partial"


async def test_crawler_stops_admitting_children_at_the_url_budget(make_settings) -> None:
    page = extracted_page(
        media=("/full.jpg",),
        links=("/one", "/two", "/three"),
    )
    repository = FakeRepository(rows=[row()])
    crawler_ = crawler(
        make_settings,
        repository,
        FakePipeline(),
        FakeInspector(ok_inspection(page)),
        MAX_URLS_PER_JOB="1",
    )

    await crawler_.run_once(scheduled=True)

    assert len(repository.upserts) == 1
    assert repository.upserts[0]["normalized_url"] == "https://example.com/one"


async def test_crawler_records_an_edge_even_when_the_child_already_exists(make_settings) -> None:
    page = extracted_page(media=("/full.jpg",), links=("/existing",))
    repository = FakeRepository(rows=[row()], created=False)
    crawler_ = crawler(
        make_settings, repository, FakePipeline(), FakeInspector(ok_inspection(page))
    )

    counters = await crawler_.run_once(scheduled=True)

    assert counters["discovered_count"] == 0
    assert len(repository.edges) == 1
    assert repository.edges[0]["child_normalized_url"] == "https://example.com/existing"


class FakeValidator:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[str] = []

    async def __call__(self, url: str):
        self.calls.append(url)
        if self.error is not None:
            raise self.error
        return object()


def manual_service(
    make_settings,
    repository: FakeRepository,
    pipeline: FakePipeline,
    inspector: FakeInspector,
    *,
    validator: FakeValidator | None = None,
):
    settings = make_settings()
    return WebSaveService(
        settings,
        repository,
        pipeline,
        client=FakeHttpClient(),
        inspector=inspector,
        validator=validator or FakeValidator(),
    )


async def test_manual_save_rejects_an_invalid_url(make_settings) -> None:
    repository = FakeRepository()
    pipeline = FakePipeline()
    service = manual_service(make_settings, repository, pipeline, FakeInspector([]))

    result = await service.save("http://127.0.0.1/private")

    assert result.status == Phase2Status.FAILED
    assert result.reason.startswith("invalid_url:")
    assert pipeline.calls == []


async def test_manual_save_publishes_direct_media_immediately(make_settings) -> None:
    repository = FakeRepository()
    pipeline = FakePipeline([Phase2Result(Phase2Status.PUBLISHED, "published")])
    validator = FakeValidator()
    service = manual_service(
        make_settings,
        repository,
        pipeline,
        FakeInspector([]),
        validator=validator,
    )

    result = await service.save("https://cdn.example/tifa.jpg")

    assert result.status == Phase2Status.PUBLISHED
    assert validator.calls == ["https://cdn.example/tifa.jpg"]
    assert repository.upserts[0]["status"] == "verified"
    assert repository.upserts[0]["source_type"] == "manual_save"
    assert repository.upserts[0]["metadata_json"]["discovered_via"] == "manual_save"
    candidate, publish = pipeline.calls[0]
    assert publish is True
    assert candidate.media_assets[0].url == "https://cdn.example/tifa.jpg"
    assert candidate.discovery_id is not None


async def test_manual_save_rejects_a_direct_media_url_that_fails_validation(
    make_settings,
) -> None:
    repository = FakeRepository()
    pipeline = FakePipeline()
    from app.discovery.url_safety import UrlSafetyError

    service = manual_service(
        make_settings,
        repository,
        pipeline,
        FakeInspector([]),
        validator=FakeValidator(UrlSafetyError("blocked_destination")),
    )

    result = await service.save("https://cdn.example/tifa.jpg")

    assert result.status == Phase2Status.FAILED
    assert result.reason == "invalid_url:blocked_destination"
    assert pipeline.calls == []


async def test_manual_save_inspects_and_publishes_original_page_media(make_settings) -> None:
    page = extracted_page(media=("/full.jpg",), links=())
    repository = FakeRepository()
    pipeline = FakePipeline([Phase2Result(Phase2Status.PUBLISHED, "published")])
    service = manual_service(
        make_settings,
        repository,
        pipeline,
        FakeInspector(ok_inspection(page)),
    )

    result = await service.save("https://example.com/page")

    assert result.status == Phase2Status.PUBLISHED
    assert repository.upserts[0]["status"] == "verified"
    assert repository.updates == []
    candidate, publish = pipeline.calls[0]
    assert publish is True
    assert candidate.source_type == "manual_save"
    assert [asset.url for asset in candidate.media_assets] == ["https://example.com/full.jpg"]


async def test_manual_save_stages_pages_without_original_media(make_settings) -> None:
    page = extracted_page(media=(), links=())
    repository = FakeRepository()
    pipeline = FakePipeline()
    service = manual_service(
        make_settings, repository, pipeline, FakeInspector(ok_inspection(page))
    )

    result = await service.save("https://example.com/page")

    assert result.status == Phase2Status.SKIPPED
    assert result.reason == "original_media_not_found"
    assert repository.upserts[0]["status"] == "dumpster"
    assert pipeline.calls == []


async def test_manual_save_reports_inspection_failures_as_skipped(make_settings) -> None:
    inspection = PageInspection("https://example.com/page", None, 403, "blocked", "http_403")
    repository = FakeRepository()
    pipeline = FakePipeline()
    service = manual_service(
        make_settings,
        repository,
        pipeline,
        FakeInspector([inspection]),
    )

    result = await service.save("https://example.com/page")

    assert result.status == Phase2Status.SKIPPED
    assert result.reason == "http_403"
    assert repository.upserts == []


async def test_manual_save_reports_discovery_write_failures(make_settings) -> None:
    class FailingRepository(FakeRepository):
        async def upsert_discovery_record(self, payload):
            raise RuntimeError("database unavailable")

    repository = FailingRepository()
    pipeline = FakePipeline()
    service = manual_service(make_settings, repository, pipeline, FakeInspector([]))

    result = await service.save("https://cdn.example/tifa.jpg")

    assert result.status == Phase2Status.FAILED
    assert result.reason == "discovery_write_failed"
    assert pipeline.calls == []


async def test_manual_save_updates_an_existing_discovery_status(make_settings) -> None:
    class ExistingRepository(FakeRepository):
        async def upsert_discovery_record(self, payload):
            self.upserts.append(payload)
            return {**payload, "id": "existing", "status": "queued"}, False

    repository = ExistingRepository()
    service = manual_service(
        make_settings,
        repository,
        FakePipeline([Phase2Result(Phase2Status.PUBLISHED, "published")]),
        FakeInspector([]),
    )

    result = await service.save("https://cdn.example/tifa.jpg")

    assert result.status == Phase2Status.PUBLISHED
    assert repository.updates[0][0] == "existing"
    assert repository.updates[0][1]["status"] == "verified"


async def test_manual_save_reports_pipeline_failures(make_settings) -> None:
    service = manual_service(
        make_settings,
        FakeRepository(),
        FakePipeline(error=RuntimeError("pipeline exploded")),
        FakeInspector([]),
    )

    result = await service.save("https://cdn.example/tifa.jpg")

    assert result.status == Phase2Status.FAILED
    assert result.reason == "manual_save_failed"


async def test_manual_save_aggregates_multiple_asset_results(make_settings) -> None:
    repository = FakeRepository()
    service = manual_service(
        make_settings,
        repository,
        FakePipeline(
            [
                Phase2Result(Phase2Status.PUBLISHED, "published"),
                Phase2Result(Phase2Status.ARCHIVED, "archived"),
            ]
        ),
        FakeInspector([]),
    )

    result = await service.save("https://cdn.example/tifa.jpg")

    assert result.status == Phase2Status.PUBLISHED
    assert result.reason == "published_multiple_assets"
    assert len(result.details["asset_results"]) == 2


async def test_manual_save_prioritises_recoverable_and_failed_aggregates(make_settings) -> None:
    service = manual_service(
        make_settings,
        FakeRepository(),
        FakePipeline(
            [
                Phase2Result(Phase2Status.PUBLISHED, "published"),
                Phase2Result(Phase2Status.RECOVERABLE, "recoverable"),
            ]
        ),
        FakeInspector([]),
    )
    result = await service.save("https://cdn.example/tifa.jpg")
    assert result.status == Phase2Status.RECOVERABLE
    assert result.reason == "partial_failure_recoverable"

    service = manual_service(
        make_settings,
        FakeRepository(),
        FakePipeline(
            [
                Phase2Result(Phase2Status.PUBLISHED, "published"),
                Phase2Result(Phase2Status.FAILED, "failed"),
            ]
        ),
        FakeInspector([]),
    )
    result = await service.save("https://cdn.example/tifa.jpg")
    assert result.status == Phase2Status.FAILED
    assert result.reason == "partial_failure"


async def test_manual_save_handles_missing_extraction(make_settings) -> None:
    inspection = PageInspection(
        "https://example.com/page", "https://example.com/page", 200, OK, None, None
    )
    service = manual_service(
        make_settings,
        FakeRepository(),
        FakePipeline(),
        FakeInspector([inspection]),
    )

    result = await service.save("https://example.com/page")

    assert result.status == Phase2Status.SKIPPED
    assert result.reason == "page_extraction_failed"
