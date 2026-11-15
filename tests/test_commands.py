from __future__ import annotations

import asyncio
import hashlib
import types

import discord
import pytest
from discord import app_commands

from app.bot.client import sync_application_commands
from app.bot.commands import (
    ABOUT_PURPOSE,
    PhaseFourCommands,
    build_about_embed,
    build_status_embed,
)
from app.bot.publisher import (
    DiscordPublicationMessageError,
    MarkerLookup,
)
from app.health.status import (
    DependencyStatus,
    StatusReport,
)
from app.storage.base import (
    StorageAccessDeniedError,
    StorageObjectNotFoundError,
    StorageObjectTooLargeError,
    StorageOperationError,
    StorageTimeoutError,
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


class RecordingRandomRepository(FakeOperationalRepository):
    def __init__(self) -> None:
        super().__init__()
        self.publication_updates: list[dict[str, object]] = []
        self.media_sources: list[dict[str, object]] | None = None
        self.publications: dict[str, dict[str, object]] = {}
        self.fail_published_update = False
        self.fail_published_update_after_commit = False
        self.fail_publication_reconciliation = False

    async def sources_for_media(self, _media_item_id: str) -> list[dict[str, object]]:
        if self.media_sources is not None:
            return [dict(source) for source in self.media_sources]
        source = await self.source_for_media(_media_item_id)
        return [source]

    async def get_publication(self, publication_id: str) -> dict[str, object] | None:
        if self.fail_publication_reconciliation:
            raise RuntimeError("publication lookup unavailable")
        row = self.publications.get(publication_id)
        return dict(row) if row is not None else None

    async def update_publication(
        self,
        publication_id: str,
        *,
        status: str,
        message_id: int | None = None,
        last_error: str | None = None,
    ) -> dict[str, object]:
        self.publication_updates.append(
            {
                "publication_id": publication_id,
                "status": status,
                "message_id": message_id,
                "last_error": last_error,
            }
        )
        if status == "published" and self.fail_published_update:
            raise RuntimeError("publication update unavailable")
        row = self.publications.setdefault(
            publication_id,
            {
                "id": publication_id,
                "status": "pending",
                "message_id": None,
                "published_at": None,
            },
        )
        if row.get("status") == "published":
            # Mirror the repository's terminal-state guard.
            return dict(row)
        if status == "published":
            row.update(
                status="published",
                message_id=message_id,
                last_error=last_error,
                published_at="2026-11-15T00:00:00+00:00",
            )
            if self.fail_published_update_after_commit:
                raise TimeoutError("response lost after commit")
        else:
            row.update(status=status, last_error=last_error)
            if message_id is not None:
                row["message_id"] = message_id
        return dict(row)


class FailingRandomStorage(FakeRandomStorage):
    def __init__(self, error: Exception) -> None:
        self.error = error

    async def download_bytes(
        self, _object_key: str, *, max_bytes: int | None = None, timeout: float | None = None
    ) -> bytes:
        raise self.error


class ControlledFollowup(FakeFollowup):
    def __init__(self, errors: list[BaseException] | None = None) -> None:
        super().__init__()
        self.errors = list(errors or [])
        self.attempts: list[dict[str, object]] = []

    async def send(self, *args: object, **kwargs: object) -> FakeSentMessage:
        self.attempts.append({"args": args, **kwargs})
        if self.errors:
            raise self.errors.pop(0)
        return await super().send(*args, **kwargs)


class FakeDiscordResponse:
    def __init__(self, status: int) -> None:
        self.status = status
        self.reason = "test response"


def discord_http_error(status: int) -> discord.HTTPException:
    return discord.HTTPException(FakeDiscordResponse(status), "test failure")


def random_interaction(*errors: BaseException) -> FakeInteraction:
    interaction = FakeInteraction(channel_id=5)
    interaction.followup = ControlledFollowup(list(errors))
    return interaction


def random_cog(
    *,
    repository: FakeOperationalRepository | None = None,
    storage: object | None = None,
) -> PhaseFourCommands:
    return PhaseFourCommands(
        FakeBot(
            None,  # type: ignore[arg-type]
            FakeStatusService(),
            repository=repository or FakeOperationalRepository(),
            storage=storage or FakeRandomStorage(),
        )
    )


def mark_publication_lookup(
    monkeypatch: pytest.MonkeyPatch,
    *,
    message_id: int | None,
    conclusive: bool,
) -> None:
    async def fake_lookup(self, *, channel_id: int, marker: str, limit: int = 500):
        del self, channel_id, marker, limit
        return MarkerLookup(message_id=message_id, conclusive=conclusive)

    monkeypatch.setattr(
        "app.bot.commands.DiscordPublisher.find_publication_marker",
        fake_lookup,
    )


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
    repository = RecordingRandomRepository()
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
    assert message["file"].fp.closed
    assert "Random Tifa artwork" in message["args"][0]
    assert "Creator: [artist](https://example.com/artist)" in message["args"][0]
    assert "tifa-random-publication:publication-random" in message["args"][0]
    assert message["suppress_embeds"] is True
    assert repository.publication_updates[-1] == {
        "publication_id": "publication-random",
        "status": "published",
        "message_id": 999,
        "last_error": "",
    }


async def test_random_command_bounds_large_crawler_provenance(make_settings) -> None:
    repository = RecordingRandomRepository()
    repository.media_sources = [
        {
            "source_url": f"https://example.com/gallery/tifa-{index}",
            "creator_name": f"Artist {index}",
            "creator_profile_url": f"https://example.com/artist/{index}",
            "title": f"Tifa artwork {index}",
        }
        for index in range(60)
    ]
    bot = FakeBot(
        make_settings(),
        FakeStatusService(),
        repository=repository,
        storage=FakeRandomStorage(),
    )
    interaction = random_interaction()

    await PhaseFourCommands.random_asset.callback(PhaseFourCommands(bot), interaction)

    message = interaction.followup.messages[0]["args"][0]
    assert len(message) <= 1800
    assert "more archived reference" in message
    assert repository.publication_updates[-1]["status"] == "published"


async def test_random_command_reports_missing_archive_object(make_settings) -> None:
    repository = RecordingRandomRepository()
    bot = FakeBot(
        make_settings(),
        FakeStatusService(),
        repository=repository,
        storage=FailingRandomStorage(StorageObjectNotFoundError("object missing")),
    )
    interaction = random_interaction()

    await PhaseFourCommands.random_asset.callback(PhaseFourCommands(bot), interaction)

    assert repository.publication_updates[-1]["status"] == "failed"
    assert str(repository.publication_updates[-1]["last_error"]).startswith(
        "archive_object_not_found:"
    )
    assert "missing from storage" in interaction.followup.messages[0]["args"][0]


@pytest.mark.parametrize(
    ("storage_error", "failure_code", "user_message"),
    [
        (
            StorageObjectTooLargeError("too large"),
            "archive_object_too_large",
            "too large to deliver",
        ),
        (
            StorageTimeoutError("timed out"),
            "archive_download_failed",
            "could not be retrieved",
        ),
        (
            StorageAccessDeniedError("denied"),
            "archive_download_failed",
            "could not be retrieved",
        ),
        (
            StorageOperationError("storage failure"),
            "archive_download_failed",
            "could not be retrieved",
        ),
    ],
)
async def test_random_command_reports_storage_stage_failures(
    make_settings,
    storage_error: Exception,
    failure_code: str,
    user_message: str,
) -> None:
    repository = RecordingRandomRepository()
    bot = FakeBot(
        make_settings(),
        FakeStatusService(),
        repository=repository,
        storage=FailingRandomStorage(storage_error),
    )
    interaction = random_interaction()

    await PhaseFourCommands.random_asset.callback(PhaseFourCommands(bot), interaction)

    update = repository.publication_updates[-1]
    assert update["status"] == "failed"
    assert str(update["last_error"]).startswith(f"{failure_code}:")
    assert user_message in interaction.followup.messages[0]["args"][0]


async def test_random_command_reports_media_selection_failure(make_settings) -> None:
    class SelectionFailureRepository(RecordingRandomRepository):
        async def random_asset(self) -> dict[str, object] | None:
            raise RuntimeError("eligible count unavailable")

    repository = SelectionFailureRepository()
    bot = FakeBot(
        make_settings(),
        FakeStatusService(),
        repository=repository,
        storage=FakeRandomStorage(),
    )
    interaction = random_interaction()

    await PhaseFourCommands.random_asset.callback(PhaseFourCommands(bot), interaction)

    assert repository.publication_updates == []
    assert "Could not select archived media" in interaction.followup.messages[0]["args"][0]


async def test_random_command_reports_publication_creation_failure(make_settings) -> None:
    class CreationFailureRepository(RecordingRandomRepository):
        async def create_publication(
            self, _media_item_id: str, _channel_id: int, _purpose: str
        ) -> dict[str, object]:
            raise RuntimeError("publication insert unavailable")

    repository = CreationFailureRepository()
    bot = FakeBot(
        make_settings(),
        FakeStatusService(),
        repository=repository,
        storage=FakeRandomStorage(),
    )
    interaction = random_interaction()

    await PhaseFourCommands.random_asset.callback(PhaseFourCommands(bot), interaction)

    assert repository.publication_updates == []
    assert (
        "publication record could not be created" in (interaction.followup.messages[0]["args"][0])
    )


async def test_random_command_reports_source_metadata_failure(make_settings) -> None:
    class SourceFailureRepository(RecordingRandomRepository):
        async def sources_for_media(self, _media_item_id: str) -> list[dict[str, object]]:
            raise RuntimeError("source lookup unavailable")

    repository = SourceFailureRepository()
    bot = FakeBot(
        make_settings(),
        FakeStatusService(),
        repository=repository,
        storage=FakeRandomStorage(),
    )
    interaction = random_interaction()

    await PhaseFourCommands.random_asset.callback(PhaseFourCommands(bot), interaction)

    update = repository.publication_updates[-1]
    assert update["status"] == "failed"
    assert str(update["last_error"]).startswith("source_metadata_retrieval_failed:")
    assert "source details could not be loaded" in interaction.followup.messages[0]["args"][0]


async def test_random_command_reports_integrity_failure(make_settings) -> None:
    class TamperedStorage(FakeRandomStorage):
        async def download_bytes(self, _object_key: str, **_kwargs: object) -> bytes:
            return b"tampered"

    repository = RecordingRandomRepository()
    bot = FakeBot(
        make_settings(),
        FakeStatusService(),
        repository=repository,
        storage=TamperedStorage(),
    )
    interaction = random_interaction()

    await PhaseFourCommands.random_asset.callback(PhaseFourCommands(bot), interaction)

    assert repository.publication_updates[-1]["status"] == "failed"
    assert str(repository.publication_updates[-1]["last_error"]).startswith(
        "integrity_verification_failed:"
    )
    assert "integrity verification" in interaction.followup.messages[0]["args"][0]


async def test_random_command_message_construction_failure_is_not_a_discord_rejection(
    make_settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_construction(_context: object) -> str:
        raise DiscordPublicationMessageError("message could not be bounded")

    monkeypatch.setattr("app.bot.commands.build_publication_message", fail_construction)
    repository = RecordingRandomRepository()
    bot = FakeBot(
        make_settings(),
        FakeStatusService(),
        repository=repository,
        storage=FakeRandomStorage(),
    )
    interaction = random_interaction()

    await PhaseFourCommands.random_asset.callback(PhaseFourCommands(bot), interaction)

    update = repository.publication_updates[-1]
    assert update["status"] == "failed"
    assert str(update["last_error"]).startswith("message_construction_failed:")
    assert interaction.followup.attempts
    assert all("file" not in attempt for attempt in interaction.followup.attempts)
    assert "could not be formatted" in interaction.followup.messages[0]["args"][0]


async def test_random_command_definitive_discord_rejection(make_settings) -> None:
    repository = RecordingRandomRepository()
    bot = FakeBot(
        make_settings(),
        FakeStatusService(),
        repository=repository,
        storage=FakeRandomStorage(),
    )
    interaction = random_interaction(discord_http_error(400))

    await PhaseFourCommands.random_asset.callback(PhaseFourCommands(bot), interaction)

    update = repository.publication_updates[-1]
    assert update["status"] == "failed"
    assert str(update["last_error"]).startswith("discord_publication_rejected:")
    assert "could not be published to Discord" in interaction.followup.messages[0]["args"][0]
    attempt_file = interaction.followup.attempts[0]["file"]
    assert isinstance(attempt_file, discord.File)
    assert attempt_file.fp.closed


async def test_random_command_uncertain_send_reconciles_existing_marker(
    make_settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = RecordingRandomRepository()
    bot = FakeBot(
        make_settings(),
        FakeStatusService(),
        repository=repository,
        storage=FakeRandomStorage(),
    )
    interaction = random_interaction(RuntimeError("transport response lost"))
    mark_publication_lookup(monkeypatch, message_id=321, conclusive=True)

    await PhaseFourCommands.random_asset.callback(PhaseFourCommands(bot), interaction)

    assert len(interaction.followup.attempts) == 1
    assert interaction.followup.messages == []
    assert repository.fail_published_update is False
    assert repository.publication_updates[-1]["status"] == "published"
    assert repository.publication_updates[-1]["message_id"] == 321


async def test_random_command_uncertain_send_conclusive_miss_is_failed(
    make_settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = RecordingRandomRepository()
    bot = FakeBot(
        make_settings(),
        FakeStatusService(),
        repository=repository,
        storage=FakeRandomStorage(),
    )
    interaction = random_interaction(RuntimeError("transport response lost"))
    mark_publication_lookup(monkeypatch, message_id=None, conclusive=True)

    await PhaseFourCommands.random_asset.callback(PhaseFourCommands(bot), interaction)

    assert len(interaction.followup.attempts) == 2
    assert "file" in interaction.followup.attempts[0]
    assert "file" not in interaction.followup.attempts[1]
    update = repository.publication_updates[-1]
    assert update["status"] == "failed"
    assert str(update["last_error"]).startswith("discord_publication_not_delivered:")
    assert "could not be published to Discord" in interaction.followup.messages[0]["args"][0]


async def test_random_command_uncertain_send_inconclusive_stays_pending(
    make_settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = RecordingRandomRepository()
    bot = FakeBot(
        make_settings(),
        FakeStatusService(),
        repository=repository,
        storage=FakeRandomStorage(),
    )
    interaction = random_interaction(discord_http_error(429))
    mark_publication_lookup(monkeypatch, message_id=None, conclusive=False)

    await PhaseFourCommands.random_asset.callback(PhaseFourCommands(bot), interaction)

    assert len(interaction.followup.attempts) == 2
    assert "file" in interaction.followup.attempts[0]
    assert "file" not in interaction.followup.attempts[1]
    update = repository.publication_updates[-1]
    assert update["status"] == "pending"
    assert str(update["last_error"]).startswith("discord_publication_ambiguous:")
    assert "outcome is uncertain" in interaction.followup.messages[0]["args"][0]


async def test_random_command_persistence_failure_never_marks_delivery_failed(
    make_settings,
) -> None:
    repository = RecordingRandomRepository()
    repository.fail_published_update = True
    bot = FakeBot(
        make_settings(),
        FakeStatusService(),
        repository=repository,
        storage=FakeRandomStorage(),
    )
    interaction = random_interaction()

    await PhaseFourCommands.random_asset.callback(PhaseFourCommands(bot), interaction)

    assert len(interaction.followup.attempts) == 1
    assert len(interaction.followup.messages) == 1
    assert not any(
        "could not" in str(message["args"][0]) for message in interaction.followup.messages
    )
    assert [update["status"] for update in repository.publication_updates] == [
        "published",
        "pending",
    ]
    assert str(repository.publication_updates[-1]["last_error"]).startswith(
        "publication_persistence_failed:"
    )


async def test_random_command_ambiguous_publish_commit_is_reconciled(
    make_settings,
) -> None:
    repository = RecordingRandomRepository()
    repository.fail_published_update_after_commit = True
    bot = FakeBot(
        make_settings(),
        FakeStatusService(),
        repository=repository,
        storage=FakeRandomStorage(),
    )
    interaction = random_interaction()

    await PhaseFourCommands.random_asset.callback(PhaseFourCommands(bot), interaction)

    assert len(interaction.followup.attempts) == 1
    assert len(interaction.followup.messages) == 1
    assert [update["status"] for update in repository.publication_updates] == ["published"]
    publication = repository.publications["publication-random"]
    assert publication["status"] == "published"
    assert publication["message_id"] == 999
    assert publication["published_at"] == "2026-11-15T00:00:00+00:00"


async def test_random_command_ambiguous_publish_without_reconciliation_does_not_downgrade(
    make_settings,
) -> None:
    repository = RecordingRandomRepository()
    repository.fail_published_update_after_commit = True
    repository.fail_publication_reconciliation = True
    bot = FakeBot(
        make_settings(),
        FakeStatusService(),
        repository=repository,
        storage=FakeRandomStorage(),
    )
    interaction = random_interaction()

    await PhaseFourCommands.random_asset.callback(PhaseFourCommands(bot), interaction)

    assert [update["status"] for update in repository.publication_updates] == ["published"]
    publication = repository.publications["publication-random"]
    assert publication["status"] == "published"
    assert publication["message_id"] == 999
    assert publication["published_at"] == "2026-11-15T00:00:00+00:00"


async def test_random_command_marker_reconciliation_preserves_committed_publish(
    make_settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = RecordingRandomRepository()
    repository.fail_published_update_after_commit = True
    bot = FakeBot(
        make_settings(),
        FakeStatusService(),
        repository=repository,
        storage=FakeRandomStorage(),
    )
    interaction = random_interaction(RuntimeError("transport response lost"))
    mark_publication_lookup(monkeypatch, message_id=321, conclusive=True)

    await PhaseFourCommands.random_asset.callback(PhaseFourCommands(bot), interaction)

    assert len(interaction.followup.attempts) == 1
    assert interaction.followup.messages == []
    assert [update["status"] for update in repository.publication_updates] == ["published"]
    publication = repository.publications["publication-random"]
    assert publication["status"] == "published"
    assert publication["message_id"] == 321
    assert publication["published_at"] == "2026-11-15T00:00:00+00:00"


async def test_random_command_post_delivery_error_never_reports_failure(
    make_settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = RecordingRandomRepository()
    bot = FakeBot(
        make_settings(),
        FakeStatusService(),
        repository=repository,
        storage=FakeRandomStorage(),
    )
    cog = PhaseFourCommands(bot)
    interaction = random_interaction()

    async def broken_persistence(_publication_id: str, _message_id: int) -> bool:
        raise RuntimeError("local post-delivery failure")

    monkeypatch.setattr(cog, "_persist_random_published", broken_persistence)

    await PhaseFourCommands.random_asset.callback(cog, interaction)

    assert len(interaction.followup.attempts) == 1
    assert len(interaction.followup.messages) == 1
    assert not any(
        "could not" in str(message["args"][0]) for message in interaction.followup.messages
    )
    assert repository.publication_updates[-1]["status"] == "pending"
    assert str(repository.publication_updates[-1]["last_error"]).startswith(
        "publication_persistence_failed:"
    )


async def test_random_command_cancellation_before_send_is_failed_and_propagates(
    make_settings,
) -> None:
    repository = RecordingRandomRepository()
    bot = FakeBot(
        make_settings(),
        FakeStatusService(),
        repository=repository,
        storage=FailingRandomStorage(asyncio.CancelledError()),
    )
    interaction = random_interaction()

    with pytest.raises(asyncio.CancelledError):
        await PhaseFourCommands.random_asset.callback(PhaseFourCommands(bot), interaction)

    assert repository.publication_updates[-1]["status"] == "failed"
    assert str(repository.publication_updates[-1]["last_error"]).startswith(
        "command_cancelled_before_delivery:"
    )
    assert interaction.followup.attempts == []


async def test_random_command_cancellation_during_send_stays_pending(
    make_settings,
) -> None:
    repository = RecordingRandomRepository()
    bot = FakeBot(
        make_settings(),
        FakeStatusService(),
        repository=repository,
        storage=FakeRandomStorage(),
    )
    interaction = random_interaction(asyncio.CancelledError())

    with pytest.raises(asyncio.CancelledError):
        await PhaseFourCommands.random_asset.callback(PhaseFourCommands(bot), interaction)

    assert repository.publication_updates[-1]["status"] == "pending"
    assert str(repository.publication_updates[-1]["last_error"]).startswith(
        "discord_publication_ambiguous:"
    )
    attempt_file = interaction.followup.attempts[0]["file"]
    assert isinstance(attempt_file, discord.File)
    assert attempt_file.fp.closed


async def test_random_command_reports_no_eligible_media_without_publication(
    make_settings,
) -> None:
    class EmptyRepository(RecordingRandomRepository):
        async def random_asset(self) -> dict[str, object] | None:
            return None

    repository = EmptyRepository()
    bot = FakeBot(
        make_settings(),
        FakeStatusService(),
        repository=repository,
        storage=FakeRandomStorage(),
    )
    interaction = random_interaction()

    await PhaseFourCommands.random_asset.callback(PhaseFourCommands(bot), interaction)

    assert repository.publication_updates == []
    assert "No verified archived media" in interaction.followup.messages[0]["args"][0]


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
