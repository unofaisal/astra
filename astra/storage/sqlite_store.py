"""SQLite-backed Store — the default, zero-setup persistence layer.

One file (or ':memory:'), one table. The full Session is stored as a
JSON blob in `data`; a handful of columns are duplicated out of it so
you can query/filter cheaply (list by user, sort by last_active) without
deserializing every row. This mirrors the "denormalize onto the parent
doc for cheap dashboard queries" pattern in the original Frappe
Conversation._checkpoint, just without a full ORM.

Safe for a single process. If you need multi-process/multi-worker
access, point this at a real Postgres-backed Store instead (implement
the same four methods — see astra.storage.base.Store) or use SQLite's
WAL mode + your own locking discipline.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

from .base import Session

_SCHEMA = """
CREATE TABLE IF NOT EXISTS astra_sessions (
    session_id TEXT PRIMARY KEY,
    user TEXT NOT NULL,
    last_active REAL NOT NULL,
    ended_reason TEXT,
    data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_astra_sessions_user ON astra_sessions(user);
CREATE INDEX IF NOT EXISTS idx_astra_sessions_last_active ON astra_sessions(last_active);
"""


class SQLiteStore:
    def __init__(self, path: str | Path = "astra_sessions.db") -> None:
        self._path = str(path)
        self._lock = threading.Lock()
        # check_same_thread=False: astra's Agent loop is async but callers
        # may run it from different threads (e.g. a thread-pool executor
        # around asyncio.run). All access is still serialized by _lock.
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def get(self, session_id: str) -> Session | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT data FROM astra_sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
        if not row:
            return None
        return Session.from_dict(json.loads(row[0]))

    def save(self, session: Session) -> None:
        payload = json.dumps(session.to_dict(), default=str)
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO astra_sessions (session_id, user, last_active, ended_reason, data)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    user=excluded.user,
                    last_active=excluded.last_active,
                    ended_reason=excluded.ended_reason,
                    data=excluded.data
                """,
                (session.session_id, session.user, session.last_active, session.ended_reason, payload),
            )
            self._conn.commit()

    def delete(self, session_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM astra_sessions WHERE session_id = ?", (session_id,))
            self._conn.commit()

    def list_sessions(self, user: str | None = None, limit: int = 50, offset: int = 0) -> list[Session]:
        query = "SELECT data FROM astra_sessions"
        params: list = []
        if user is not None:
            query += " WHERE user = ?"
            params.append(user)
        query += " ORDER BY last_active DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [Session.from_dict(json.loads(r[0])) for r in rows]

    def close(self) -> None:
        with self._lock:
            self._conn.close()
