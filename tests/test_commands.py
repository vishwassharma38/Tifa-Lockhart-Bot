from __future__ import annotations

from app.bot.client import sync_application_commands
from app.bot.commands import (
    ABOUT_PURPOSE,
    PhaseOneCommands,
    build_about_embed,
    build_status_embed,
)
from app.health.status import (
    COLLECTION_NOT_IMPLEMENTED,
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
    assert embed.url is None
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
    assert fields["Collection"] == COLLECTION_NOT_IMPLEMENTED


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
    assert fields["Collection"] == "Not implemented in Phase 1"


def test_phase_one_registers_only_status_and_about() -> None:
    names = {command.name for command in PhaseOneCommands.__cog_app_commands__}
    assert names == {"about", "status"}


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

    async def send_message(self, **kwargs: object) -> None:
        self.messages.append(kwargs)


class FakeFollowup:
    def __init__(self) -> None:
        self.messages: list[dict[str, object]] = []

    async def send(self, *args: object, **kwargs: object) -> None:
        self.messages.append({"args": args, **kwargs})


class FakeInteraction:
    def __init__(self) -> None:
        self.response = FakeResponse()
        self.followup = FakeFollowup()


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
    cog = PhaseOneCommands(FakeBot(settings, FakeStatusService()))
    interaction = FakeInteraction()

    await PhaseOneCommands.about.callback(cog, interaction)

    assert len(interaction.response.messages) == 1
    embed = interaction.response.messages[0]["embed"]
    assert embed.title == "Tifa Lockhart Bot"


async def test_status_command_defers_then_reports_dependency_state(make_settings) -> None:
    report = StatusReport(
        discord_connected=True,
        app_version="1.2.3",
        app_env="test",
        supabase=DependencyStatus.verified(),
        storage=DependencyStatus.verified(),
    )
    cog = PhaseOneCommands(FakeBot(make_settings(), FakeStatusService(report=report)))
    interaction = FakeInteraction()

    await PhaseOneCommands.status_command.callback(cog, interaction)

    assert interaction.response.deferred == [{"ephemeral": True, "thinking": True}]
    assert len(interaction.followup.messages) == 1
    message = interaction.followup.messages[0]
    assert message["ephemeral"] is True
    assert message["embed"].title == "Tifa Lockhart Bot - Status"


async def test_status_command_reports_failure_without_leaking_details(make_settings) -> None:
    error = RuntimeError("secret-looking failure detail")
    cog = PhaseOneCommands(FakeBot(make_settings(), FakeStatusService(error=error)))
    interaction = FakeInteraction()

    await PhaseOneCommands.status_command.callback(cog, interaction)

    assert len(interaction.followup.messages) == 1
    message = interaction.followup.messages[0]
    assert message["ephemeral"] is True
    assert "secret-looking failure detail" not in str(message)
