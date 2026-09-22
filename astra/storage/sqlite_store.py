"""SQLite-backed Store — the default, zero-setup persistence layer.

One file (or ':memory:'), one table. The full Session is stored as a
JSON blob in `data`; a handful of columns are duplicated out of it so
you can query/filter cheaply (list by user, sort by last_active) without
deserializing every row. This mirrors the "denormalize onto the parent
doc for cheap dashboard queries" pattern in the original Frappe
Conversation._checkpoint, just without a full ORM.

File databases run in WAL mode with ``synchronous=NORMAL`` by default
(measured ≈4× the throughput of the default rollback journal under
concurrent sessions, because every checkpoint is a commit).  The trade-off
is standard for WAL: an application crash loses nothing, but an OS crash /
power loss may drop the last few commits.  Pass ``wal=False`` for the old
behaviour.

Writes use optimistic concurrency: ``Session.version`` is bumped on each
checkpoint and a save whose base version no longer matches the stored one
raises ``ConcurrentModificationError`` instead of silently overwriting
another writer's changes (two tabs, two workers, a webhook racing a user).
The check runs inside a ``BEGIN IMMEDIATE`` transaction so it is atomic
across processes sharing the file.  For heavy multi-process load, implement
the four ``Store`` methods against a server database instead — the
protocol is unchanged.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

from .base import ConcurrentModificationError, Session

_SCHEMA = """
CREATE TABLE IF NOT EXISTS astra_sessions (
    session_id TEXT PRIMARY KEY,
    user TEXT NOT NULL,
    last_active REAL NOT NULL,
    ended_reason TEXT,
    data TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_astra_sessions_user ON astra_sessions(user);
CREATE INDEX IF NOT EXISTS idx_astra_sessions_last_active ON astra_sessions(last_active);
"""


class SQLiteStore:
    def __init__(self, path: str | Path = "astra_sessions.db", *, wal: bool = True, busy_timeout_ms: int = 5000, check_versions: bool = True) -> None:
        self._path = str(path)
        self._lock = threading.Lock()
        self._check_versions = check_versions
        # check_same_thread=False: astra's Agent loop is async but callers
        # may run it from different threads (e.g. a thread-pool executor
        # around asyncio.run). All access is still serialized by _lock.
        self._conn = sqlite3.connect(self._path, check_same_thread=False, timeout=busy_timeout_ms / 1000)
        self._conn.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
        if wal and self._path != ":memory:" and not self._path.startswith("file::memory:"):
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(_SCHEMA)
        cols = {r[1] for r in self._conn.execute("PRAGMA table_info(astra_sessions)")}
        if "version" not in cols:  # migrate databases created by older versions
            self._conn.execute("ALTER TABLE astra_sessions ADD COLUMN version INTEGER NOT NULL DEFAULT 0")
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
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                if self._check_versions and session.version > 0:
                    row = self._conn.execute("SELECT version FROM astra_sessions WHERE session_id = ?", (session.session_id,)).fetchone()
                    if row is not None and row[0] != session.version - 1:
                        raise ConcurrentModificationError(
                            f"session {session.session_id} was modified elsewhere "
                            f"(stored version {row[0]}, this copy is based on {session.version - 1})"
                        )
                self._conn.execute(
                    """
                    INSERT INTO astra_sessions (session_id, user, last_active, ended_reason, data, version)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(session_id) DO UPDATE SET
                        user=excluded.user,
                        last_active=excluded.last_active,
                        ended_reason=excluded.ended_reason,
                        data=excluded.data,
                        version=excluded.version
                    """,
                    (session.session_id, session.user, session.last_active, session.ended_reason, payload, session.version),
                )
                self._conn.execute("COMMIT")
            except BaseException:
                if self._conn.in_transaction:
                    self._conn.execute("ROLLBACK")
                raise

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
