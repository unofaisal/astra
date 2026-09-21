"""Example: a Postgres-backed Store, for a multi-process/multi-worker
deployment where SQLiteStore's single-file/single-process model isn't
enough.

Same 4-method contract as astra.storage.SQLiteStore — this is meant to
be a drop-in replacement, not a different API. Uses psycopg (sync) to
match Conversation._checkpoint()'s synchronous save() call.

    pip install psycopg[binary]

Usage:
    from postgres_store import PostgresStore
    store = PostgresStore("postgresql://user:pass@localhost/astra")
    conversation = Conversation(store=store, user="alice")
"""

from __future__ import annotations

import json

import psycopg
from psycopg.rows import dict_row

from astra.storage import Session, Store  # Store is a Protocol — not subclassed, just satisfied

_SCHEMA = """
CREATE TABLE IF NOT EXISTS astra_sessions (
    session_id   TEXT PRIMARY KEY,
    "user"       TEXT NOT NULL,
    last_active  DOUBLE PRECISION NOT NULL,
    ended_reason TEXT,
    data         JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_astra_sessions_user ON astra_sessions ("user");
CREATE INDEX IF NOT EXISTS idx_astra_sessions_last_active ON astra_sessions (last_active DESC);
"""


class PostgresStore:  # implicitly satisfies astra.storage.Store — no inheritance needed
    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        with psycopg.connect(self._dsn) as conn:
            conn.execute(_SCHEMA)
            conn.commit()

    def get(self, session_id: str) -> Session | None:
        with psycopg.connect(self._dsn, row_factory=dict_row) as conn:
            row = conn.execute(
                "SELECT data FROM astra_sessions WHERE session_id = %s", (session_id,)
            ).fetchone()
        if not row:
            return None
        return Session.from_dict(row["data"])

    def save(self, session: Session) -> None:
        payload = json.dumps(session.to_dict(), default=str)
        with psycopg.connect(self._dsn) as conn:
            conn.execute(
                """
                INSERT INTO astra_sessions (session_id, "user", last_active, ended_reason, data)
                VALUES (%s, %s, %s, %s, %s::jsonb)
                ON CONFLICT (session_id) DO UPDATE SET
                    "user" = EXCLUDED."user",
                    last_active = EXCLUDED.last_active,
                    ended_reason = EXCLUDED.ended_reason,
                    data = EXCLUDED.data
                """,
                (session.session_id, session.user, session.last_active, session.ended_reason, payload),
            )
            conn.commit()

    def delete(self, session_id: str) -> None:
        with psycopg.connect(self._dsn) as conn:
            conn.execute("DELETE FROM astra_sessions WHERE session_id = %s", (session_id,))
            conn.commit()

    def list_sessions(self, user: str | None = None, limit: int = 50, offset: int = 0) -> list[Session]:
        query = "SELECT data FROM astra_sessions"
        params: list = []
        if user is not None:
            query += ' WHERE "user" = %s'
            params.append(user)
        query += " ORDER BY last_active DESC LIMIT %s OFFSET %s"
        params.extend([limit, offset])
        with psycopg.connect(self._dsn, row_factory=dict_row) as conn:
            rows = conn.execute(query, params).fetchall()
        return [Session.from_dict(r["data"]) for r in rows]
