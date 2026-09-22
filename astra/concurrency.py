"""Loop-agnostic concurrency primitives shared by the tool runtime, the
LLM gate and the delegate fan-out caps.

Why not just asyncio.Semaphore?  Astra is a *library*: callers run it
from FastAPI (one long-lived loop), from Django/Frappe sync code via
``asyncio.run()`` (a NEW loop per call), or from several threads at
once.  An ``asyncio.Semaphore`` is bound to one event loop, so a
process-wide instance breaks the moment two loops touch it.  ``Limiter``
guards its counters with a ``threading.Lock`` and hands slots to waiters
through ``loop.call_soon_threadsafe`` — safe across loops and threads.

Stdlib only; works on Python 3.10+.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections import deque
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Deque, Optional, Tuple


class LimiterBusy(Exception):
    """Raised when a slot could not be obtained (queue full / wait timed out)."""


class Limiter:
    """A counting limiter with a bounded FIFO wait queue."""

    def __init__(self, limit: int, *, max_queue: Optional[int] = None, name: str = "") -> None:
        if limit < 1:
            raise ValueError("limit must be >= 1")
        self.name = name
        self._limit = limit
        self._max_queue = max_queue
        self._lock = threading.Lock()
        self._active = 0
        self._waiters: Deque[Tuple[asyncio.AbstractEventLoop, "asyncio.Future[None]"]] = deque()

    # ── introspection ────────────────────────────────────────────
    @property
    def limit(self) -> int:
        return self._limit

    @property
    def active(self) -> int:
        return self._active

    @property
    def waiting(self) -> int:
        return len(self._waiters)

    # ── acquire / release ────────────────────────────────────────
    def try_acquire(self) -> bool:
        with self._lock:
            if self._active < self._limit and not self._waiters:
                self._active += 1
                return True
            return False

    async def acquire(self, timeout: Optional[float] = None) -> None:
        with self._lock:
            if self._active < self._limit and not self._waiters:
                self._active += 1
                return
            if self._max_queue is not None and len(self._waiters) >= self._max_queue:
                raise LimiterBusy(f"{self.name or 'limiter'}: queue full ({self._max_queue} waiting)")
            loop = asyncio.get_running_loop()
            fut: "asyncio.Future[None]" = loop.create_future()
            entry = (loop, fut)
            self._waiters.append(entry)
        try:
            if timeout is None:
                await fut
            else:
                await asyncio.wait_for(fut, timeout)
        except BaseException as exc:
            with self._lock:
                try:
                    self._waiters.remove(entry)
                    removed = True
                except ValueError:
                    removed = False
            if not removed and fut.done() and not fut.cancelled():
                # The slot was handed to us but we are bailing out: give it back.
                self.release()
            if isinstance(exc, asyncio.TimeoutError):
                raise LimiterBusy(f"{self.name or 'limiter'}: timed out after {timeout}s waiting for a slot") from None
            raise

    def release(self) -> None:
        with self._lock:
            while self._waiters:
                loop, fut = self._waiters.popleft()
                try:
                    loop.call_soon_threadsafe(self._grant, fut)
                except RuntimeError:  # that loop is closed — skip this waiter
                    continue
                return  # slot handed over; active count unchanged
            if self._active > 0:
                self._active -= 1

    def _grant(self, fut: "asyncio.Future[None]") -> None:
        if fut.done():  # waiter was cancelled while the grant was in flight
            self.release()
        else:
            fut.set_result(None)

    @asynccontextmanager
    async def slot(self, timeout: Optional[float] = None) -> AsyncIterator[None]:
        await self.acquire(timeout)
        try:
            yield
        finally:
            self.release()


class KeyedLimiters:
    """Lazily-created Limiter per key (tenant id, tool name, ...)."""

    def __init__(self, limit: int, *, max_queue: Optional[int] = None, name: str = "") -> None:
        self._limit = limit
        self._max_queue = max_queue
        self._name = name
        self._lock = threading.Lock()
        self._items: dict[str, Limiter] = {}

    def get(self, key: str, limit: Optional[int] = None) -> Limiter:
        with self._lock:
            lim = self._items.get(key)
            if lim is None:
                lim = Limiter(limit or self._limit, max_queue=self._max_queue, name=f"{self._name}:{key}")
                self._items[key] = lim
            return lim

    def snapshot(self) -> dict[str, dict[str, int]]:
        with self._lock:
            return {k: {"active": v.active, "waiting": v.waiting, "limit": v.limit} for k, v in self._items.items()}


class CircuitBreaker:
    """Classic closed → open → half-open breaker (thread-safe)."""

    CLOSED, OPEN, HALF_OPEN = "closed", "open", "half_open"

    def __init__(self, failure_threshold: int = 5, cooldown: float = 30.0, clock=time.monotonic) -> None:
        self.failure_threshold = failure_threshold
        self.cooldown = cooldown
        self._clock = clock
        self._lock = threading.Lock()
        self._failures = 0
        self._opened_at: Optional[float] = None
        self._probe_in_flight = False

    @property
    def state(self) -> str:
        with self._lock:
            return self._state_locked()

    def _state_locked(self) -> str:
        if self._opened_at is None:
            return self.CLOSED
        if self._clock() - self._opened_at >= self.cooldown:
            return self.HALF_OPEN
        return self.OPEN

    def allow(self) -> bool:
        with self._lock:
            st = self._state_locked()
            if st == self.CLOSED:
                return True
            if st == self.HALF_OPEN and not self._probe_in_flight:
                self._probe_in_flight = True  # exactly one probe
                return True
            return False

    def record_success(self) -> None:
        with self._lock:
            self._failures = 0
            self._opened_at = None
            self._probe_in_flight = False

    def record_failure(self) -> None:
        with self._lock:
            self._probe_in_flight = False
            self._failures += 1
            if self._failures >= self.failure_threshold or self._opened_at is not None:
                self._opened_at = self._clock()

    def retry_after(self) -> float:
        with self._lock:
            if self._opened_at is None:
                return 0.0
            return max(0.0, self.cooldown - (self._clock() - self._opened_at))


def snapshot_limiter(lim: Limiter) -> dict[str, Any]:
    return {"active": lim.active, "waiting": lim.waiting, "limit": lim.limit}
