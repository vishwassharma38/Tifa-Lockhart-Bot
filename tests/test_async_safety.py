from __future__ import annotations

import asyncio
import time

import pytest

from app.async_safety import await_cancellation_safe


async def test_cancellation_safe_helper_returns_normal_result() -> None:
    async def cleanup() -> str:
        return "done"

    completed, result, error = await await_cancellation_safe(cleanup(), timeout=1)

    assert (completed, result, error) == (True, "done", None)


async def test_cancellation_safe_helper_reports_cleanup_errors() -> None:
    async def cleanup() -> None:
        raise RuntimeError("database unavailable")

    completed, result, error = await await_cancellation_safe(cleanup(), timeout=1)

    assert completed is False
    assert result is None
    assert isinstance(error, RuntimeError)


async def test_cancellation_safe_helper_survives_repeated_cancellation() -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    async def cleanup() -> str:
        started.set()
        await release.wait()
        return "requeued"

    task = asyncio.create_task(await_cancellation_safe(cleanup(), timeout=2))
    await asyncio.wait_for(started.wait(), timeout=1)

    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()

    release.set()
    completed, result, error = await task

    assert (completed, result, error) == (True, "requeued", None)


async def test_cancellation_safe_helper_is_bounded() -> None:
    release = asyncio.Event()

    async def cleanup() -> None:
        await release.wait()

    started = time.monotonic()
    completed, _result, error = await await_cancellation_safe(cleanup(), timeout=0.05)
    elapsed = time.monotonic() - started

    assert completed is False
    assert isinstance(error, TimeoutError)
    assert elapsed < 1.0


async def test_cancellation_safe_helper_reports_cancelled_cleanup_task() -> None:
    async def cleanup() -> None:
        raise asyncio.CancelledError()

    completed, _result, error = await await_cancellation_safe(cleanup(), timeout=1)

    assert completed is False
    assert isinstance(error, asyncio.CancelledError)


async def test_cancellation_safe_helper_rejects_negative_timeout_as_immediate() -> None:
    release = asyncio.Event()

    async def cleanup() -> None:
        await release.wait()

    with pytest.raises(TimeoutError):
        # The helper itself returns a timeout result rather than raising, so a
        # direct check documents the bounded behavior without hanging.
        completed, _result, error = await await_cancellation_safe(cleanup(), timeout=-1)
        assert completed is False
        raise error  # type: ignore[misc]
