"""Application startup, orchestration, and graceful shutdown."""

from __future__ import annotations

import asyncio
import inspect
import logging
import signal
from collections.abc import Callable
from contextlib import suppress
from typing import Any

from app.bot.client import TifaBot
from app.config import ConfigurationError, Settings
from app.db.client import SupabaseDatabase
from app.health.server import HealthServer
from app.logging_config import configure_logging
from app.status import StatusService
from app.storage.r2 import R2Storage

log = logging.getLogger(__name__)


class Application:
    """Owns the long-running resources for the single-process V1 runtime."""

    def __init__(
        self,
        settings: Settings,
        *,
        bot_factory: Callable[[], Any] | None = None,
        database: SupabaseDatabase | None = None,
        storage: R2Storage | None = None,
        health_server: Any | None = None,
        install_signal_handlers: bool = True,
    ) -> None:
        self.settings = settings
        self._database = database or SupabaseDatabase.from_settings(settings)
        self._storage = storage or R2Storage.from_settings(settings)
        self._status_service = StatusService(settings, self._database, self._storage)
        self._bot_factory = bot_factory or (lambda: TifaBot(settings, self._status_service))
        self._health_server = health_server or HealthServer(settings.port)
        self._install_signal_handlers_enabled = install_signal_handlers

        self._bot: Any | None = None
        self._bot_task: asyncio.Task[None] | None = None
        self._shutdown_event = asyncio.Event()
        self._started = False
        self._closed = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_signals: list[signal.Signals] = []
        self._previous_signal_handlers: dict[signal.Signals, Any] = {}

    async def run(self) -> None:
        try:
            await self.start()
            await self._wait_for_shutdown()
        finally:
            if self._started:
                await self.stop()

    async def start(self) -> None:
        if self._started:
            raise RuntimeError("Application is already running")

        self._closed = False
        configure_logging(self.settings.log_level, self.settings.secret_values)
        self.settings.validate_runtime()
        self.settings.validate_service_configuration()

        if self._install_signal_handlers_enabled:
            self._install_signal_handlers()

        log.info("Starting Tifa Lockhart Bot (%s)", self.settings.safe_summary())

        try:
            await self._health_server.start()
            self._initialize_optional_services()
            self._bot = self._bot_factory()
            self._bot_task = asyncio.create_task(
                self._bot.start(self.settings.discord_token),
                name="discord-client",
            )
        except BaseException:
            await self._stop_resources()
            raise

        self._started = True

    async def stop(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._shutdown_event.set()
        await self._stop_resources()

    def request_shutdown(self) -> None:
        if not self._shutdown_event.is_set():
            log.info("Shutdown requested")
        self._shutdown_event.set()

    async def _wait_for_shutdown(self) -> None:
        bot_task = self._bot_task
        if bot_task is None:
            return

        shutdown_task = asyncio.create_task(
            self._shutdown_event.wait(),
            name="shutdown-wait",
        )
        try:
            done, _pending = await asyncio.wait(
                {bot_task, shutdown_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if shutdown_task in done:
                log.info("Shutdown signal received")
            else:
                log.warning("Discord client stopped unexpectedly")
        finally:
            if not shutdown_task.done():
                shutdown_task.cancel()
                with suppress(asyncio.CancelledError):
                    await shutdown_task

        if bot_task.done():
            exception = bot_task.exception()
            if exception is not None:
                raise exception

    def _initialize_optional_services(self) -> None:
        for name, service in (
            ("Supabase", self._database),
            ("R2", self._storage),
        ):
            try:
                initialized = service.initialize()
            except Exception as exc:
                log.error(
                    "%s initialization failed (%s); continuing in degraded mode",
                    name,
                    type(exc).__name__,
                )
            else:
                if initialized:
                    log.info("%s client initialized", name)
                else:
                    log.warning("%s is not configured; continuing in degraded mode", name)

    async def _stop_resources(self) -> None:
        await self._close_bot()
        await self._close_service("Supabase", self._database.close)
        await self._close_service("R2", self._storage.close)
        try:
            await self._health_server.stop()
        except Exception as exc:
            log.debug("Ignoring health server close error (%s)", type(exc).__name__)
        self._restore_signal_handlers()
        log.info("Application shutdown complete")

    async def _close_bot(self) -> None:
        bot = self._bot
        task = self._bot_task

        if bot is not None:
            try:
                await bot.close()
            except Exception as exc:
                log.debug("Ignoring Discord close error (%s)", type(exc).__name__)

        if task is not None:
            if not task.done():
                try:
                    await asyncio.wait_for(asyncio.shield(task), timeout=10.0)
                except TimeoutError:
                    log.warning("Discord client did not close in time; cancelling it")
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task
                except asyncio.CancelledError:
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task
                except Exception:
                    pass
            if task.done():
                with suppress(asyncio.CancelledError, Exception):
                    task.exception()

    async def _close_service(self, name: str, close: Callable[[], Any]) -> None:
        try:
            result = close()
            if inspect.isawaitable(result):
                await result
        except Exception as exc:
            log.debug("Ignoring %s close error (%s)", name, type(exc).__name__)

    def _install_signal_handlers(self) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._loop = loop

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self.request_shutdown)
                self._loop_signals.append(sig)
            except (NotImplementedError, RuntimeError):
                try:
                    self._previous_signal_handlers[sig] = signal.getsignal(sig)
                    signal.signal(sig, self._handle_signal)
                except (ValueError, OSError):
                    pass

    def _handle_signal(self, _signum: int, _frame: Any) -> None:
        loop = self._loop
        if loop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(self.request_shutdown)

    def _restore_signal_handlers(self) -> None:
        loop = self._loop
        if loop is not None:
            for sig in self._loop_signals:
                with suppress(NotImplementedError, RuntimeError, ValueError):
                    loop.remove_signal_handler(sig)
        self._loop_signals.clear()

        for sig, handler in self._previous_signal_handlers.items():
            with suppress(ValueError, OSError, TypeError):
                signal.signal(sig, handler)
        self._previous_signal_handlers.clear()


def main() -> None:
    try:
        settings = Settings.from_env()
    except ConfigurationError as exc:
        configure_logging("CRITICAL")
        log.critical("Configuration error: %s", exc)
        raise SystemExit(2) from None
    except Exception as exc:
        configure_logging("CRITICAL")
        log.critical("Configuration could not be loaded (%s)", type(exc).__name__)
        raise SystemExit(2) from None

    try:
        asyncio.run(Application(settings).run())
    except ConfigurationError as exc:
        log.critical("Configuration error: %s", exc)
        raise SystemExit(2) from None
    except KeyboardInterrupt:
        log.info("Shutdown requested")
    except Exception:
        log.exception("Application stopped unexpectedly")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
