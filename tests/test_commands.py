from __future__ import annotations

import hashlib
import types

import discord
from discord import app_commands

from app.bot.client import sync_application_commands
from app.bot.commands import (
    ABOUT_PURPOSE,
    PhaseFourCommands,
    build_about_embed,
    build_status_embed,
)
from app.health.status import (
    DependencyStatus,
    StatusReport,
)


def test_about_embed_contains_required_facts(make_settings) -> None:
    settings = make_settings(APP_VERSION="9.8.7")
    embed = build_about_embed(settings)
    fields = {field.name: field.value for field in embed.fields}

    assert embed.title == "Tifa Lockhart Bot"
    assert embed.description == ABOUT_PURPOSE
    assert "Tifa Lockhart" in ABOUT_PURPOSE
    assert fields["Version"] == "9.8.7"
    assert fields["Developer"] == "SoftwareFromVish"
    assert embed.url == "https://github.com/vishwassharma38/Tifa-Lockhart-Bot"
    assert "http" not in ABOUT_PURPOSE


def test_about_embed_does_not_expose_configured_secrets(make_settings) -> None:
    settings = make_settings(DISCORD_TOKEN="discord-secret-123")
    embed = build_about_embed(settings)
    rendered = f"{embed.title} {embed.description} {embed.footer.text}"

    assert "discord-secret-123" not in rendered


def test_status_embed_reports_dependency_states() -> None:
    report = StatusReport(
        discord_connected=True,
        app_version="1.2.3",
        app_env="test",
        supabase=DependencyStatus.verified(),
        storage=DependencyStatus.unavailable(),
    )
    embed = build_status_embed(report)
    fields = {field.name: field.value for field in embed.fields}

    assert fields["Discord"] == "Connected"
    assert fields["Version"] == "1.2.3 (test)"
    assert fields["Supabase"] == "Connected / verified"
    assert fields["B2 storage"] == "Unavailable"
    assert fields["Collection"] == "Persisted state unavailable"


def test_status_embed_distinguishes_unchecked_and_unconfigured() -> None:
    report = StatusReport(
        discord_connected=False,
        app_version="1.0.0",
        app_env="development",
        supabase=DependencyStatus.not_checked(),
        storage=DependencyStatus.not_configured(),
    )
    embed = build_status_embed(report)
    fields = {field.name: field.value for field in embed.fields}

    assert fields["Discord"] == "Disconnected / starting"
    assert fields["Supabase"] == "Configured, not checked"
    assert fields["B2 storage"] == "Not configured"
    assert fields["Collection"] == "Persisted state unavailable"


def test_phase_four_commands_are_registered_once() -> None:
    names = {command.name for command in PhaseFourCommands.__cog_app_commands__}
    assert names == {"about", "status", "start", "stop", "save", "random", "stats"}


class FakeTree:
    def __init__(self) -> None:
        self.copied_guild = None
        self.sync_calls: list[object | None] = []
        self.commands = [object(), object()]

    def copy_global_to(self, guild) -> None:
        self.copied_guild = guild

    async def sync(self, guild=None):
        self.sync_calls.append(guild)
        return self.commands


async def test_sync_uses_development_guild_scope_when_configured() -> None:
    tree = FakeTree()
    count = await sync_application_commands(tree, 123456789)

    assert count == 2
    assert tree.copied_guild.id == 123456789
    assert len(tree.sync_calls) == 1
    assert tree.sync_calls[0] is tree.copied_guild


async def test_sync_uses_global_scope_when_guild_is_not_configured() -> None:
    tree = FakeTree()
    count = await sync_application_commands(tree, None)

    assert count == 2
    assert tree.copied_guild is None
    assert tree.sync_calls == [None]


class FakeResponse:
    def __init__(self) -> None:
        self.deferred: list[dict[str, object]] = []
        self.messages: list[dict[str, object]] = []

    async def defer(self, **kwargs: object) -> None:
        self.deferred.append(kwargs)

    async def send_message(self, *args: object, **kwargs: object) -> None:
        self.messages.append({"args": args, **kwargs})


class FakeSentMessage:
    def __init__(self, message_id: int = 999) -> None:
        self.id = message_id


class FakeFollowup:
    def __init__(self) -> None:
        self.messages: list[dict[str, object]] = []

    async def send(self, *args: object, **kwargs: object) -> FakeSentMessage:
        self.messages.append({"args": args, **kwargs})
        return FakeSentMessage()


class FakeInteraction:
    def __init__(self, user_id: int = 1, channel_id: int = 5) -> None:
        self.response = FakeResponse()
        self.followup = FakeFollowup()
        self.user = type("User", (), {"id": user_id})()
        self.channel_id = channel_id


class FakeStatusService:
    def __init__(
        self, *, report: StatusReport | None = None, error: Exception | None = None
    ) -> None:
        self.report = report
        self.error = error

    async def collect(self, **_kwargs: object) -> StatusReport:
        if self.error is not None:
            raise self.error
        assert self.report is not None
        return self.report


class FakeBot:
    def __init__(
        self,
        settings,
        status_service: FakeStatusService,
        *,
        repository: object | None = None,
        storage: object | None = None,
    ) -> None:
        self.settings = settings
        self.status_service = status_service
        self.repository = repository
        self.storage = storage

    def is_ready(self) -> bool:
        return True


async def test_about_command_sends_the_about_embed(make_settings) -> None:
    settings = make_settings(APP_VERSION="9.8.7")
    cog = PhaseFourCommands(FakeBot(settings, FakeStatusService()))
    interaction = FakeInteraction()

    await PhaseFourCommands.about.callback(cog, interaction)

    assert len(interaction.response.messages) == 1
    embed = interaction.response.messages[0]["embed"]
    assert embed.title == "Tifa Lockhart Bot"


async def test_start_is_owner_only_and_persists_collection_state(make_settings) -> None:
    class Repository:
        enabled = False

        async def set_collection_enabled(self, enabled):
            self.enabled = enabled
            return {"collection_enabled": enabled}

    class Collector:
        block_reason = "Reddit access approval has not been confirmed"

    bot = FakeBot(make_settings(OWNER_DISCORD_ID="1"), FakeStatusService())
    bot.repository = Repository()
    bot.collector = Collector()
    cog = PhaseFourCommands(bot)
    forbidden = FakeInteraction(user_id=2)
    await PhaseFourCommands.start_collection.callback(cog, forbidden)
    assert bot.repository.enabled is False
    assert "restricted" in forbidden.response.messages[0]["args"][0]

    owner = FakeInteraction(user_id=1)
    await PhaseFourCommands.start_collection.callback(cog, owner)
    assert bot.repository.enabled is True
    assert "blocked" in owner.followup.messages[0]["args"][0]


async def test_stop_persists_paused_state(make_settings) -> None:
    class Repository:
        async def set_collection_enabled(self, enabled):
            return {"collection_enabled": enabled}

    bot = FakeBot(make_settings(OWNER_DISCORD_ID="1"), FakeStatusService())
    bot.repository = Repository()
    cog = PhaseFourCommands(bot)
    interaction = FakeInteraction(user_id=1)
    await PhaseFourCommands.stop_collection.callback(cog, interaction)
    assert "paused" in interaction.followup.messages[0]["args"][0]


async def test_status_command_defers_then_reports_dependency_state(make_settings) -> None:
    report = StatusReport(
        discord_connected=True,
        app_version="1.2.3",
        app_env="test",
        supabase=DependencyStatus.verified(),
        storage=DependencyStatus.verified(),
    )
    cog = PhaseFourCommands(FakeBot(make_settings(), FakeStatusService(report=report)))
    interaction = FakeInteraction()

    await PhaseFourCommands.status_command.callback(cog, interaction)

    assert interaction.response.deferred == [{"ephemeral": True, "thinking": True}]
    assert len(interaction.followup.messages) == 1
    message = interaction.followup.messages[0]
    assert message["ephemeral"] is True
    assert message["embed"].title == "Tifa Lockhart Bot - Status"


async def test_status_command_reports_failure_without_leaking_details(make_settings) -> None:
    error = RuntimeError("secret-looking failure detail")
    cog = PhaseFourCommands(FakeBot(make_settings(), FakeStatusService(error=error)))
    interaction = FakeInteraction()

    await PhaseFourCommands.status_command.callback(cog, interaction)

    assert len(interaction.followup.messages) == 1
    message = interaction.followup.messages[0]
    assert message["ephemeral"] is True
    assert "secret-looking failure detail" not in str(message)


class FakeOperationalRepository:
    def __init__(
        self,
        *,
        digest_runs: list[dict[str, object]] | None = None,
        digest_state: dict[str, object] | None = None,
        digest_counts: dict[str, int] | None = None,
        fail_digest: bool = False,
    ) -> None:
        self.digest_runs = digest_runs or []
        self.digest_state = digest_state or {"id": 1}
        self.digest_counts = digest_counts or {
            "pending": 0,
            "failed_retryable": 0,
            "failed_exhausted": 0,
        }
        self.fail_digest = fail_digest

    async def get_runtime_state(self) -> dict[str, object]:
        return {
            "collection_enabled": True,
            "last_reddit_success_at": "2026-01-01T00:00:00+00:00",
            "last_bing_success_at": None,
            "last_digest_success_at": "2026-01-01T04:00:00+00:00",
            "bing_paused": False,
            "bing_pause_reason": None,
        }

    async def latest_reddit_runs(self, _limit: int = 1) -> list[dict[str, object]]:
        return []

    async def latest_bing_runs(self, _limit: int = 1) -> list[dict[str, object]]:
        return []

    async def latest_crawl_runs(self, _limit: int = 1) -> list[dict[str, object]]:
        return []

    async def latest_digest_runs(self, _limit: int = 1) -> list[dict[str, object]]:
        if self.fail_digest:
            raise RuntimeError("digest runs unavailable")
        return self.digest_runs

    async def digest_publication_counts(self, *, attempt_limit: int = 3) -> dict[str, int]:
        assert attempt_limit == 3
        if self.fail_digest:
            raise RuntimeError("digest counts unavailable")
        return dict(self.digest_counts)

    async def get_digest_state(self) -> dict[str, object]:
        if self.fail_digest:
            raise RuntimeError("digest state unavailable")
        return dict(self.digest_state)

    async def operational_counts(self) -> dict[str, object]:
        return {
            "queued": 7,
            "processing": 1,
            "verified": 4,
            "dumpster": 3,
            "rejected": 2,
            "blocked": 1,
            "failed": 1,
            "oversize": 1,
            "last_digest_success_at": "2026-01-01T04:00:00+00:00",
            "last_bing_success_at": None,
            "bing_paused": False,
            "bing_pause_reason": None,
        }

    async def asset_stats(self, _start: str | None = None) -> dict[str, int]:
        return {"total": 10, "delivered": 8, "image": 6, "animation": 2, "video": 2}

    async def random_asset(self) -> dict[str, object]:
        data = b"random-media"
        return {
            "id": "media-random",
            "storage_object_key": "media/random",
            "sha256": hashlib.sha256(data).hexdigest(),
            "filename": "random.png",
            "extension": "png",
        }

    async def source_for_media(self, _media_item_id: str) -> dict[str, object]:
        return {
            "title": "Random Tifa artwork",
            "creator_name": "artist",
            "creator_profile_url": "https://example.com/artist",
            "source_url": "https://example.com/random",
        }

    async def create_publication(
        self, _media_item_id: str, _channel_id: int, _purpose: str
    ) -> dict[str, object]:
        return {"id": "publication-random"}

    async def update_publication(
        self,
        _publication_id: str,
        *,
        status: str,
        message_id: int | None = None,
        last_error: str | None = None,
    ) -> dict[str, object]:
        return {"status": status, "message_id": message_id, "last_error": last_error}


class FakeRandomStorage:
    async def download_bytes(
        self, _object_key: str, *, max_bytes: int | None = None, timeout: float | None = None
    ) -> bytes:
        assert max_bytes == 10_000_000
        return b"random-media"


async def test_status_command_reports_real_digest_state(make_settings) -> None:
    report = StatusReport(
        discord_connected=True,
        app_version="1.2.3",
        app_env="test",
        supabase=DependencyStatus.verified(),
        storage=DependencyStatus.verified(),
    )
    repository = FakeOperationalRepository(
        digest_runs=[
            {
                "outcome": "partial",
                "error_summary": "storage_download_failed:RuntimeError",
            }
        ],
        digest_state={
            "id": 1,
            "last_window_start": "2026-01-01T22:00:00+00:00",
            "last_window_end": "2026-01-02T00:00:00+00:00",
            "last_completed_at": None,
        },
        digest_counts={"pending": 4, "failed_retryable": 2, "failed_exhausted": 1},
    )
    cog = PhaseFourCommands(
        FakeBot(
            make_settings(OWNER_DISCORD_ID="1"),
            FakeStatusService(report=report),
            repository=repository,
        )
    )
    interaction = FakeInteraction()

    await PhaseFourCommands.status_command.callback(cog, interaction)

    assert len(interaction.followup.messages) == 1
    embed = interaction.followup.messages[0]["embed"]
    fields = {field.name: field.value for field in embed.fields}
    digest = fields["Daily digest"]
    assert "Schedule: 04:00 IST" in digest
    assert "last success: 2026-01-01T04:00:00+00:00" in digest
    assert "latest: partial (storage_download_failed:RuntimeError)" in digest
    assert "pending: 4" in digest
    assert "retryable failed: 2" in digest
    assert "exhausted: 1" in digest
    assert "window: incomplete / awaiting recovery" in digest
    assert "Not implemented in Phase 6" not in digest


async def test_status_command_reports_empty_digest_history(make_settings) -> None:
    report = StatusReport(
        discord_connected=True,
        app_version="1.2.3",
        app_env="test",
        supabase=DependencyStatus.verified(),
        storage=DependencyStatus.verified(),
    )
    repository = FakeOperationalRepository(digest_runs=[], digest_state={"id": 1})
    cog = PhaseFourCommands(
        FakeBot(make_settings(), FakeStatusService(report=report), repository=repository)
    )
    interaction = FakeInteraction()

    await PhaseFourCommands.status_command.callback(cog, interaction)

    embed = interaction.followup.messages[0]["embed"]
    fields = {field.name: field.value for field in embed.fields}
    digest = fields["Daily digest"]
    assert "latest: No runs" in digest
    assert "pending: 0" in digest
    assert "window: not started" in digest


async def test_status_command_reports_bing_gates_and_latest_run(make_settings) -> None:
    class FakeBingService:
        def block_reason(self, _state: dict[str, object]) -> str:
            return "BING_HTML_ENABLED is false"

    report = StatusReport(
        discord_connected=True,
        app_version="1.2.3",
        app_env="test",
        supabase=DependencyStatus.verified(),
        storage=DependencyStatus.verified(),
    )
    bot = FakeBot(
        make_settings(),
        FakeStatusService(report=report),
        repository=FakeOperationalRepository(),
    )
    bot.bing_service = FakeBingService()
    cog = PhaseFourCommands(bot)
    interaction = FakeInteraction()

    await PhaseFourCommands.status_command.callback(cog, interaction)

    assert len(interaction.followup.messages) == 1
    embed = interaction.followup.messages[0]["embed"]
    fields = {field.name: field.value for field in embed.fields}
    assert fields["Bing discovery"] == (
        "Feature: disabled; access: not approved; state: disabled by feature flag; "
        "pause reason: none; block: BING_HTML_ENABLED is false"
    )
    assert fields["Latest Bing run"] == "No runs"
    assert fields["Last successful Bing discovery"] == "Never"


async def test_status_command_survives_unavailable_digest_data(make_settings) -> None:
    report = StatusReport(
        discord_connected=True,
        app_version="1.2.3",
        app_env="test",
        supabase=DependencyStatus.verified(),
        storage=DependencyStatus.verified(),
    )
    repository = FakeOperationalRepository(fail_digest=True)
    cog = PhaseFourCommands(
        FakeBot(make_settings(), FakeStatusService(report=report), repository=repository)
    )
    interaction = FakeInteraction()

    await PhaseFourCommands.status_command.callback(cog, interaction)

    assert len(interaction.followup.messages) == 1
    embed = interaction.followup.messages[0]["embed"]
    fields = {field.name: field.value for field in embed.fields}
    assert "Daily digest" not in fields
    assert fields["Collection"].startswith("Persisted: enabled")


async def test_stop_pauses_collection_without_touching_digest(make_settings) -> None:
    class Repository(FakeOperationalRepository):
        async def set_collection_enabled(self, enabled: bool) -> dict[str, object]:
            return {"collection_enabled": enabled}

    class Digest:
        def __init__(self) -> None:
            self.run_calls: list[str] = []

        async def run(self, *, trigger: str) -> None:
            self.run_calls.append(trigger)

    bot = FakeBot(
        make_settings(OWNER_DISCORD_ID="1"),
        FakeStatusService(),
        repository=Repository(),
    )
    digest = Digest()
    bot.digest_service = digest
    cog = PhaseFourCommands(bot)
    interaction = FakeInteraction(user_id=1)

    await PhaseFourCommands.stop_collection.callback(cog, interaction)

    assert "paused" in interaction.followup.messages[0]["args"][0]
    assert digest.run_calls == []


async def test_random_command_still_delivers_archived_media(make_settings) -> None:
    repository = FakeOperationalRepository()
    bot = FakeBot(
        make_settings(),
        FakeStatusService(),
        repository=repository,
        storage=FakeRandomStorage(),
    )
    cog = PhaseFourCommands(bot)
    interaction = FakeInteraction(channel_id=5)

    await PhaseFourCommands.random_asset.callback(cog, interaction)

    assert interaction.response.deferred == [{"thinking": True}]
    message = interaction.followup.messages[0]
    assert isinstance(message["file"], discord.File)
    assert "Random Tifa artwork" in message["args"][0]
    assert "Creator: [artist](https://example.com/artist)" in message["args"][0]
    assert message["suppress_embeds"] is True


async def test_stats_command_still_reports_archive_counts(make_settings) -> None:
    cog = PhaseFourCommands(
        FakeBot(
            make_settings(),
            FakeStatusService(),
            repository=FakeOperationalRepository(),
        )
    )
    interaction = FakeInteraction()

    await PhaseFourCommands.stats.callback(
        cog,
        interaction,
        app_commands.Choice(name="Week", value="week"),
    )

    text = interaction.followup.messages[0]["args"][0]
    assert "unique archived assets: 10" in text
    assert "delivered files: 8" in text
    assert "images: 6" in text


async def test_save_command_remains_available_while_collection_is_paused(make_settings) -> None:
    class Repository(FakeOperationalRepository):
        async def get_runtime_state(self) -> dict[str, object]:
            return {"collection_enabled": False}

    class Pipeline:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def run(self, url: str) -> object:
            self.calls.append(url)
            return types.SimpleNamespace(
                status=types.SimpleNamespace(value="published"),
                reason="published",
            )

    pipeline = Pipeline()
    bot = FakeBot(
        make_settings(OWNER_DISCORD_ID="1"),
        FakeStatusService(),
        repository=Repository(),
    )
    bot.pipeline = pipeline
    cog = PhaseFourCommands(bot)
    interaction = FakeInteraction()

    await PhaseFourCommands.save.callback(cog, interaction, "abc123")

    assert interaction.response.deferred == [{"ephemeral": True, "thinking": True}]
    assert pipeline.calls == ["abc123"]
    assert "published" in interaction.followup.messages[0]["args"][0]
