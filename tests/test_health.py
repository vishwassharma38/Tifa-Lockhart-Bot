from __future__ import annotations

from aiohttp.test_utils import TestClient, TestServer

from app.health.server import HealthServer, create_health_app


async def test_healthz_returns_minimal_ok_response() -> None:
    async with TestClient(TestServer(create_health_app())) as client:
        response = await client.get("/healthz")

        assert response.status == 200
        assert response.headers["Cache-Control"] == "no-store"
        assert await response.json() == {"status": "ok"}


async def test_unknown_path_returns_404() -> None:
    async with TestClient(TestServer(create_health_app())) as client:
        response = await client.get("/not-healthz")
        assert response.status == 404


async def test_health_server_starts_and_stops() -> None:
    server = HealthServer(port=0)
    assert server.running is False

    await server.start()
    assert server.running is True

    await server.stop()
    assert server.running is False
