"""Persistence for GroupSession — deliberately the same four-method shape
as astra.storage.Store (get/save/delete/list_sessions), so anyone who
already wrote a Store adapter (Postgres, Django, Frappe) can copy it in
about five minutes. Kept separate from Store itself rather than reusing
it generically, because GroupSession is a different payload — merging
the two protocols would force every existing Store implementation to
learn about groups whether or not the app uses them.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Protocol

from ..storage.base import ConcurrentModificationError
from .models import GroupSession


class GroupStore(Protocol):
    def get(self, group_id: str) -> GroupSession | None: ...

    def save(self, group: GroupSession) -> None: ...

    def delete(self, group_id: str) -> None: ...

    def list_groups(self, user: str | None = None, limit: int = 50, offset: int = 0) -> list[GroupSession]: ...


class GroupMemoryStore:
    """In-process, single-worker. Default — zero setup."""

    def __init__(self) -> None:
        self._data: dict[str, GroupSession] = {}
        self._lock = threading.Lock()

    def get(self, group_id: str) -> GroupSession | None:
        with self._lock:
            return self._data.get(group_id)

    def save(self, group: GroupSession) -> None:
        # No optimistic-concurrency check here, deliberately: this store
        # keeps the live object by reference (single process), matching
        # astra.storage.MemoryStore exactly — in-process safety comes from
        # SessionGuard's exclusivity, not the store. The version check
        # lives in GroupSQLiteStore, which serializes to JSON and so is
        # unaffected by later in-place mutation of the object handed to it.
        with self._lock:
            self._data[group.group_id] = group

    def delete(self, group_id: str) -> None:
        with self._lock:
            self._data.pop(group_id, None)

    def list_groups(self, user: str | None = None, limit: int = 50, offset: int = 0) -> list[GroupSession]:
        with self._lock:
            items = [g for g in self._data.values() if user is None or g.user == user]
        items.sort(key=lambda g: g.last_active, reverse=True)
        return items[offset : offset + limit]


_SCHEMA = """
CREATE TABLE IF NOT EXISTS astra_group_sessions (
    group_id TEXT PRIMARY KEY,
    user TEXT,
    last_active REAL,
    ended_reason TEXT,
    data TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_astra_group_sessions_user ON astra_group_sessions(user);
"""


class GroupSQLiteStore:
    """File-backed, WAL mode + optimistic concurrency — same discipline as
    astra.storage.SQLiteStore (see that module's docstring for the
    WAL/durability trade-off this makes)."""

    def __init__(self, path: str | Path = "astra_group_sessions.db", *, wal: bool = True, busy_timeout_ms: int = 5000) -> None:
        self._path = str(path)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self._path, check_same_thread=False, timeout=busy_timeout_ms / 1000)
        self._conn.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
        if wal and self._path != ":memory:" and not self._path.startswith("file::memory:"):
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def get(self, group_id: str) -> GroupSession | None:
        with self._lock:
            row = self._conn.execute("SELECT data FROM astra_group_sessions WHERE group_id = ?", (group_id,)).fetchone()
        return GroupSession.from_dict(json.loads(row[0])) if row else None

    def save(self, group: GroupSession) -> None:
        payload = json.dumps(group.to_dict(), default=str)
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                if group.version > 0:
                    row = self._conn.execute("SELECT version FROM astra_group_sessions WHERE group_id = ?", (group.group_id,)).fetchone()
                    if row is not None and row[0] != group.version - 1:
                        raise ConcurrentModificationError(
                            f"group {group.group_id} was modified elsewhere (stored version {row[0]}, this copy is based on {group.version - 1})"
                        )
                self._conn.execute(
                    """
                    INSERT INTO astra_group_sessions (group_id, user, last_active, ended_reason, data, version)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(group_id) DO UPDATE SET
                        user=excluded.user, last_active=excluded.last_active,
                        ended_reason=excluded.ended_reason, data=excluded.data, version=excluded.version
                    """,
                    (group.group_id, group.user, group.last_active, group.ended_reason, payload, group.version),
                )
                self._conn.execute("COMMIT")
            except BaseException:
                if self._conn.in_transaction:
                    self._conn.execute("ROLLBACK")
                raise

    def delete(self, group_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM astra_group_sessions WHERE group_id = ?", (group_id,))
            self._conn.commit()

    def list_groups(self, user: str | None = None, limit: int = 50, offset: int = 0) -> list[GroupSession]:
        with self._lock:
            if user is not None:
                rows = self._conn.execute(
                    "SELECT data FROM astra_group_sessions WHERE user = ? ORDER BY last_active DESC LIMIT ? OFFSET ?", (user, limit, offset)
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT data FROM astra_group_sessions ORDER BY last_active DESC LIMIT ? OFFSET ?", (limit, offset)
                ).fetchall()
        return [GroupSession.from_dict(json.loads(r[0])) for r in rows]

    def close(self) -> None:
        with self._lock:
            self._conn.close()
