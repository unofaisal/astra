"""Session persistence — the pluggable replacement for the Frappe
"Agent session" doctype (with its "messages" and "tool_calls" child
tables).

Design: a Session is a plain, JSON-serializable dataclass tree. A Store
is a tiny protocol (get / save / delete / list) that persists a whole
Session blob keyed by session_id. This is intentionally the simplest
possible contract — anyone can implement Store against Postgres, a
plain dict, DynamoDB, a JSON file per session, whatever — by
implementing four methods.

Astra ships one default implementation (SQLiteStore, see
astra.storage.sqlite_store) that needs zero setup: a single .db file.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field, asdict
from typing import Any, Protocol


def gen_id(prefix: str = "") -> str:
    return f"{prefix}{uuid.uuid4().hex[:20]}"


# ── Row types ──────────────────────────────────────────────────────


@dataclass
class MessageRow:
    message_id: str
    role: str  # "system" | "user" | "assistant" | "tool" | "reasoning"
    content: str | None = None
    attachments: list[dict] | None = None
    timestamp: float = field(default_factory=time.time)

    is_error: bool = False
    reasoning_meta: dict | None = None
    chain_break: dict | None = None
    turn_index: int | None = None
    model: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int | None = None
    reasoning_tokens: int | None = None
    latency_ms: int | None = None
    estimated_cost: float | None = None

    # per-turn skill injection bookkeeping (see Conversation.add_system_message)
    skill_name: str | None = None


@dataclass
class ToolCallRow:
    call_id: str
    parent_message: str
    tool_name: str
    arguments: str = "{}"
    status: str = "pending"  # pending|running|success|error|cancelled|awaiting_clarification
    result: str | None = None
    error: str | None = None
    started_at: float | None = None
    completed_at: float | None = None
    elapsed_ms: int | None = None
    was_loop_strike: bool = False
    clarification_id: str | None = None


@dataclass
class Session:
    session_id: str
    user: str = "anonymous"
    title: str | None = None
    agent_name: str | None = None

    messages: list[MessageRow] = field(default_factory=list)
    tool_calls: list[ToolCallRow] = field(default_factory=list)

    ended_reason: str | None = None
    outcome: str | None = None
    user_feedback: str | None = None
    user_feedback_note: str | None = None

    model: str | None = None
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    estimated_cost: float = 0.0
    turn_count: int = 0
    message_count: int = 0
    tool_call_count: int = 0

    # provenance (see astra.agent.runner.SessionProvenance)
    trigger_type: str | None = None
    trigger_source: str | None = None
    trigger_ref: str | None = None
    parent_session: str | None = None
    delegated_skill: str | None = None
    delegate_depth: int = 0

    skill_invoked: str | None = None
    skill_content_hash: str | None = None

    created_at: float = field(default_factory=time.time)
    last_active: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Session":
        data = dict(data)
        data["messages"] = [MessageRow(**m) for m in data.get("messages", [])]
        data["tool_calls"] = [ToolCallRow(**t) for t in data.get("tool_calls", [])]
        return cls(**data)


# ── Store protocol ─────────────────────────────────────────────────


class Store(Protocol):
    """Minimal persistence contract. Implement this against whatever
    backend you like — the default (SQLiteStore) needs no setup at all.
    """

    def get(self, session_id: str) -> Session | None:
        """Return the Session, or None if it doesn't exist."""
        ...

    def save(self, session: Session) -> None:
        """Persist (create or update) the given session."""
        ...

    def delete(self, session_id: str) -> None:
        ...

    def list_sessions(
        self, user: str | None = None, limit: int = 50, offset: int = 0
    ) -> list[Session]:
        ...
