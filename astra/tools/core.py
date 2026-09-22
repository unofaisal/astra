"""Core value types for the tool layer.

A tool is now three separable things:

  * **what it is**  — ``Tool`` (name, description, JSON-Schema parameters,
    annotations, limits);
  * **how it runs** — a ``Handler`` (a Python function, a subprocess, an
    MCP call, ...); the *only* part that differs between backends;
  * **where it comes from** — a source (Python module dir, ``tool.json``
    manifest dir, MCP server) that yields ``Tool`` objects.

Results travel as ``ToolResult`` and are serialized back to the exact wire
shapes the rest of astra already understands (plain string, JSON, or a JSON
object with a top-level ``"error"`` key — see
``astra.agent.conversation._is_error_result``).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional, Protocol

# ── error taxonomy ────────────────────────────────────────────────
INVALID_ARGS = "invalid_args"
NOT_FOUND = "not_found"
DENIED = "denied"
TIMEOUT = "timeout"
BUSY = "busy"
CRASHED = "crashed"
CANCELLED = "cancelled"
TOOL_ERROR = "tool_error"
UNAVAILABLE = "unavailable"

ERROR_TYPES = (INVALID_ARGS, NOT_FOUND, DENIED, TIMEOUT, BUSY, CRASHED, CANCELLED, TOOL_ERROR, UNAVAILABLE)

# Error types that should count against a circuit breaker (the tool
# itself is misbehaving) as opposed to caller mistakes.
BREAKER_ERRORS = frozenset({TIMEOUT, CRASHED, TOOL_ERROR, UNAVAILABLE})


class ToolError(Exception):
    """Raise from a handler to return a typed, model-actionable error."""

    def __init__(self, message: str, *, error_type: str = TOOL_ERROR, hint: Optional[str] = None, retryable: bool = False, **extra: Any) -> None:
        super().__init__(message)
        self.message = message
        self.error_type = error_type
        self.hint = hint
        self.retryable = retryable
        self.extra = extra


@dataclass
class ToolAnnotations:
    """Behavioural hints, deliberately mirroring MCP's tool annotations so
    MCP tools map 1:1 onto native ones."""

    read_only: bool = False
    destructive: bool = False
    idempotent: bool = False  # gates automatic retry
    open_world: bool = False  # talks to systems outside our control


@dataclass
class ToolResult:
    ok: bool = True
    content: Any = None
    error_type: Optional[str] = None
    message: Optional[str] = None
    hint: Optional[str] = None
    retryable: bool = False
    truncated: bool = False
    meta: Dict[str, Any] = field(default_factory=dict)
    wire: Optional[str] = None  # set by the executor after truncation/spill

    @classmethod
    def success(cls, content: Any = None, **meta: Any) -> "ToolResult":
        return cls(ok=True, content=content, meta=meta)

    @classmethod
    def error(cls, message: str, error_type: str = TOOL_ERROR, *, hint: Optional[str] = None, retryable: bool = False, **meta: Any) -> "ToolResult":
        return cls(ok=False, error_type=error_type, message=message, hint=hint, retryable=retryable, meta=meta)

    def to_wire(self) -> str:
        """Serialize to the string astra stores/replays as the tool result."""
        if self.wire is not None:
            return self.wire
        if self.ok:
            c = self.content
            if c is None:
                return ""
            if isinstance(c, str):
                return c
            if isinstance(c, (dict, list)):
                return json.dumps(c, default=str)
            return str(c)
        payload: Dict[str, Any] = {"error": self.message or "Tool failed", "error_type": self.error_type or TOOL_ERROR}
        if self.hint:
            payload["hint"] = self.hint
        if self.retryable:
            payload["retryable"] = True
        for k, v in self.meta.items():
            payload.setdefault(k, v)
        return json.dumps(payload, default=str)


@dataclass
class ToolContext:
    """Per-call context handed to handlers (replaces module-level
    ``configure_*`` globals for new-style tools)."""

    tool_name: str = ""
    call_id: Optional[str] = None
    session_id: Optional[str] = None
    user: Optional[str] = None
    tenant: Optional[str] = None
    depth: int = 0
    deadline: Optional[float] = None  # loop.time()-independent: time.monotonic()
    workspace: Optional[str] = None
    conversation: Any = None  # the live Conversation (agent-level tools)
    registry: Any = None  # the ToolRegistry this call resolved against
    runtime: Any = None  # the ToolRuntime executing this call
    services: Dict[str, Any] = field(default_factory=dict)
    stop_check: Optional[Callable[[], bool]] = None
    emit: Optional[Callable[[str, bytes], Awaitable[None]]] = None  # stream partial output

    def is_stopped(self) -> bool:
        return bool(self.stop_check and self.stop_check())

    def remaining(self, default: Optional[float] = None) -> Optional[float]:
        import time

        if self.deadline is None:
            return default
        return max(0.0, self.deadline - time.monotonic())


class Handler(Protocol):
    async def __call__(self, args: Dict[str, Any], ctx: ToolContext) -> Any: ...


@dataclass
class Tool:
    name: str
    description: str
    parameters: Dict[str, Any]
    handler: Handler
    toolset: str = "default"
    annotations: ToolAnnotations = field(default_factory=ToolAnnotations)
    timeout: Optional[float] = None  # seconds; None → runtime default
    max_output_chars: Optional[int] = None
    max_concurrency: Optional[int] = None  # per-tool cap
    check: Optional[Callable[[], bool]] = None  # availability gate; False → hidden
    deferred: bool = False  # hidden until discovered via search_tools
    breaker: bool = False  # enable circuit breaker for this tool
    validate: bool = True
    source: str = "python"  # python | manifest | mcp | ...
    meta: Dict[str, Any] = field(default_factory=dict)

    def schema(self) -> Dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description or "",
                "parameters": self.parameters or {"type": "object", "properties": {}},
            },
        }

    @property
    def schema_hash(self) -> str:
        blob = json.dumps({"n": self.name, "d": self.description, "p": self.parameters}, sort_keys=True, default=str)
        return hashlib.sha256(blob.encode()).hexdigest()

    def available(self) -> bool:
        if self.check is None:
            return True
        try:
            return bool(self.check())
        except Exception:
            return False
