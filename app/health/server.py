"""Minimal HTTP health endpoint for Render and uptime monitors."""

from __future__ import annotations

import logging

from aiohttp import web

log = logging.getLogger(__name__)


async def healthz(_request: web.Request) -> web.Response:
    """Liveness endpoint; intentionally reveals no secrets or dependency details.

    The response describes process liveness only. Dependency health is exposed
    through the Discord ``/status`` command, never through this endpoint.
    """
    return web.json_response(
        {"status": "ok", "scope": "liveness"},
        headers={"Cache-Control": "no-store"},
    )


def create_health_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/healthz", healthz)
    return app


class HealthServer:
    """Owns the aiohttp runner and TCP site for the health endpoint."""

    def __init__(self, port: int, host: str = "0.0.0.0") -> None:
        self._port = port
        self._host = host
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None

    @property
    def running(self) -> bool:
        return self._runner is not None

    async def start(self) -> None:
        if self._runner is not None:
            return

        runner = web.AppRunner(create_health_app(), access_log=None, shutdown_timeout=5.0)
        try:
            await runner.setup()
            site = web.TCPSite(runner, self._host, self._port)
            await site.start()
        except BaseException:
            await runner.cleanup()
            raise

        self._runner = runner
        self._site = site
        log.info("Health endpoint listening on %s:%s/healthz", self._host, self._port)

    async def stop(self) -> None:
        runner = self._runner
        self._runner = None
        self._site = None
        if runner is not None:
            await runner.cleanup()
            log.info("Health endpoint stopped")
