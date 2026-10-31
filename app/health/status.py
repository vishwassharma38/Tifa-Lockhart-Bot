"""Status models shared by dependency adapters and Discord commands."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

COLLECTION_NOT_IMPLEMENTED = "Not implemented in Phase 1"


class ServiceState(StrEnum):
    NOT_CONFIGURED = "not_configured"
    NOT_CHECKED = "not_checked"
    VERIFIED = "verified"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class DependencyStatus:
    state: ServiceState

    @property
    def configured(self) -> bool:
        return self.state is not ServiceState.NOT_CONFIGURED

    @property
    def label(self) -> str:
        return {
            ServiceState.NOT_CONFIGURED: "Not configured",
            ServiceState.NOT_CHECKED: "Configured, not checked",
            ServiceState.VERIFIED: "Connected / verified",
            ServiceState.UNAVAILABLE: "Unavailable",
        }[self.state]

    @classmethod
    def not_configured(cls) -> DependencyStatus:
        return cls(ServiceState.NOT_CONFIGURED)

    @classmethod
    def not_checked(cls) -> DependencyStatus:
        return cls(ServiceState.NOT_CHECKED)

    @classmethod
    def verified(cls) -> DependencyStatus:
        return cls(ServiceState.VERIFIED)

    @classmethod
    def unavailable(cls) -> DependencyStatus:
        return cls(ServiceState.UNAVAILABLE)


@dataclass(frozen=True, slots=True)
class StatusReport:
    discord_connected: bool
    app_version: str
    app_env: str
    supabase: DependencyStatus
    r2: DependencyStatus
    collection_state: str = COLLECTION_NOT_IMPLEMENTED

    @property
    def discord_label(self) -> str:
        return "Connected" if self.discord_connected else "Disconnected / starting"
