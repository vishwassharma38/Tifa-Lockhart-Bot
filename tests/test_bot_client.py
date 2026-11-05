from __future__ import annotations

import pytest

from app.bot import client as bot_client
from app.bot.client import TifaBot


class DummyStatusService:
    async def collect(self, **_kwargs: object) -> object:
        raise AssertionError("status service must not be called during setup")


def build_bot(make_settings, *, sync_commands: bool) -> TifaBot:
    return TifaBot(make_settings(), DummyStatusService(), sync_commands=sync_commands)


async def test_setup_hook_registers_phase_one_commands(make_settings) -> None:
    bot = build_bot(make_settings, sync_commands=False)
    try:
        await bot.setup_hook()
        names = {command.name for command in bot.tree.get_commands()}
        assert names == {"about", "status", "start", "stop", "save", "random", "stats"}
    finally:
        await bot.close()


async def test_setup_hook_syncs_globally_when_no_guild_is_configured(
    make_settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[int | None] = []

    async def fake_sync(_tree: object, guild_id: int | None) -> int:
        calls.append(guild_id)
        return 2

    monkeypatch.setattr(bot_client, "sync_application_commands", fake_sync)
    bot = build_bot(make_settings, sync_commands=True)
    try:
        await bot.setup_hook()
    finally:
        await bot.close()

    assert calls == [None]


async def test_setup_hook_sync_uses_guild_when_configured(
    make_settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[int | None] = []

    async def fake_sync(_tree: object, guild_id: int | None) -> int:
        calls.append(guild_id)
        return 2

    monkeypatch.setattr(bot_client, "sync_application_commands", fake_sync)
    bot = build_bot(
        lambda: make_settings(DISCORD_GUILD_ID="123456789"),
        sync_commands=True,
    )
    try:
        await bot.setup_hook()
    finally:
        await bot.close()

    assert calls == [123456789]


async def test_setup_hook_survives_command_sync_failure(
    make_settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def failing_sync(_tree: object, _guild_id: int | None) -> int:
        raise RuntimeError("discord command sync failed")

    monkeypatch.setattr(bot_client, "sync_application_commands", failing_sync)
    bot = build_bot(make_settings, sync_commands=True)
    try:
        await bot.setup_hook()
        names = {command.name for command in bot.tree.get_commands()}
        assert names == {"about", "status", "start", "stop", "save", "random", "stats"}
    finally:
        await bot.close()
