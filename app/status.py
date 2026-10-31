"""Aggregate truthful status information for the Discord command layer."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable
from typing import TYPE_CHECKING

from app.config import Settings
from app.health.status import DependencyStatus, StatusReport

if TYPE_CHECKING:
    from app.db.client import SupabaseDatabase
    from app.storage.r2 import R2Storage

log = logging.getLogger(__name__)


class StatusService:
    """Check configured dependencies on demand with bounded waits."""

    def __init__(
        self,
        settings: Settings,
        database: SupabaseDatabase,
        storage: R2Storage,
        *,
        check_timeout: float = 5.0,
    ) -> None:
        self._settings = settings
        self._database = database
        self._storage = storage
        self._check_timeout = check_timeout

    async def collect(self, *, discord_connected: bool) -> StatusReport:
        supabase, r2 = await asyncio.gather(
            self._safe_check(self._database.verify(timeout=self._check_timeout)),
            self._safe_check(self._storage.verify(timeout=self._check_timeout)),
        )
        return StatusReport(
            discord_connected=discord_connected,
            app_version=self._settings.app_version,
            app_env=self._settings.app_env,
            supabase=supabase,
            r2=r2,
        )

    @staticmethod
    async def _safe_check(check: Awaitable[DependencyStatus]) -> DependencyStatus:
        try:
            return await check
        except TimeoutError:
            log.warning("Dependency status check timed out")
        except Exception as exc:
            log.warning("Dependency status check failed (%s)", type(exc).__name__)
        return DependencyStatus.unavailable()
