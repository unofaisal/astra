"""In-memory Store — zero persistence, useful for tests / ephemeral runs
or as a building block for a user-supplied Store."""

from __future__ import annotations

import threading

from .base import Session


class MemoryStore:
    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}
        self._lock = threading.Lock()

    def get(self, session_id: str) -> Session | None:
        with self._lock:
            return self._sessions.get(session_id)

    def save(self, session: Session) -> None:
        with self._lock:
            self._sessions[session.session_id] = session

    def delete(self, session_id: str) -> None:
        with self._lock:
            self._sessions.pop(session_id, None)

    def list_sessions(self, user: str | None = None, limit: int = 50, offset: int = 0) -> list[Session]:
        with self._lock:
            rows = [s for s in self._sessions.values() if user is None or s.user == user]
        rows.sort(key=lambda s: s.last_active, reverse=True)
        return rows[offset : offset + limit]
