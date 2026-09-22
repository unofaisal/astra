"""Data model for group chats — a SEPARATE model from Session/Conversation.

Why not extend Session? A single-agent Session assumes one continuous
"assistant" identity for the whole transcript — that assumption is baked
into MessageRow.role, Conversation.get_messages()'s replay, clarification
pause/resume, crash recovery, and context summarization. A group has many
standing "assistant" identities in the SAME thread, and turning any of
those into a per-viewer choice (see astra.group.projection) is a different
function, not a flag on the existing one. Reusing Session would mean
threading a "whose perspective" parameter through code that many other
features depend on staying simple and well-tested. So: new, small,
independent model; the tool/provider/runtime layer underneath (ToolExecutor,
ToolRuntime, ProviderPool, SessionGuard, ContextManager, Quota) is fully
reused — nothing there assumed a single assistant identity to begin with.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, fields
from typing import Any, Callable, Protocol


def gen_group_id(prefix: str = "grp_") -> str:
    from ..storage.base import gen_id

    return gen_id(prefix)


class ParticipantSpec(Protocol):
    """What a group participant needs — the same shape as astra's existing
    delegate_task SubAgentSpec, so one "agent directory" can serve both
    delegate_task and group chat."""

    content: str  # system prompt
    is_agent: bool
    is_enabled: bool
    # optional (read with getattr): tools, disallowed_tools, model,
    # reasoning_effort, description (shown to a moderator/selector)


@dataclass
class GroupToolCall:
    """One tool call made during a turn, flattened for replay (see
    astra.group.projection — structured tool_call/tool role pairing does
    not survive being re-projected as another speaker's "user" turn on
    most providers, so group history stores/replays tool activity as text
    summaries rather than raw tool_call blocks)."""

    tool_name: str
    arguments: str = "{}"
    summary: str = ""  # short text of the result/error, for replay
    ok: bool = True


@dataclass
class GroupMessage:
    message_id: str
    author: str  # a participant name, or "__user__"
    content: str | None = None
    tool_calls: list[GroupToolCall] = field(default_factory=list)
    round: int = 0
    timestamp: float = field(default_factory=time.time)
    input_tokens: int = 0
    output_tokens: int = 0
    estimated_cost: float = 0.0
    mentions: list[str] = field(default_factory=list)  # @-mentions parsed from this message


@dataclass
class GroupSession:
    group_id: str
    user: str = "anonymous"
    tenant: str | None = None
    title: str | None = None
    participants: list[str] = field(default_factory=list)  # ordered; index 0 is the default responder
    messages: list[GroupMessage] = field(default_factory=list)
    round: int = 0  # rounds consumed so far (persisted so max_rounds survives a restart)
    last_speaker: str | None = None
    created_at: float = field(default_factory=time.time)
    last_active: float = field(default_factory=time.time)
    ended_reason: str | None = None  # None while "open"; else "max_rounds"|"terminated"|"stopped"|"error"|...
    version: int = 0  # optimistic-concurrency token, same discipline as Session.version

    def to_dict(self) -> dict[str, Any]:
        from dataclasses import asdict

        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "GroupSession":
        data = dict(data)
        msg_fields = {f.name for f in fields(GroupMessage)}
        tc_fields = {f.name for f in fields(GroupToolCall)}
        own_fields = {f.name for f in fields(cls)}
        msgs = []
        for m in data.get("messages", []):
            m = dict(m)
            m["tool_calls"] = [GroupToolCall(**{k: v for k, v in tc.items() if k in tc_fields}) for tc in m.get("tool_calls", [])]
            msgs.append(GroupMessage(**{k: v for k, v in m.items() if k in msg_fields}))
        data["messages"] = msgs
        return cls(**{k: v for k, v in data.items() if k in own_fields})


@dataclass
class GroupTurnResult:
    """What one speaker's turn produced — returned to the caller and to
    telemetry/callbacks, never stored (GroupMessage is the stored form)."""

    speaker: str
    message: GroupMessage
    stopped: bool = False  # True if this turn ended the group (termination condition, error, cap)


@dataclass
class GroupResult:
    group_id: str
    status: str  # "done" | "max_rounds" | "terminated" | "stopped" | "error" | "busy"
    turns: list[GroupTurnResult] = field(default_factory=list)
    error: str | None = None

    @property
    def final_text(self) -> str | None:
        for t in reversed(self.turns):
            if t.message.content:
                return t.message.content
        return None
