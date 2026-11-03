"""Supabase client initialization and bounded health checks."""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from supabase import create_client

from app.config import Settings
from app.health.status import DependencyStatus

log = logging.getLogger(__name__)


class DatabaseInitializationError(RuntimeError):
    """Raised when a configured Supabase client cannot be created."""


class SupabaseDatabase:
    """Owns the Supabase client while keeping sync SDK calls off the event loop."""

    def __init__(
        self,
        url: str,
        service_role_key: str,
        *,
        client_factory: Callable[..., Any] = create_client,
        to_thread: Callable[..., Awaitable[Any]] = asyncio.to_thread,
        verify_timeout: float = 5.0,
    ) -> None:
        self._url = url
        self._service_role_key = service_role_key
        self._client_factory = client_factory
        self._to_thread = to_thread
        self._verify_timeout = verify_timeout
        self._client: Any | None = None

    @classmethod
    def from_settings(cls, settings: Settings) -> SupabaseDatabase:
        return cls(settings.supabase_url, settings.supabase_service_role_key)

    @property
    def configured(self) -> bool:
        return bool(self._url and self._service_role_key)

    @property
    def client(self) -> Any:
        """Return the initialized client or fail without exposing configuration."""
        if self._client is None:
            raise DatabaseInitializationError("Supabase client is not initialized")
        return self._client

    def initialize(self) -> bool:
        """Create the client if configured. Returns whether a client exists."""
        if not self.configured:
            return False
        if self._client is not None:
            return True
        try:
            self._client = self._client_factory(self._url, self._service_role_key)
        except Exception as exc:
            raise DatabaseInitializationError("Supabase client initialization failed") from exc
        return True

    async def verify(self, timeout: float | None = None) -> DependencyStatus:
        """Run a lightweight query and report a truthful dependency state."""
        if not self.configured:
            return DependencyStatus.not_configured()
        if self._client is None:
            try:
                self.initialize()
            except DatabaseInitializationError:
                return DependencyStatus.unavailable()

        try:
            await asyncio.wait_for(
                self._to_thread(self._query_runtime_state),
                timeout=timeout if timeout is not None else self._verify_timeout,
            )
        except TimeoutError:
            log.warning("Supabase health check timed out")
            return DependencyStatus.unavailable()
        except Exception as exc:
            log.warning("Supabase health check failed (%s)", type(exc).__name__)
            return DependencyStatus.unavailable()
        return DependencyStatus.verified()

    def _query_runtime_state(self) -> Any:
        client = self._client
        if client is None:
            raise DatabaseInitializationError("Supabase client is not initialized")
        return client.table("runtime_state").select("id").limit(1).execute()

    async def close(self) -> None:
        """Close the SDK transport when supported and release the client."""
        client = self._client
        self._client = None
        if client is None:
            return

        for method_name in ("aclose", "close"):
            method = getattr(client, method_name, None)
            if not callable(method):
                continue
            try:
                result = method()
                if inspect.isawaitable(result):
                    await result
            except Exception as exc:
                log.debug("Ignoring Supabase close error (%s)", type(exc).__name__)
            return
