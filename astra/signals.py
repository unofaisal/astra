"""Cross-cutting run-control signals.

The original harness used Redis for the cooperative "stop" flag a
user's Stop button sets — checked at loop boundaries in Agent.run() —
which doesn't belong in the main session blob because it's a short-lived
/ racy control signal about a run in progress, not conversation content.

(Clarification pause/resume does NOT need one of these: it's resolved
entirely through the durable Store — see Conversation.resolve_clarification
— which is why there's no expiry to worry about there, unlike the stop
flag below.)

This is just "key -> value with a TTL". SignalStore is that protocol.
The default (InMemorySignalStore) is a plain dict with lock + TTL
checked on read — perfectly fine for a single-process app. Swap in a
Redis-backed implementation (same three methods) if you're running
multiple worker processes and need the stop flag shared across them.
"""

from __future__ import annotations

import threading
import time
from typing import Protocol


class SignalStore(Protocol):
    def set(self, key: str, value: str, ttl_seconds: float | None = None) -> None: ...

    def get(self, key: str) -> str | None: ...

    def delete(self, key: str) -> None: ...


class InMemorySignalStore:
    """Default SignalStore: an in-process dict guarded by a lock, with
    lazy TTL expiry checked on read. Not shared across processes.
    """

    def __init__(self) -> None:
        self._data: dict[str, tuple[str, float | None]] = {}  # key -> (value, expires_at|None)
        self._lock = threading.Lock()

    def set(self, key: str, value: str, ttl_seconds: float | None = None) -> None:
        expires_at = time.monotonic() + ttl_seconds if ttl_seconds else None
        with self._lock:
            self._data[key] = (value, expires_at)

    def get(self, key: str) -> str | None:
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                return None
            value, expires_at = entry
            if expires_at is not None and time.monotonic() > expires_at:
                del self._data[key]
                return None
            return value

    def delete(self, key: str) -> None:
        with self._lock:
            self._data.pop(key, None)


# ── Stop-flag helpers (thin convenience wrappers over SignalStore) ──

_STOP_PREFIX = "astra:stop:"
_STOP_TTL = 600  # seconds — generously beyond any realistic single-turn runtime


def request_stop(signals: SignalStore, session_id: str) -> None:
    signals.set(f"{_STOP_PREFIX}{session_id}", "1", ttl_seconds=_STOP_TTL)


def is_stop_requested(signals: SignalStore, session_id: str) -> bool:
    return bool(signals.get(f"{_STOP_PREFIX}{session_id}"))


def clear_stop_flag(signals: SignalStore, session_id: str) -> None:
    signals.delete(f"{_STOP_PREFIX}{session_id}")
