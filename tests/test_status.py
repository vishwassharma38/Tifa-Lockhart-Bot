from __future__ import annotations

from app.health.status import (
    COLLECTION_NOT_IMPLEMENTED,
    DependencyStatus,
    ServiceState,
)
from app.status import StatusService


class FakeDependency:
    def __init__(
        self,
        status: DependencyStatus,
        *,
        error: Exception | None = None,
    ) -> None:
        self.status = status
        self.error = error
        self.timeouts: list[float | None] = []

    async def verify(self, timeout: float | None = None) -> DependencyStatus:
        self.timeouts.append(timeout)
        if self.error is not None:
            raise self.error
        return self.status


def test_dependency_labels_are_distinct() -> None:
    assert DependencyStatus.not_configured().label == "Not configured"
    assert DependencyStatus.not_checked().label == "Configured, not checked"
    assert DependencyStatus.verified().label == "Connected / verified"
    assert DependencyStatus.unavailable().label == "Unavailable"
    assert DependencyStatus.not_configured().configured is False
    assert DependencyStatus.not_checked().configured is True


async def test_status_service_collects_truthful_dependency_states(make_settings) -> None:
    settings = make_settings(APP_VERSION="4.5.6", APP_ENV="staging")
    database = FakeDependency(DependencyStatus.verified())
    storage = FakeDependency(DependencyStatus.unavailable())
    service = StatusService(settings, database, storage, check_timeout=3.0)

    report = await service.collect(discord_connected=True)

    assert report.discord_connected is True
    assert report.discord_label == "Connected"
    assert report.app_version == "4.5.6"
    assert report.app_env == "staging"
    assert report.supabase.state is ServiceState.VERIFIED
    assert report.r2.state is ServiceState.UNAVAILABLE
    assert report.collection_state == COLLECTION_NOT_IMPLEMENTED
    assert database.timeouts == [3.0]
    assert storage.timeouts == [3.0]


async def test_status_service_converts_check_exceptions_to_unavailable(make_settings) -> None:
    settings = make_settings()
    database = FakeDependency(
        DependencyStatus.verified(),
        error=RuntimeError("database exploded with a secret-looking detail"),
    )
    storage = FakeDependency(DependencyStatus.not_configured())
    service = StatusService(settings, database, storage)

    report = await service.collect(discord_connected=False)

    assert report.discord_connected is False
    assert report.discord_label == "Disconnected / starting"
    assert report.supabase.state is ServiceState.UNAVAILABLE
    assert report.r2.state is ServiceState.NOT_CONFIGURED
