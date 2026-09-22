"""Event emission — the pluggable replacement for frappe.publish_realtime.

The original harness pushed everything (tokens, reasoning deltas, tool
start/done, clarification requests, done/error) over Frappe's realtime
(socket.io) channel to a per-user "room". Astra has no opinion about
your transport: you hand Conversation a Callbacks object made of plain
Python callables, and it calls them directly — synchronously or as
awaitables, your choice. Wire them to websockets, SSE, a queue, stdout,
whatever. This is deliberately the same shape as the on_token/on_reasoning
hooks Agent.run() already accepted, just generalized to every event type
(the "like hermes does" pattern requested).

Every field is optional — pass only the hooks you care about. A hook may
be a plain sync function or an async function; Conversation awaits it if
it returns a coroutine.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional, Union

Hook = Optional[Callable[..., Union[None, Awaitable[None]]]]

logger = logging.getLogger(__name__)


async def _maybe_await(result: Any) -> None:
    if inspect.isawaitable(result):
        await result


@dataclass
class Callbacks:
    """All hooks are `(**kwargs) -> None | Awaitable[None]`."""

    on_token: Hook = None  # (delta: str)
    on_reasoning: Hook = None  # (delta: str)
    on_message: Hook = None  # (role: str, content: str, **extra)
    on_tool_start: Hook = None  # (call_id, tool_name, args)
    on_tool_result: Hook = None  # (call_id, tool_name, result|error, elapsed_ms, is_error)
    on_tool_output: Hook = None  # (session_id, call_id, stream, data) — live stdout/stderr of process tools
    on_clarification_request: Hook = None  # (session_id, clarification_id, question, options, allow_free_text)
    on_done: Hook = None  # (session_id, response)
    on_error: Hook = None  # (session_id, error_text)
    on_event: Hook = None  # (payload: dict) — catch-all, called alongside the specific hooks above

    async def emit(self, hook_name: str, /, **kwargs) -> None:
        hook: Hook = getattr(self, hook_name, None)
        if hook is not None:
            try:
                await _maybe_await(hook(**kwargs))
            except Exception:
                logger.exception("Callbacks.%s raised — swallowed so it can't crash the agent run.", hook_name)
        if self.on_event is not None and hook_name != "on_event":
            try:
                await _maybe_await(self.on_event(type=hook_name, **kwargs))
            except Exception:
                logger.exception("Callbacks.on_event raised (via %s) — swallowed so it can't crash the agent run.", hook_name)

    def emit_sync(self, hook_name: str, /, **kwargs) -> None:
        """Fire-and-forget variant for non-async call sites. If a hook is
        itself a coroutine function, its coroutine is scheduled on the
        running loop (if any) rather than awaited — best-effort only.
        """
        hook: Hook = getattr(self, hook_name, None)
        for h in (hook, self.on_event if hook_name != "on_event" else None):
            if h is None:
                continue
            call_kwargs = kwargs if h is not self.on_event else {"type": hook_name, **kwargs}
            try:
                result = h(**call_kwargs)
            except Exception:
                logger.exception("Callbacks.%s raised (sync) — swallowed so it can't crash the agent run.", hook_name)
                continue
            if inspect.isawaitable(result):
                try:
                    asyncio.get_running_loop().create_task(result)  # type: ignore[arg-type]
                except RuntimeError:
                    # No running loop — nothing sensible to do with the
                    # coroutine; drop it rather than raise from inside
                    # an event emission.
                    pass
