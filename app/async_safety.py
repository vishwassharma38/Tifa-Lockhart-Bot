"""Small cancellation-safe helpers for bounded shutdown cleanup."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable
from typing import TypeVar

log = logging.getLogger(__name__)

T = TypeVar("T")


async def await_cancellation_safe(
    awaitable: Awaitable[T],
    *,
    timeout: float,
) -> tuple[bool, T | None, BaseException | None]:
    """Await cleanup work even if the calling task is cancelled again.

    Repeated cancellation must not abandon a database requeue or publication
    state update that is already in flight. The helper shields the cleanup task,
    keeps waiting through repeated ``CancelledError`` deliveries, and gives up
    after a bounded timeout so shutdown cannot hang indefinitely.
    """
    task = asyncio.ensure_future(awaitable)
    deadline = time.monotonic() + max(0.0, timeout)
    while not task.done():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            task.cancel()
            try:
                await asyncio.wait({task}, timeout=0)
            except asyncio.CancelledError:
                pass
            log.error("Cleanup exceeded %.1f second(s)", timeout)
            return False, None, TimeoutError("cleanup timed out")
        try:
            await asyncio.wait({task}, timeout=remaining)
        except asyncio.CancelledError:
            # The caller was cancelled again. Keep the cleanup task alive and
            # continue waiting until it finishes or the bounded deadline passes.
            continue
    if task.cancelled():
        return False, None, asyncio.CancelledError()
    error = task.exception()
    if error is not None:
        return False, None, error
    return True, task.result(), None
