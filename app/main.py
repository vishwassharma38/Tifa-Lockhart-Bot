"""Initial application entry point: Discord gateway + HTTP health endpoint."""

from __future__ import annotations

import asyncio
import logging
import signal
from contextlib import suppress

import aiohttp
from aiohttp import web
import discord
from discord.ext import commands

from app.config import Settings
from app.logging_config import configure_logging

log = logging.getLogger(__name__)


async def healthz(_request: web.Request) -> web.Response:
    """Liveness endpoint; intentionally reveals no secrets or dependency details."""
    return web.json_response({"status": "ok"})


async def start_health_server(port: int) -> tuple[web.AppRunner, web.TCPSite]:
    app = web.Application()
    app.router.add_get("/healthz", healthz)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=port)
    await site.start()
    log.info("Health endpoint listening on 0.0.0.0:%s/healthz", port)
    return runner, site


async def run() -> None:
    settings = Settings.from_env()
    settings.validate_runtime()
    configure_logging(settings.log_level)

    intents = discord.Intents.default()
    bot = commands.Bot(command_prefix="!", intents=intents)
    runner, _site = await start_health_server(settings.port)

    try:
        async with bot:
            await bot.start(settings.discord_token)
    finally:
        await runner.cleanup()


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        log.info("Shutdown requested")


if __name__ == "__main__":
    main()
