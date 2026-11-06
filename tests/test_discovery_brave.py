from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from app.config import Settings
from app.discovery.brave import (
    BraveAccessNotApprovedError,
    BraveBarrierError,
    BraveDiscoveryService,
    BraveParseError,
    BraveRequestError,
    BraveResult,
    BraveSearchAdapter,
    BraveSearchPage,
    BraveSearchParser,
    detect_brave_barrier,
)
from app.discovery.http import OK, FetchResult

RESULT_HTML = """
<!doctype html>
<html>
  <body>
    <main id="results">
      <div class="snippet" data-type="web">
        <h2 class="title"><a href="https://artist.example/tifa">Tifa Lockhart gallery</a></h2>
        <p class="snippet-description">A fan art gallery of Tifa Lockhart.</p>
      </div>
      <div class="snippet">
        <h3><a href="https://search.brave.com/search?q=internal">Brave navigation</a></h3>
        <p>Internal navigation link.</p>
      </div>
      <div class="snippet ad">
        <h2><a href="https://ads.example/promo">Sponsored result</a></h2>
        <p>Advertisement.</p>
      </div>
      <div class="snippet">
        <h2><a href="https://cosplay.example/tifa-lockhart">Tifa Lockhart cosplay</a></h2>
        <p class="description">Cosplay photographs.</p>
      </div>
      <div class="snippet">
        <h2><a href="https://artist.example/tifa#section">Duplicate destination</a></h2>
        <p class="description">Should be deduplicated.</p>
      </div>
      <div class="snippet">
        <h2><a href="javascript:void(0)">Broken result</a></h2>
        <p>No usable destination.</p>
      </div>
      <div class="snippet">
        <h2><a href="mailto:test@example.com">Email result</a></h2>
        <p>No usable destination.</p>
      </div>
      <div class="snippet">
        <h2><a href="https://second.example/page">Second site</a></h2>
        <p class="snippet-content">Another result without a title element.</p>
      </div>
    </main>
  </body>
</html>
"""


def parse_results(html: str = RESULT_HTML) -> tuple[BraveResult, ...]:
    return BraveSearchParser().parse(
        html,
        search_phrase="Tifa Lockhart",
        result_page_url="https://search.brave.com/search?q=Tifa+Lockhart",
    )


def test_parser_extracts_destination_title_and_snippet() -> None:
    results = parse_results()

    assert len(results) == 3
    first = results[0]
    assert first.title == "Tifa Lockhart gallery"
    assert first.snippet == "A fan art gallery of Tifa Lockhart."
    assert first.url == "https://artist.example/tifa"
    assert first.original_url == "https://artist.example/tifa"
    assert first.search_phrase == "Tifa Lockhart"
    assert first.result_page_url == "https://search.brave.com/search?q=Tifa+Lockhart"
    assert first.to_metadata()["discovered_via"] == "brave_html"


def test_parser_rejects_ads_internal_navigation_and_malformed_links() -> None:
    results = parse_results()
    urls = {result.url for result in results}

    assert urls == {
        "https://artist.example/tifa",
        "https://cosplay.example/tifa-lockhart",
        "https://second.example/page",
    }
    assert all("ads.example" not in url for url in urls)
    assert all("search.brave.com" not in url for url in urls)


def test_parser_reports_no_results_without_raising() -> None:
    assert (
        parse_results(
            "<html><body><main id='results'><p>No results found.</p></main></body></html>"
        )
        == ()
    )
    assert parse_results("<html><body><main id='results'></main></body></html>") == ()
    assert parse_results("") == ()


def test_parser_rejects_unrecognized_markup() -> None:
    with pytest.raises(BraveParseError):
        parse_results("<html><body><div>Unrelated page</div></body></html>")


def test_parser_wraps_soup_factory_failures() -> None:
    def failing_factory(_html: str):
        raise RuntimeError("parser exploded")

    parser = BraveSearchParser(soup_factory=failing_factory)

    with pytest.raises(BraveParseError):
        parser.parse(
            "<html></html>",
            search_phrase="Tifa",
            result_page_url="https://search.brave.com/search?q=Tifa",
        )


def test_brave_barrier_detection_distinguishes_restrictions() -> None:
    assert detect_brave_barrier(401, None) == "http_401_access_denied"
    assert detect_brave_barrier(403, None) == "http_403_access_denied"
    assert detect_brave_barrier(429, None) == "http_429_rate_limited"
    assert (
        detect_brave_barrier(200, "<html><body><div class='g-recaptcha'></div></body></html>")
        == "anti_automation_challenge"
    )
    assert (
        detect_brave_barrier(
            200, "<html><title>Access denied</title><body>Brave Search</body></html>"
        )
        == "explicit_access_denial_page"
    )
    assert detect_brave_barrier(200, "<html><body>Tifa Lockhart results</body></html>") is None


def test_search_url_preserves_base_query_and_adds_pagination() -> None:
    settings = Settings.from_env(
        {"BRAVE_SEARCH_BASE_URL": "https://search.brave.com/search?source=web"},
        load_env_file=False,
    )
    adapter = BraveSearchAdapter(settings)

    first = adapter.search_url("Tifa Lockhart")
    second = adapter.search_url("Tifa Lockhart", page=2)

    assert "source=web" in first
    assert "q=Tifa+Lockhart" in first
    assert "offset=20" in second
    assert adapter.access_permitted is False


def test_adapter_requires_both_explicit_access_gates() -> None:
    disabled = BraveSearchAdapter(Settings.from_env({}, load_env_file=False))
    with pytest.raises(BraveAccessNotApprovedError, match="BRAVE_HTML_ENABLED"):
        disabled.ensure_access()

    unapproved = BraveSearchAdapter(
        Settings.from_env({"BRAVE_HTML_ENABLED": "true"}, load_env_file=False)
    )
    with pytest.raises(BraveAccessNotApprovedError, match="ACCESS_APPROVED"):
        unapproved.ensure_access()

    approved = BraveSearchAdapter(
        Settings.from_env(
            {
                "BRAVE_HTML_ENABLED": "true",
                "BRAVE_HTML_ACCESS_APPROVED": "true",
            },
            load_env_file=False,
        )
    )
    approved.ensure_access()
    assert approved.access_permitted is True


def test_adapter_fetches_and_parses_an_overridden_response() -> None:
    settings = Settings.from_env(
        {
            "BRAVE_HTML_ENABLED": "true",
            "BRAVE_HTML_ACCESS_APPROVED": "true",
        },
        load_env_file=False,
    )

    async def request_override(url: str) -> FetchResult:
        assert "q=Tifa+Lockhart" in url
        return FetchResult(
            requested_url=url,
            final_url=url,
            status=200,
            category=OK,
            content_type="text/html",
            body=RESULT_HTML.encode(),
        )

    adapter = BraveSearchAdapter(settings, request_override=request_override)

    async def run() -> BraveSearchPage:
        from app.discovery.http import DomainThrottle, RequestBudget

        return await adapter.fetch_page(
            "Tifa Lockhart",
            page=0,
            budget=RequestBudget(max_domain_requests=5),
            throttle=DomainThrottle(0),
        )

    import asyncio

    page = asyncio.run(run())

    assert page.phrase == "Tifa Lockhart"
    assert len(page.results) == 3


def test_adapter_maps_barriers_requests_and_parse_failures() -> None:
    settings = Settings.from_env(
        {
            "BRAVE_HTML_ENABLED": "true",
            "BRAVE_HTML_ACCESS_APPROVED": "true",
        },
        load_env_file=False,
    )

    def adapter_with(result: FetchResult) -> BraveSearchAdapter:
        async def request_override(_url: str) -> FetchResult:
            return result

        return BraveSearchAdapter(settings, request_override=request_override)

    async def fetch(adapter: BraveSearchAdapter):
        from app.discovery.http import DomainThrottle, RequestBudget

        return await adapter.fetch_page(
            "Tifa",
            page=0,
            budget=RequestBudget(max_domain_requests=5),
            throttle=DomainThrottle(0),
        )

    import asyncio

    with pytest.raises(BraveBarrierError) as blocked:
        asyncio.run(
            fetch(
                adapter_with(
                    FetchResult(
                        requested_url="https://search.brave.com/search",
                        final_url=None,
                        status=403,
                        category="blocked",
                    )
                )
            )
        )
    assert blocked.value.reason == "http_403_access_denied"

    with pytest.raises(BraveBarrierError) as rate_limited:
        asyncio.run(
            fetch(
                adapter_with(
                    FetchResult(
                        requested_url="https://search.brave.com/search",
                        final_url=None,
                        status=429,
                        category="rate_limited",
                    )
                )
            )
        )
    assert rate_limited.value.reason == "http_429_rate_limited"

    with pytest.raises(BraveBarrierError) as challenge:
        asyncio.run(
            fetch(
                adapter_with(
                    FetchResult(
                        requested_url="https://search.brave.com/search",
                        final_url="https://search.brave.com/search",
                        status=200,
                        category=OK,
                        body=b"<html><body><div class='cf-challenge'></div></body></html>",
                    )
                )
            )
        )
    assert challenge.value.reason == "anti_automation_challenge"

    with pytest.raises(BraveRequestError):
        asyncio.run(
            fetch(
                adapter_with(
                    FetchResult(
                        requested_url="https://search.brave.com/search",
                        final_url=None,
                        status=500,
                        category="server_error",
                        error="http_500",
                    )
                )
            )
        )

    with pytest.raises(BraveParseError):
        asyncio.run(
            fetch(
                adapter_with(
                    FetchResult(
                        requested_url="https://search.brave.com/search",
                        final_url="https://search.brave.com/search",
                        status=200,
                        category=OK,
                        body=b"<html><body>unrecognized</body></html>",
                    )
                )
            )
        )


class FakeRepository:
    configured = True

    def __init__(
        self,
        *,
        state: dict[str, Any] | None = None,
        created: bool = True,
    ) -> None:
        self.state = {"collection_enabled": True, **(state or {})}
        self.created = created
        self.jobs: list[tuple[str, dict[str, Any], str]] = []
        self.finished: list[tuple[str, dict[str, Any], str, str | None]] = []
        self.upserts: list[dict[str, Any]] = []
        self.paused: list[str] = []
        self.success_marks = 0

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

    async def upsert_discovery_record(self, payload: dict[str, Any]):
        self.upserts.append(payload)
        return {"id": f"discovery-{len(self.upserts)}", **payload}, self.created

    async def pause_brave(self, reason: str) -> None:
        self.paused.append(reason)

    async def mark_brave_success(self) -> None:
        self.success_marks += 1


@dataclass
class FakeAdapter:
    pages: list[BraveSearchPage | Exception]
    calls: list[tuple[str, int]] | None = None

    def __post_init__(self) -> None:
        if self.calls is None:
            self.calls = []

    async def fetch_page(self, phrase, *, page, budget, throttle):
        self.calls.append((phrase, page))
        value = self.pages.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


def approved_settings(**overrides: object) -> Settings:
    env = {
        "BRAVE_HTML_ENABLED": "true",
        "BRAVE_HTML_ACCESS_APPROVED": "true",
        "BRAVE_SEARCH_PHRASES": "Tifa Lockhart",
        "BRAVE_SEARCH_MAX_PAGES_PER_PHRASE": "1",
    }
    env.update({key: str(value) for key, value in overrides.items()})
    return Settings.from_env(env, load_env_file=False)


def result(url: str, title: str = "Tifa Lockhart") -> BraveResult:
    return BraveResult(
        title=title,
        snippet="snippet",
        url=url,
        original_url=url,
        search_phrase="Tifa Lockhart",
        result_page_url="https://search.brave.com/search?q=Tifa",
    )


def test_service_block_reason_reflects_flags_pause_and_database() -> None:
    settings = approved_settings()
    repository = FakeRepository()
    service = BraveDiscoveryService(settings, repository, FakeAdapter([]))

    assert service.block_reason({}) is None
    assert "paused" in service.block_reason(
        {"brave_paused": True, "brave_pause_reason": "http_403_access_denied"}
    )
    disabled = BraveDiscoveryService(
        approved_settings(BRAVE_HTML_ENABLED="false"), repository, FakeAdapter([])
    )
    assert "BRAVE_HTML_ENABLED" in disabled.block_reason({})
    unapproved = BraveDiscoveryService(
        approved_settings(BRAVE_HTML_ACCESS_APPROVED="false"),
        repository,
        FakeAdapter([]),
    )
    assert "approval" in unapproved.block_reason({})
    repository.configured = False
    assert "Supabase" in service.block_reason({})


async def test_service_skips_when_collection_is_paused() -> None:
    repository = FakeRepository(state={"collection_enabled": False})
    adapter = FakeAdapter([])
    service = BraveDiscoveryService(approved_settings(), repository, adapter)

    counters = await service.run(scheduled=True)

    assert counters["processed_count"] == 0
    assert repository.jobs == []
    assert adapter.calls == []


async def test_service_records_a_skipped_job_for_a_closed_gate() -> None:
    repository = FakeRepository()
    service = BraveDiscoveryService(
        approved_settings(BRAVE_HTML_ENABLED="false"),
        repository,
        FakeAdapter([]),
    )

    counters = await service.run(scheduled=False)

    assert repository.jobs[0][2] == "brave_discovery"
    assert repository.jobs[0][1]["blocked_reason"] == "BRAVE_HTML_ENABLED is false"
    assert repository.finished[0][2] == "skipped"
    assert counters["processed_count"] == 0


async def test_service_persists_unique_results_with_provenance() -> None:
    page = BraveSearchPage(
        phrase="Tifa Lockhart",
        page_url="https://search.brave.com/search?q=Tifa",
        results=(
            result("https://artist.example/tifa"),
            result("https://artist.example/tifa"),
            result("https://cosplay.example/tifa"),
        ),
    )
    repository = FakeRepository()
    adapter = FakeAdapter([page])
    service = BraveDiscoveryService(approved_settings(), repository, adapter)

    counters = await service.run(scheduled=True)

    assert counters["discovered_count"] == 3
    assert counters["examined_count"] == 2
    assert counters["duplicate_count"] == 1
    assert counters["processed_count"] == 2
    assert len(repository.upserts) == 2
    payload = repository.upserts[0]
    assert payload["url"] == "https://artist.example/tifa"
    assert payload["normalized_url"] == "https://artist.example/tifa"
    assert payload["source_type"] == "brave_html"
    assert payload["domain"] == "artist.example"
    assert payload["depth"] == 0
    assert payload["status"] == "queued"
    assert payload["metadata_json"]["search_phrase"] == "Tifa Lockhart"
    assert payload["metadata_json"]["discovered_via"] == "brave_html"
    assert repository.finished[-1][2] == "succeeded"
    assert repository.success_marks == 1


async def test_service_counts_existing_queue_rows_as_duplicates() -> None:
    repository = FakeRepository(created=False)
    adapter = FakeAdapter(
        [
            BraveSearchPage(
                phrase="Tifa Lockhart",
                page_url="https://search.brave.com/search?q=Tifa",
                results=(result("https://artist.example/tifa"),),
            )
        ]
    )
    service = BraveDiscoveryService(approved_settings(), repository, adapter)

    counters = await service.run(scheduled=True)

    assert counters["processed_count"] == 0
    assert counters["duplicate_count"] == 1
    assert repository.finished[-1][2] == "succeeded"


async def test_service_stops_at_the_configured_url_budget() -> None:
    repository = FakeRepository()
    adapter = FakeAdapter(
        [
            BraveSearchPage(
                phrase="Tifa Lockhart",
                page_url="https://search.brave.com/search?q=Tifa",
                results=(
                    result("https://one.example/tifa"),
                    result("https://two.example/tifa"),
                ),
            )
        ]
    )
    service = BraveDiscoveryService(approved_settings(MAX_URLS_PER_JOB="1"), repository, adapter)

    counters = await service.run(scheduled=True)

    assert len(repository.upserts) == 1
    assert counters["processed_count"] == 1
    assert counters["examined_count"] == 2


async def test_service_persistently_pauses_on_a_barrier_without_retrying() -> None:
    repository = FakeRepository()
    adapter = FakeAdapter([BraveBarrierError("http_403_access_denied")])
    service = BraveDiscoveryService(approved_settings(), repository, adapter)

    counters = await service.run(scheduled=True)

    assert repository.paused == ["http_403_access_denied"]
    assert repository.finished[-1][2] == "failed"
    assert repository.finished[-1][3] == "brave_access_restriction:http_403_access_denied"
    assert repository.success_marks == 0
    assert adapter.calls == [("Tifa Lockhart", 0)]
    assert counters["processed_count"] == 0


async def test_service_reports_parser_failures_separately() -> None:
    repository = FakeRepository()
    adapter = FakeAdapter([BraveParseError("markup changed")])
    service = BraveDiscoveryService(approved_settings(), repository, adapter)

    await service.run(scheduled=True)

    assert repository.paused == []
    assert repository.finished[-1][2] == "failed"
    assert repository.finished[-1][3] == "brave_parse_failed:BraveParseError"
    assert adapter.calls == [("Tifa Lockhart", 0)]


async def test_service_handles_a_transport_failure_without_pausing() -> None:
    repository = FakeRepository()
    adapter = FakeAdapter([BraveRequestError("http_500")])
    service = BraveDiscoveryService(approved_settings(), repository, adapter)

    await service.run(scheduled=True)

    assert repository.paused == []
    assert repository.finished[-1][2] == "failed"
    assert repository.finished[-1][3] == "brave_request_failed:BraveRequestError"


async def test_service_skips_a_persisted_pause_even_when_scheduled() -> None:
    repository = FakeRepository(
        state={
            "collection_enabled": True,
            "brave_paused": True,
            "brave_pause_reason": "anti_automation_challenge",
        }
    )
    adapter = FakeAdapter([])
    service = BraveDiscoveryService(approved_settings(), repository, adapter)

    counters = await service.run(scheduled=True)

    assert adapter.calls == []
    assert repository.jobs[0][1]["blocked_reason"].endswith("anti_automation_challenge")
    assert repository.finished[0][2] == "skipped"
    assert counters["processed_count"] == 0
