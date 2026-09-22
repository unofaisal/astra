"""Multi-user safety: per-session run guard + ownership checks.

Problem (measured): two concurrent runs on one session both load the same
copy, both append, last writer wins — the persisted history ends up
interleaved with a lost answer.  This happens with a double-click, two
browser tabs, or a webhook racing a user message.

``SessionGuard`` makes runs on a session mutually exclusive:
  * in-process — a lock table (works across event loops and threads);
  * across processes — when the ``SignalStore`` offers an atomic
    ``set_if_absent`` (Redis ``SET NX EX``), a lease with a heartbeat, so a
    crashed worker's lock expires instead of wedging the session.

A second run fails fast with ``SessionBusyError`` (HTTP 409 in the example
API) rather than queueing behind a possibly minutes-long run.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import uuid
from contextlib import asynccontextmanager
from typing import AsyncIterator, Optional

from .telemetry import metrics

logger = logging.getLogger(__name__)


class SessionBusyError(RuntimeError):
    def __init__(self, session_id: str) -> None:
        super().__init__(f"Session {session_id} already has a run in progress.")
        self.session_id = session_id


class SessionOwnershipError(PermissionError):
    pass


def check_owner(session_user: Optional[str], user: Optional[str], session_id: str = "") -> None:
    """Raise unless ``user`` owns the session.  ``user`` must be supplied."""
    if not user:
        raise SessionOwnershipError("A user identity is required to access this session.")
    if session_user != user:
        # Deliberately vague: don't confirm the session exists for someone else.
        raise SessionOwnershipError(f"Session {session_id} is not accessible to this user.")


class SessionGuard:
    def __init__(self, signals=None, *, ttl: float = 900.0, prefix: str = "astra:lock:") -> None:
        self.signals = signals
        self.ttl = ttl
        self.prefix = prefix
        self._lock = threading.Lock()
        self._held: dict[str, str] = {}  # session_id -> token (this process)

    def _distributed(self) -> bool:
        return self.signals is not None and hasattr(self.signals, "set_if_absent")

    def held_sessions(self) -> list[str]:
        with self._lock:
            return list(self._held)

    def is_locked(self, session_id: str) -> bool:
        with self._lock:
            return session_id in self._held

    @asynccontextmanager
    async def hold(self, session_id: str, *, wait: float = 0.0) -> AsyncIterator[None]:
        token = uuid.uuid4().hex
        deadline = asyncio.get_running_loop().time() + wait
        while True:
            if self._try_acquire(session_id, token):
                break
            if asyncio.get_running_loop().time() >= deadline:
                metrics.inc("astra_session_busy_total")
                raise SessionBusyError(session_id)
            await asyncio.sleep(0.05)

        heartbeat: Optional[asyncio.Task] = None
        if self._distributed():
            heartbeat = asyncio.get_running_loop().create_task(self._heartbeat(session_id, token))
        try:
            yield
        finally:
            if heartbeat is not None:
                heartbeat.cancel()
                try:
                    await heartbeat
                except BaseException:
                    pass
            self._release(session_id, token)

    # ── internals ────────────────────────────────────────────────
    def _try_acquire(self, session_id: str, token: str) -> bool:
        with self._lock:
            if session_id in self._held:
                return False
            self._held[session_id] = token
        if self._distributed():
            try:
                ok = self.signals.set_if_absent(self.prefix + session_id, token, self.ttl)
            except Exception:
                logger.exception("distributed session lock failed; falling back to local-only for %s", session_id)
                ok = True
            if not ok:
                with self._lock:
                    self._held.pop(session_id, None)
                return False
        return True

    def _release(self, session_id: str, token: str) -> None:
        with self._lock:
            if self._held.get(session_id) == token:
                del self._held[session_id]
        if self._distributed():
            try:
                if self.signals.get(self.prefix + session_id) == token:
                    self.signals.delete(self.prefix + session_id)
            except Exception:
                logger.exception("failed to release distributed lock for %s", session_id)

    async def _heartbeat(self, session_id: str, token: str) -> None:
        interval = max(1.0, self.ttl / 3)
        while True:
            await asyncio.sleep(interval)
            try:
                if self.signals.get(self.prefix + session_id) == token:
                    self.signals.set(self.prefix + session_id, token, self.ttl)
            except Exception:
                logger.exception("session lock heartbeat failed for %s", session_id)
