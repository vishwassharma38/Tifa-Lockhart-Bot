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
