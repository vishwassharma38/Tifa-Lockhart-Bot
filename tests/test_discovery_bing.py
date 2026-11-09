from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from app.config import Settings
from app.discovery.bing import (
    BingAccessNotApprovedError,
    BingBarrierError,
    BingDiscoveryService,
    BingParseError,
    BingRequestError,
    BingResult,
    BingSearchAdapter,
    BingSearchPage,
    BingSearchParser,
    detect_bing_barrier,
)
from app.discovery.http import OK, DomainThrottle, FetchResult, RequestBudget


def click_url(target: str) -> str:
    encoded = base64.urlsafe_b64encode(target.encode("utf-8")).decode("ascii").rstrip("=")
    return f"https://www.bing.com/ck/a?!&&p=tracking&amp;u=a1{encoded}&amp;ntb=1"


RESULT_HTML = f"""
<!doctype html>
<html>
  <body>
    <ol id="b_results">
      <li class="b_algo">
        <h2><a href="https://artist.example/tifa">Tifa Lockhart gallery</a></h2>
        <div class="b_caption"><p>A fan art gallery of Tifa Lockhart.</p></div>
      </li>
      <li class="b_algo">
        <h2><a href="https://www.bing.com/search?q=internal">Bing navigation</a></h2>
        <div class="b_caption"><p>Internal navigation link.</p></div>
      </li>
      <li class="b_algo b_ad">
        <h2><a href="https://ads.example/promo">Sponsored result</a></h2>
        <div class="b_caption"><p>Advertisement.</p></div>
      </li>
      <li class="b_algo" data-type="ad">
        <h2><a href="https://ads.example/second">Another advertisement</a></h2>
        <div class="b_caption"><p>Advertisement.</p></div>
      </li>
      <li class="b_algo">
        <h2><a href="{click_url("https://cosplay.example/tifa-lockhart")}">Tifa Lockhart cosplay</a></h2>
        <div class="b_caption"><p>Cosplay photographs.</p></div>
      </li>
      <li class="b_algo">
        <h2><a href="https://artist.example/tifa#section">Duplicate destination</a></h2>
        <div class="b_caption"><p>Should be deduplicated.</p></div>
      </li>
      <li class="b_algo">
        <h2><a href="javascript:void(0)">Broken result</a></h2>
        <p>No usable destination.</p>
      </li>
      <li class="b_algo">
        <h2><a href="https://www.bing.com/ck/a?u=not-decodable">Undecodable tracking result</a></h2>
        <p>No reliably recoverable destination.</p>
      </li>
      <li class="b_algo">
        <h2><a href="https://second.example/page">Second site</a></h2>
        <div class="b_snippet">Another result without a caption paragraph.</div>
      </li>
    </ol>
  </body>
</html>
"""


def parse_results(html: str = RESULT_HTML) -> tuple[BingResult, ...]:
    return BingSearchParser().parse(
        html,
        search_phrase="Tifa Lockhart",
        result_page_url="https://www.bing.com/search?q=Tifa+Lockhart",
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
    assert first.result_page_url == "https://www.bing.com/search?q=Tifa+Lockhart"
    assert first.to_metadata()["discovered_via"] == "bing_html"


def test_parser_decodes_bing_click_tracking_to_the_real_destination() -> None:
    results = parse_results()

    cosplay = next(result for result in results if "cosplay.example" in result.url)
    assert cosplay.url == "https://cosplay.example/tifa-lockhart"
    assert cosplay.original_url == "https://cosplay.example/tifa-lockhart"
    assert "bing.com" not in cosplay.original_url


def test_parser_rejects_ads_internal_navigation_tracking_and_malformed_links() -> None:
    results = parse_results()
    urls = {result.url for result in results}

    assert urls == {
        "https://artist.example/tifa",
        "https://cosplay.example/tifa-lockhart",
        "https://second.example/page",
    }
    assert all("ads.example" not in url for url in urls)
    assert all("bing.com" not in url for url in urls)
    assert all("not-decodable" not in url for url in urls)


def test_parser_reports_no_results_without_raising() -> None:
    assert (
        parse_results(
            "<html><body><div id='b_results'><p>There are no results for Tifa.</p></div>"
            "</body></html>"
        )
        == ()
    )
    assert parse_results("<html><body><ol id='b_results'></ol></body></html>") == ()
    assert parse_results("") == ()


def test_parser_rejects_unrecognized_markup() -> None:
    with pytest.raises(BingParseError):
        parse_results("<html><body><div>Unrelated page</div></body></html>")


def test_parser_wraps_soup_factory_failures() -> None:
    def failing_factory(_html: str):
        raise RuntimeError("parser exploded")

    parser = BingSearchParser(soup_factory=failing_factory)

    with pytest.raises(BingParseError):
        parser.parse(
            "<html></html>",
            search_phrase="Tifa",
            result_page_url="https://www.bing.com/search?q=Tifa",
        )


def test_bing_barrier_detection_distinguishes_restrictions() -> None:
    assert detect_bing_barrier(401, None) == "http_401_access_denied"
    assert detect_bing_barrier(403, None) == "http_403_access_denied"
    assert detect_bing_barrier(429, None) == "http_429_rate_limited"
    assert (
        detect_bing_barrier(200, "<html><body><div class='g-recaptcha'></div></body></html>")
        == "anti_automation_challenge"
    )
    assert (
        detect_bing_barrier(
            200, "<html><title>Access denied</title><body>Bing Search</body></html>"
        )
        == "explicit_access_denial_page"
    )
    assert detect_bing_barrier(200, "<html><body>Tifa Lockhart results</body></html>") is None


def test_search_url_preserves_base_query_and_uses_first_pagination() -> None:
    settings = Settings.from_env(
        {
            "BING_SEARCH_BASE_URL": (
                "https://www.bing.com/search?source=web&q=stale&first=99&form=QBLH"
            )
        },
        load_env_file=False,
    )
    adapter = BingSearchAdapter(settings)

    first = adapter.search_url("Tifa Lockhart")
    second = adapter.search_url("Tifa Lockhart", page=2)

    first_query = parse_qs(urlsplit(first).query)
    second_query = parse_qs(urlsplit(second).query)

    assert first_query["source"] == ["web"]
    assert first_query["form"] == ["QBLH"]
    assert first_query["q"] == ["Tifa Lockhart"]
    assert "first" not in first_query
    assert second_query["q"] == ["Tifa Lockhart"]
    assert second_query["first"] == ["21"]
    assert parse_qs(urlsplit(first).query).get("first") is None
    assert adapter.access_permitted is False


def test_search_url_page_one_uses_first_eleven() -> None:
    adapter = BingSearchAdapter(Settings.from_env({}, load_env_file=False))

    page_url = adapter.search_url("Tifa & cosplay", page=1)
    query = parse_qs(urlsplit(page_url).query)

    assert query["q"] == ["Tifa & cosplay"]
    assert query["first"] == ["11"]


def test_adapter_requires_both_explicit_access_gates() -> None:
    disabled = BingSearchAdapter(Settings.from_env({}, load_env_file=False))
    with pytest.raises(BingAccessNotApprovedError, match="BING_HTML_ENABLED"):
        disabled.ensure_access()

    unapproved = BingSearchAdapter(
        Settings.from_env({"BING_HTML_ENABLED": "true"}, load_env_file=False)
    )
    with pytest.raises(BingAccessNotApprovedError, match="ACCESS_APPROVED"):
        unapproved.ensure_access()

    approved = BingSearchAdapter(
        Settings.from_env(
            {
                "BING_HTML_ENABLED": "true",
                "BING_HTML_ACCESS_APPROVED": "true",
            },
            load_env_file=False,
        )
    )
    approved.ensure_access()
    assert approved.access_permitted is True


class RecordingClient:
    def __init__(self, result: FetchResult) -> None:
        self.result = result
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def fetch(self, url: str, **kwargs: Any) -> FetchResult:
        self.calls.append((url, kwargs))
        return self.result


def test_adapter_uses_the_configured_url_and_shared_request_infrastructure() -> None:
    settings = Settings.from_env(
        {
            "BING_HTML_ENABLED": "true",
            "BING_HTML_ACCESS_APPROVED": "true",
            "BING_SEARCH_BASE_URL": "https://www.bing.com/search?source=web",
        },
        load_env_file=False,
    )
    requested = "https://www.bing.com/search?source=web&q=Tifa+Lockhart"
    client = RecordingClient(
        FetchResult(
            requested_url=requested,
            final_url=requested,
            status=200,
            category=OK,
            content_type="text/html",
            body=RESULT_HTML.encode(),
        )
    )
    adapter = BingSearchAdapter(settings, client=client)

    def fetch() -> BingSearchPage:
        return asyncio.run(
            adapter.fetch_page(
                "Tifa Lockhart",
                page=0,
                budget=RequestBudget(max_domain_requests=5),
                throttle=DomainThrottle(0),
            )
        )

    page = fetch()

    assert page.phrase == "Tifa Lockhart"
    assert len(page.results) == 3
    assert client.calls[0][0] == requested
    kwargs = client.calls[0][1]
    assert isinstance(kwargs["budget"], RequestBudget)
    assert isinstance(kwargs["throttle"], DomainThrottle)
    assert kwargs["max_bytes"] == settings.max_page_bytes
    assert "text/html" in kwargs["allowed_content_types"]


def test_adapter_maps_barriers_requests_and_parse_failures() -> None:
    settings = Settings.from_env(
        {
            "BING_HTML_ENABLED": "true",
            "BING_HTML_ACCESS_APPROVED": "true",
        },
        load_env_file=False,
    )

    def adapter_with(result: FetchResult) -> BingSearchAdapter:
        async def request_override(_url: str) -> FetchResult:
            return result

        return BingSearchAdapter(settings, request_override=request_override)

    def fetch(adapter: BingSearchAdapter) -> BingSearchPage:
        return asyncio.run(
            adapter.fetch_page(
                "Tifa",
                page=0,
                budget=RequestBudget(max_domain_requests=5),
                throttle=DomainThrottle(0),
            )
        )

    for status, reason in (
        (401, "http_401_access_denied"),
        (403, "http_403_access_denied"),
        (429, "http_429_rate_limited"),
    ):
        with pytest.raises(BingBarrierError) as blocked:
            fetch(
                adapter_with(
                    FetchResult(
                        requested_url="https://www.bing.com/search",
                        final_url=None,
                        status=status,
                        category="blocked" if status != 429 else "rate_limited",
                    )
                )
            )
        assert blocked.value.reason == reason

    with pytest.raises(BingBarrierError) as challenge:
        fetch(
            adapter_with(
                FetchResult(
                    requested_url="https://www.bing.com/search",
                    final_url="https://www.bing.com/search",
                    status=200,
                    category=OK,
                    body=b"<html><body><div class='cf-challenge'></div></body></html>",
                )
            )
        )
    assert challenge.value.reason == "anti_automation_challenge"

    with pytest.raises(BingBarrierError) as denial:
        fetch(
            adapter_with(
                FetchResult(
                    requested_url="https://www.bing.com/search",
                    final_url="https://www.bing.com/search",
                    status=200,
                    category=OK,
                    body=b"<html><title>Access denied</title><body>Bing</body></html>",
                )
            )
        )
    assert denial.value.reason == "explicit_access_denial_page"

    with pytest.raises(BingRequestError):
        fetch(
            adapter_with(
                FetchResult(
                    requested_url="https://www.bing.com/search",
                    final_url=None,
                    status=500,
                    category="server_error",
                    error="http_500",
                )
            )
        )

    with pytest.raises(BingParseError):
        fetch(
            adapter_with(
                FetchResult(
                    requested_url="https://www.bing.com/search",
                    final_url="https://www.bing.com/search",
                    status=200,
                    category=OK,
                    body=b"<html><body>unrecognized</body></html>",
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

    async def pause_bing(self, reason: str) -> None:
        self.paused.append(reason)

    async def mark_bing_success(self) -> None:
        self.success_marks += 1


@dataclass
class FakeAdapter:
    pages: list[BingSearchPage | Exception]
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
        "BING_HTML_ENABLED": "true",
        "BING_HTML_ACCESS_APPROVED": "true",
        "BING_SEARCH_PHRASES": "Tifa Lockhart",
        "BING_SEARCH_MAX_PAGES_PER_PHRASE": "1",
    }
    env.update({key: str(value) for key, value in overrides.items()})
    return Settings.from_env(env, load_env_file=False)


def result(url: str, title: str = "Tifa Lockhart") -> BingResult:
    return BingResult(
        title=title,
        snippet="snippet",
        url=url,
        original_url=url,
        search_phrase="Tifa Lockhart",
        result_page_url="https://www.bing.com/search?q=Tifa",
    )


def test_service_block_reason_reflects_flags_pause_and_database() -> None:
    settings = approved_settings()
    repository = FakeRepository()
    service = BingDiscoveryService(settings, repository, FakeAdapter([]))

    assert service.block_reason({}) is None
    assert "paused" in service.block_reason(
        {"bing_paused": True, "bing_pause_reason": "http_403_access_denied"}
    )
    disabled = BingDiscoveryService(
        approved_settings(BING_HTML_ENABLED="false"), repository, FakeAdapter([])
    )
    assert "BING_HTML_ENABLED" in disabled.block_reason({})
    unapproved = BingDiscoveryService(
        approved_settings(BING_HTML_ACCESS_APPROVED="false"),
        repository,
        FakeAdapter([]),
    )
    assert "approval" in unapproved.block_reason({})
    repository.configured = False
    assert "Supabase" in service.block_reason({})


async def test_service_skips_when_collection_is_paused() -> None:
    repository = FakeRepository(state={"collection_enabled": False})
    adapter = FakeAdapter([])
    service = BingDiscoveryService(approved_settings(), repository, adapter)

    counters = await service.run(scheduled=True)

    assert counters["processed_count"] == 0
    assert repository.jobs == []
    assert adapter.calls == []


async def test_service_records_a_skipped_job_for_a_closed_gate() -> None:
    repository = FakeRepository()
    service = BingDiscoveryService(
        approved_settings(BING_HTML_ENABLED="false"),
        repository,
        FakeAdapter([]),
    )

    counters = await service.run(scheduled=False)

    assert repository.jobs[0][2] == "bing_discovery"
    assert repository.jobs[0][1]["blocked_reason"] == "BING_HTML_ENABLED is false"
    assert repository.finished[0][2] == "skipped"
    assert counters["processed_count"] == 0


async def test_service_persists_unique_results_with_provenance() -> None:
    page = BingSearchPage(
        phrase="Tifa Lockhart",
        page_url="https://www.bing.com/search?q=Tifa",
        results=(
            result("https://artist.example/tifa"),
            result("https://artist.example/tifa"),
            result("https://cosplay.example/tifa"),
        ),
    )
    repository = FakeRepository()
    adapter = FakeAdapter([page])
    service = BingDiscoveryService(approved_settings(), repository, adapter)

    counters = await service.run(scheduled=True)

    assert counters["discovered_count"] == 3
    assert counters["examined_count"] == 2
    assert counters["duplicate_count"] == 1
    assert counters["processed_count"] == 2
    assert len(repository.upserts) == 2
    payload = repository.upserts[0]
    assert payload["url"] == "https://artist.example/tifa"
    assert payload["normalized_url"] == "https://artist.example/tifa"
    assert payload["source_type"] == "bing_html"
    assert payload["domain"] == "artist.example"
    assert payload["depth"] == 0
    assert payload["status"] == "queued"
    assert payload["metadata_json"]["search_phrase"] == "Tifa Lockhart"
    assert payload["metadata_json"]["discovered_via"] == "bing_html"
    assert payload["metadata_json"]["origin_source_type"] == "bing_html"
    assert repository.finished[-1][2] == "succeeded"
    assert repository.success_marks == 1


async def test_service_counts_existing_queue_rows_as_duplicates() -> None:
    repository = FakeRepository(created=False)
    adapter = FakeAdapter(
        [
            BingSearchPage(
                phrase="Tifa Lockhart",
                page_url="https://www.bing.com/search?q=Tifa",
                results=(result("https://artist.example/tifa"),),
            )
        ]
    )
    service = BingDiscoveryService(approved_settings(), repository, adapter)

    counters = await service.run(scheduled=True)

    assert counters["processed_count"] == 0
    assert counters["duplicate_count"] == 1
    assert repository.finished[-1][2] == "succeeded"
    assert repository.success_marks == 1


async def test_service_stops_at_the_configured_url_budget() -> None:
    repository = FakeRepository()
    adapter = FakeAdapter(
        [
            BingSearchPage(
                phrase="Tifa Lockhart",
                page_url="https://www.bing.com/search?q=Tifa",
                results=(
                    result("https://one.example/tifa"),
                    result("https://two.example/tifa"),
                ),
            )
        ]
    )
    service = BingDiscoveryService(approved_settings(MAX_URLS_PER_JOB="1"), repository, adapter)

    counters = await service.run(scheduled=True)

    assert len(repository.upserts) == 1
    assert counters["processed_count"] == 1
    assert counters["examined_count"] == 2


async def test_service_requests_each_configured_result_page() -> None:
    repository = FakeRepository()
    adapter = FakeAdapter(
        [
            BingSearchPage(
                phrase="Tifa Lockhart",
                page_url="https://www.bing.com/search?q=Tifa",
                results=(result("https://one.example/tifa"),),
            ),
            BingSearchPage(
                phrase="Tifa Lockhart",
                page_url="https://www.bing.com/search?q=Tifa&first=11",
                results=(result("https://two.example/tifa"),),
            ),
        ]
    )
    service = BingDiscoveryService(
        approved_settings(BING_SEARCH_MAX_PAGES_PER_PHRASE="2"),
        repository,
        adapter,
    )

    counters = await service.run(scheduled=True)

    assert adapter.calls == [("Tifa Lockhart", 0), ("Tifa Lockhart", 1)]
    assert counters["processed_count"] == 2


async def test_service_persistently_pauses_on_a_barrier_without_retrying() -> None:
    repository = FakeRepository()
    adapter = FakeAdapter(
        [
            BingBarrierError("http_403_access_denied"),
            BingSearchPage(
                phrase="Tifa Lockhart",
                page_url="https://www.bing.com/search?q=Tifa",
                results=(result("https://artist.example/tifa"),),
            ),
        ]
    )
    service = BingDiscoveryService(approved_settings(), repository, adapter)

    counters = await service.run(scheduled=True)

    assert repository.paused == ["http_403_access_denied"]
    assert repository.finished[-1][2] == "failed"
    assert repository.finished[-1][3] == "bing_access_restriction:http_403_access_denied"
    assert repository.success_marks == 0
    assert adapter.calls == [("Tifa Lockhart", 0)]
    assert counters["processed_count"] == 0


async def test_service_reports_parser_failures_separately() -> None:
    repository = FakeRepository()
    adapter = FakeAdapter([BingParseError("markup changed")])
    service = BingDiscoveryService(approved_settings(), repository, adapter)

    await service.run(scheduled=True)

    assert repository.paused == []
    assert repository.finished[-1][2] == "failed"
    assert repository.finished[-1][3] == "bing_parse_failed:BingParseError"
    assert adapter.calls == [("Tifa Lockhart", 0)]


async def test_service_handles_a_transport_failure_without_pausing() -> None:
    repository = FakeRepository()
    adapter = FakeAdapter([BingRequestError("http_500")])
    service = BingDiscoveryService(approved_settings(), repository, adapter)

    await service.run(scheduled=True)

    assert repository.paused == []
    assert repository.finished[-1][2] == "failed"
    assert repository.finished[-1][3] == "bing_request_failed:BingRequestError"


async def test_service_skips_a_persisted_pause_even_when_scheduled() -> None:
    repository = FakeRepository(
        state={
            "collection_enabled": True,
            "bing_paused": True,
            "bing_pause_reason": "anti_automation_challenge",
        }
    )
    adapter = FakeAdapter([])
    service = BingDiscoveryService(approved_settings(), repository, adapter)

    counters = await service.run(scheduled=True)

    assert adapter.calls == []
    assert repository.jobs[0][1]["blocked_reason"].endswith("anti_automation_challenge")
    assert repository.finished[0][2] == "skipped"
    assert counters["processed_count"] == 0
