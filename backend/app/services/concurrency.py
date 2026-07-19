"""Per-instance concurrency gate for LLM-heavy screening runs (#15).

A screening's stream/approve path drives graph inference — expensive, and
unbounded concurrency lets a burst exhaust memory and starve every request.
This caps in-flight runs with a semaphore and *fails fast* (429 + Retry-After)
when saturated, rather than silently queueing callers behind a growing backlog.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, AsyncIterator, Iterator
from contextlib import contextmanager

from app.exceptions import TooManyActiveScreeningsError


class ConcurrencyLimiter:
    """A non-blocking bounded gate. `acquire()` raises immediately when full.

    A plain counter, not an ``asyncio.Semaphore``: the gate never *waits* (it
    fails fast instead of queueing), so the semaphore's blocking machinery is
    unused. The event loop is single-threaded and there is no ``await`` between
    the check and the increment, so the check-then-take is effectively atomic.
    """

    def __init__(self, limit: int, retry_after_seconds: int = 5) -> None:
        self._limit = limit
        self._active = 0
        self._retry_after = retry_after_seconds

    def acquire(self) -> None:
        """Take a slot, or raise 429 (with Retry-After) if none are free."""
        if self._active >= self._limit:
            raise TooManyActiveScreeningsError(
                "Too many screenings in progress; retry shortly.",
                headers={"Retry-After": str(self._retry_after)},
            )
        self._active += 1

    def release(self) -> None:
        # Clamp at zero so a stray double-release can never open the gate wider
        # than `limit` (a masked balance bug is safer than an unbounded one).
        self._active = max(0, self._active - 1)

    @contextmanager
    def slot(self) -> Iterator[None]:
        """Hold a slot for a synchronous critical section (non-streaming route)."""
        self.acquire()
        try:
            yield
        finally:
            self.release()


async def release_after(
    frames: AsyncIterator[str], limiter: ConcurrencyLimiter
) -> AsyncGenerator[str, None]:
    """Yield every frame, then release the (already-acquired) slot — even if the
    client disconnects mid-stream, since Starlette closes this generator and its
    `finally` runs. The caller must `acquire()` before building `frames` so a
    saturation 429 is raised before any response headers are sent.
    """
    try:
        async for frame in frames:
            yield frame
    finally:
        limiter.release()
