"""Concurrency gate (#15): bounded in-flight runs, fail-fast 429 + Retry-After."""

import pytest

from app.exceptions import TooManyActiveScreeningsError
from app.services.concurrency import ConcurrencyLimiter, release_after


async def test_acquire_up_to_limit_then_raises():
    limiter = ConcurrencyLimiter(limit=2, retry_after_seconds=7)
    limiter.acquire()
    limiter.acquire()
    with pytest.raises(TooManyActiveScreeningsError) as exc_info:
        limiter.acquire()
    # 429 carries a Retry-After so the client knows to back off.
    assert exc_info.value.http_status == 429
    assert exc_info.value.headers["Retry-After"] == "7"


async def test_release_frees_a_slot():
    limiter = ConcurrencyLimiter(limit=1)
    limiter.acquire()
    limiter.release()
    limiter.acquire()  # slot is free again — no raise


async def test_slot_context_manager_releases_on_exit():
    limiter = ConcurrencyLimiter(limit=1)
    with limiter.slot():
        with pytest.raises(TooManyActiveScreeningsError):
            limiter.acquire()
    limiter.acquire()  # released on context exit


async def test_slot_releases_even_on_exception():
    limiter = ConcurrencyLimiter(limit=1)
    with pytest.raises(ValueError):
        with limiter.slot():
            raise ValueError("boom")
    limiter.acquire()  # slot was released despite the error


async def test_release_after_frees_slot_when_stream_drains():
    limiter = ConcurrencyLimiter(limit=1)
    limiter.acquire()

    async def frames():
        yield "a"
        yield "b"

    collected = [f async for f in release_after(frames(), limiter)]
    assert collected == ["a", "b"]
    limiter.acquire()  # released after the generator finished


async def test_release_after_frees_slot_on_early_close():
    """A client disconnecting mid-stream → generator closed → slot freed."""
    limiter = ConcurrencyLimiter(limit=1)
    limiter.acquire()

    async def frames():
        yield "a"
        yield "b"

    gen = release_after(frames(), limiter)
    assert await gen.__anext__() == "a"
    await gen.aclose()  # simulate Starlette closing on client disconnect
    limiter.acquire()  # slot freed by the finally
