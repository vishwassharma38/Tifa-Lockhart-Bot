from __future__ import annotations

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


class FakeFollowup:
    def __init__(self) -> None:
        self.messages: list[dict[str, object]] = []

    async def send(self, *args: object, **kwargs: object) -> None:
        self.messages.append({"args": args, **kwargs})


class FakeInteraction:
    def __init__(self, user_id: int = 1) -> None:
        self.response = FakeResponse()
        self.followup = FakeFollowup()
        self.user = type("User", (), {"id": user_id})()


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
    def __init__(self, settings, status_service: FakeStatusService) -> None:
        self.settings = settings
        self.status_service = status_service

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
