from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from app.bot.client import TifaBot
from app.bot.commands import PhaseFourCommands, build_status_embed
from app.config import ConfigurationError, Settings
from app.health.status import DependencyStatus, StatusReport
from app.pipeline.phase2 import Phase2Result, Phase2Status


class FakeResponse:
    def __init__(self) -> None:
        self.deferred: list[dict[str, object]] = []
        self.messages: list[dict[str, object]] = []

    async def defer(self, **kwargs: object) -> None:
        self.deferred.append(kwargs)

    async def send_message(self, *args: object, **kwargs: object) -> None:
        self.messages.append({"args": args, **kwargs})


class FakeFollowup:
    def __init__(self) -> None:
        self.messages: list[dict[str, object]] = []

    async def send(self, *args: object, **kwargs: object) -> None:
        self.messages.append({"args": args, **kwargs})


class FakeInteraction:
    def __init__(self, user_id: int = 1) -> None:
        self.response = FakeResponse()
        self.followup = FakeFollowup()
        self.user = SimpleNamespace(id=user_id)
        self.channel_id = 99


class FakeStatusService:
    def __init__(self, report: StatusReport) -> None:
        self.report = report

    async def collect(self, **_kwargs: object) -> StatusReport:
        return self.report


def status_report() -> StatusReport:
    return StatusReport(
        discord_connected=True,
        app_version="1.2.3",
        app_env="test",
        supabase=DependencyStatus.verified(),
        storage=DependencyStatus.verified(),
    )


class FilePipeline:
    def __init__(self, result: Phase2Result | None = None) -> None:
        self.result = result or Phase2Result(Phase2Status.PUBLISHED, "published")
        self.calls: list[str] = []

    async def run(self, reference: str) -> Phase2Result:
        self.calls.append(reference)
        return self.result


class FileWebSave:
    def __init__(self, result: Phase2Result | None = None) -> None:
        self.result = result or Phase2Result(Phase2Status.PUBLISHED, "published")
        self.calls: list[str] = []

    async def save(self, url: str) -> Phase2Result:
        self.calls.append(url)
        return self.result


def command_bot(settings) -> SimpleNamespace:
    return SimpleNamespace(
        settings=settings,
        pipeline=None,
        web_save_service=None,
        status_service=FakeStatusService(status_report()),
        repository=None,
        collector=None,
        brave_service=None,
        is_ready=lambda: True,
    )


async def test_save_routes_reddit_references_to_the_existing_pipeline(make_settings) -> None:
    bot = command_bot(make_settings())
    pipeline = FilePipeline()
    web_save = FileWebSave()
    bot.pipeline = pipeline
    bot.web_save_service = web_save
    cog = PhaseFourCommands(bot)
    interaction = FakeInteraction()

    await PhaseFourCommands.save.callback(cog, interaction, "abc123")

    assert pipeline.calls == ["abc123"]
    assert web_save.calls == []
    assert "published" in interaction.followup.messages[0]["args"][0]


async def test_save_routes_public_urls_through_the_safe_web_service(make_settings) -> None:
    bot = command_bot(make_settings())
    pipeline = FilePipeline()
    web_save = FileWebSave(Phase2Result(Phase2Status.SKIPPED, "original_media_not_found"))
    bot.pipeline = pipeline
    bot.web_save_service = web_save
    cog = PhaseFourCommands(bot)
    interaction = FakeInteraction()

    await PhaseFourCommands.save.callback(cog, interaction, "https://example.com/page")

    assert pipeline.calls == []
    assert web_save.calls == ["https://example.com/page"]
    assert "original_media_not_found" in interaction.followup.messages[0]["args"][0]


async def test_save_reports_unavailable_web_service(make_settings) -> None:
    bot = command_bot(make_settings())
    bot.pipeline = FilePipeline()
    bot.web_save_service = None
    cog = PhaseFourCommands(bot)
    interaction = FakeInteraction()

    await PhaseFourCommands.save.callback(cog, interaction, "https://example.com/page")

    assert interaction.followup.messages[0]["args"][0].startswith("Public URL saves")


async def test_save_reports_missing_pipeline(make_settings) -> None:
    bot = command_bot(make_settings())
    cog = PhaseFourCommands(bot)
    interaction = FakeInteraction()

    await PhaseFourCommands.save.callback(cog, interaction, "https://example.com/page")

    assert interaction.followup.messages[0]["args"][0].startswith("Save is unavailable")


async def test_save_does_not_leak_internal_failure_details(make_settings) -> None:
    class ExplodingWebSave:
        async def save(self, _url: str) -> Phase2Result:
            raise RuntimeError("secret internal detail")

    bot = command_bot(make_settings())
    bot.pipeline = FilePipeline()
    bot.web_save_service = ExplodingWebSave()
    cog = PhaseFourCommands(bot)
    interaction = FakeInteraction()

    await PhaseFourCommands.save.callback(cog, interaction, "https://example.com/page")

    message = interaction.followup.messages[0]["args"][0]
    assert "secret internal detail" not in message
    assert message.startswith("Could not save")


class StartRepository:
    def __init__(self) -> None:
        self.state = {"collection_enabled": False, "brave_paused": True}
        self.clear_calls = 0

    async def set_collection_enabled(self, enabled: bool) -> dict[str, Any]:
        self.state["collection_enabled"] = enabled
        return self.state

    async def clear_brave_pause(self) -> None:
        self.clear_calls += 1


async def test_start_does_not_clear_a_persisted_brave_pause(make_settings) -> None:
    bot = command_bot(make_settings(OWNER_DISCORD_ID="1"))
    repository = StartRepository()
    bot.repository = repository
    cog = PhaseFourCommands(bot)
    interaction = FakeInteraction(user_id=1)

    await PhaseFourCommands.start_collection.callback(cog, interaction)

    assert repository.state["collection_enabled"] is True
    assert repository.state["brave_paused"] is True
    assert repository.clear_calls == 0


class StatusRepository:
    def __init__(self) -> None:
        self.state = {
            "collection_enabled": True,
            "brave_paused": True,
            "brave_pause_reason": "http_403_access_denied",
            "last_reddit_success_at": "2026-01-01T00:00:00+00:00",
            "last_brave_success_at": "2026-01-02T00:00:00+00:00",
        }

    async def get_runtime_state(self) -> dict[str, Any]:
        return self.state

    async def latest_reddit_runs(self, _limit: int):
        return [{"outcome": "succeeded", "error_summary": None}]

    async def latest_brave_runs(self, _limit: int):
        return [{"outcome": "failed", "error_summary": "brave_access_restriction"}]

    async def latest_crawl_runs(self, _limit: int):
        return [{"outcome": "partial", "error_summary": "rate_limited: 1"}]

    async def latest_digest_runs(self, _limit: int):
        return [{"outcome": "succeeded", "error_summary": None}]

    async def digest_publication_counts(self, *, attempt_limit: int):
        assert attempt_limit == 3
        return {"pending": 1, "failed_retryable": 2, "failed_exhausted": 0}

    async def get_digest_state(self):
        return {
            "id": 1,
            "last_window_start": "2026-01-01T22:30:00+00:00",
            "last_window_end": "2026-01-02T00:00:00+00:00",
            "last_completed_at": "2026-01-02T00:01:00+00:00",
        }

    async def operational_counts(self):
        return {
            "queued": 12,
            "processing": 0,
            "verified": 3,
            "dumpster": 4,
            "rejected": 2,
            "blocked": 1,
            "failed": 1,
            "oversize": 0,
            "last_digest_success_at": None,
            "last_brave_success_at": self.state["last_brave_success_at"],
            "brave_paused": True,
            "brave_pause_reason": self.state["brave_pause_reason"],
        }


class StatusBraveService:
    def block_reason(self, state: dict[str, Any]) -> str | None:
        if state.get("brave_paused"):
            return "Brave adapter persistently paused: http_403_access_denied"
        return None


async def test_status_reports_brave_pause_runs_and_queue_state(make_settings) -> None:
    settings = make_settings(
        BRAVE_HTML_ENABLED="true",
        BRAVE_HTML_ACCESS_APPROVED="true",
        OWNER_DISCORD_ID="1",
    )
    bot = command_bot(settings)
    bot.repository = StatusRepository()
    bot.brave_service = StatusBraveService()
    bot.collector = SimpleNamespace(block_reason=None)
    cog = PhaseFourCommands(bot)
    interaction = FakeInteraction()

    await PhaseFourCommands.status_command.callback(cog, interaction)

    embed = interaction.followup.messages[0]["embed"]
    fields = {field.name: field.value for field in embed.fields}
    assert "Brave discovery" in fields
    assert "persistently paused" in fields["Brave discovery"]
    assert "http_403_access_denied" in fields["Brave discovery"]
    assert fields["Latest Brave run"].startswith("failed")
    assert fields["Latest crawler run"].startswith("partial")
    assert fields["Queue / backlog"] == "12"
    assert fields["Dumpster"] == "4"
    assert fields["Last successful Brave discovery"] == "2026-01-02T00:00:00+00:00"
    assert "Schedule: 04:00 IST" in fields["Daily digest"]
    assert "latest: succeeded" in fields["Daily digest"]
    assert "pending: 1" in fields["Daily digest"]
    assert "retryable failed: 2" in fields["Daily digest"]
    assert "window: complete" in fields["Daily digest"]


async def test_status_labels_disabled_brave_feature(make_settings) -> None:
    bot = command_bot(make_settings())
    bot.repository = StatusRepository()
    bot.brave_service = StatusBraveService()
    bot.collector = SimpleNamespace(block_reason=None)
    cog = PhaseFourCommands(bot)
    interaction = FakeInteraction()

    await PhaseFourCommands.status_command.callback(cog, interaction)

    fields = {field.name: field.value for field in interaction.followup.messages[0]["embed"].fields}
    assert "state: disabled by feature flag" in fields["Brave discovery"]


class SetupRepository:
    def __init__(self) -> None:
        self.marked = 0
        self.recovered = 0

    async def mark_interrupted_jobs(self) -> None:
        self.marked += 1

    async def recover_processing_discoveries(self) -> int:
        self.recovered += 1
        return 2

    async def get_runtime_state(self) -> dict[str, Any]:
        return {"collection_enabled": False}

    async def record_source(self, *args: object, **kwargs: object) -> None:
        return None


async def test_scheduler_registers_phase_five_jobs_with_required_intervals(
    make_settings,
) -> None:
    settings = make_settings(
        BRAVE_SEARCH_INTERVAL_HOURS="8",
        CRAWL_BATCH_SIZE="25",
    )
    repository = SetupRepository()
    bot = TifaBot(
        settings,
        FakeStatusService(status_report()),
        sync_commands=False,
        repository=repository,
        storage=object(),
    )
    try:
        await bot.setup_hook()
        assert bot.scheduler is not None
        jobs = {job.id: job for job in bot.scheduler.get_jobs()}
        assert set(jobs) == {
            "reddit_collection_hourly",
            "brave_discovery",
            "crawl_processing",
            "daily_digest",
        }
        assert jobs["brave_discovery"].trigger.interval.total_seconds() == 8 * 3600
        assert jobs["crawl_processing"].trigger.interval.total_seconds() == 300
        assert "hour='4'" in str(jobs["daily_digest"].trigger)
        assert "minute='0'" in str(jobs["daily_digest"].trigger)
        assert jobs["daily_digest"].coalesce is True
        assert jobs["daily_digest"].max_instances == 1
        assert bot.pipeline is not None
        assert bot.brave_service is not None
        assert bot.crawler is not None
        assert bot.web_save_service is not None
        assert bot.safe_http_client is not None
        assert repository.marked == 1
        assert repository.recovered == 1
    finally:
        await bot.close()


class ScheduledBrave:
    def __init__(self, order: list[str]) -> None:
        self.order = order

    async def run(self, *, scheduled: bool) -> None:
        self.order.append(f"brave:{scheduled}")


class ScheduledCrawler:
    def __init__(self, order: list[str]) -> None:
        self.order = order
        self.order.append("crawler:created")

    async def run_once(self, *, scheduled: bool) -> None:
        self.order.append(f"crawl:{scheduled}")


async def test_brave_schedule_runs_discovery_then_the_crawler(make_settings) -> None:
    bot = TifaBot(make_settings(), FakeStatusService(status_report()), sync_commands=False)
    order: list[str] = []
    bot.brave_service = ScheduledBrave(order)
    bot.crawler = ScheduledCrawler(order)
    try:
        await bot._scheduled_brave_discovery()
    finally:
        await bot.close()

    assert order == ["crawler:created", "brave:True", "crawl:True"]


async def test_crawl_schedule_runs_only_the_crawler(make_settings) -> None:
    bot = TifaBot(make_settings(), FakeStatusService(status_report()), sync_commands=False)
    order: list[str] = []
    bot.brave_service = ScheduledBrave(order)
    bot.crawler = ScheduledCrawler(order)
    try:
        await bot._scheduled_crawl_processing()
    finally:
        await bot.close()

    assert order == ["crawler:created", "crawl:True"]


class ClosingHttpClient:
    def __init__(self) -> None:
        self.closed = 0

    async def close(self) -> None:
        self.closed += 1


async def test_bot_close_releases_the_shared_http_client(make_settings) -> None:
    bot = TifaBot(make_settings(), FakeStatusService(status_report()), sync_commands=False)
    client = ClosingHttpClient()
    bot.safe_http_client = client

    await bot.close()

    assert client.closed == 1
    assert bot.safe_http_client is None


def test_phase_five_crawler_defaults_are_locked_and_bounded() -> None:
    settings = Settings.from_env({}, load_env_file=False)

    assert settings.max_urls_per_job == 300
    assert settings.max_pages_per_domain == 20
    assert settings.max_concurrent_requests == 5
    assert settings.per_domain_delay_seconds == 2.0
    assert settings.max_page_bytes == 5_000_000
    assert settings.max_redirects == 5
    assert settings.crawl_connect_timeout_seconds == 10.0
    assert settings.crawl_read_timeout_seconds == 20.0
    assert settings.max_crawl_depth == 6
    assert settings.crawl_max_attempts == 3
    assert settings.brave_html_enabled is False
    assert settings.brave_html_access_approved is False
    assert settings.brave_search_interval_hours == 8
    assert settings.brave_search_max_pages_per_phrase == 2
    assert settings.brave_search_phrases == settings.reddit_search_phrases


def test_phase_five_settings_are_independently_configurable() -> None:
    settings = Settings.from_env(
        {
            "BRAVE_SEARCH_BASE_URL": "https://search.brave.com/search?source=web",
            "BRAVE_SEARCH_INTERVAL_HOURS": "12",
            "BRAVE_SEARCH_PHRASES": "Tifa|Tifa Lockhart cosplay",
            "BRAVE_SEARCH_MAX_PAGES_PER_PHRASE": "3",
            "MAX_URLS_PER_JOB": "100",
            "MAX_PAGES_PER_DOMAIN": "10",
            "CRAWL_BATCH_SIZE": "10",
        },
        load_env_file=False,
    )

    assert settings.brave_search_base_url.endswith("?source=web")
    assert settings.brave_search_interval_hours == 12
    assert settings.brave_search_phrases == ("Tifa", "Tifa Lockhart cosplay")
    assert settings.brave_search_max_pages_per_phrase == 3
    assert settings.max_urls_per_job == 100
    assert settings.max_pages_per_domain == 10
    assert settings.crawl_batch_size == 10


@pytest.mark.parametrize(
    "overrides",
    [
        {"BRAVE_SEARCH_PHRASES": ""},
        {"BRAVE_SEARCH_INTERVAL_HOURS": "0"},
        {"BRAVE_SEARCH_INTERVAL_HOURS": "169"},
        {"BRAVE_SEARCH_MAX_PAGES_PER_PHRASE": "0"},
        {"BRAVE_SEARCH_MAX_PAGES_PER_PHRASE": "11"},
        {"BRAVE_SEARCH_BASE_URL": "http://search.brave.com/search"},
        {"BRAVE_SEARCH_BASE_URL": "https://user:pass@search.brave.com/search"},
        {"MAX_PAGE_BYTES": "5000001"},
        {"MAX_CRAWL_DEPTH": "7"},
        {"MAX_URLS_PER_JOB": "301"},
        {"MAX_PAGES_PER_DOMAIN": "21"},
        {"MAX_CONCURRENT_REQUESTS": "6"},
        {"PER_DOMAIN_DELAY_SECONDS": "1.9"},
        {"PER_DOMAIN_DELAY_SECONDS": "61"},
        {"MAX_REDIRECTS": "6"},
        {"CRAWL_CONNECT_TIMEOUT_SECONDS": "0.05"},
        {"CRAWL_CONNECT_TIMEOUT_SECONDS": "11"},
        {"CRAWL_READ_TIMEOUT_SECONDS": "0.05"},
        {"CRAWL_READ_TIMEOUT_SECONDS": "21"},
        {"CRAWL_MAX_ATTEMPTS": "0"},
        {"CRAWL_MAX_ATTEMPTS": "4"},
        {"CRAWL_BATCH_SIZE": "0"},
        {"CRAWL_BATCH_SIZE": "301"},
    ],
)
def test_phase_five_runtime_validation_rejects_unsafe_values(
    make_settings, overrides: dict[str, str]
) -> None:
    settings = make_settings(**overrides)

    with pytest.raises(ConfigurationError):
        settings.validate_runtime()


def test_status_embed_footer_marks_phase_five() -> None:
    embed = build_status_embed(status_report())

    assert embed.footer.text == "Phase 5 discovery and crawler"
