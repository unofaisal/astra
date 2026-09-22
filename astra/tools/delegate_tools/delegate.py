# astra/tools/delegate_tools/delegate.py
"""Multi-agent delegation: run named sub-agents as isolated Agent loops.

Three tools, one mechanism:

  * ``delegate_task``  — run a sub-agent and wait (blocking).  Several
    ``delegate_task`` calls in the same model step run in PARALLEL (they are
    ordinary parallel tool calls) — the orchestrator-worker pattern.
  * ``spawn_agent``    — start a sub-agent in the background, keep working.
  * ``await_agents`` / ``cancel_agent`` — collect / cancel background runs.

What the host controls per sub-agent (optional attributes on the object
your ``resolve_agent`` returns — a plain dataclass works):

    content            system prompt text                 (required, as before)
    is_agent / is_enabled                                 (as before)
    tools              allow-list of tool names           (least privilege)
    disallowed_tools   deny-list
    model              cheaper/faster model for cheap work
    reasoning_effort   per-agent reasoning effort override
    max_turns          per-agent turn cap
    timeout_seconds    wall-clock cap for the sub-run

Safety properties
  * **Cancellation propagates.**  A parent Stop/timeout cancels an in-flight
    blocking sub-agent (asyncio cancellation) and, via a run-end hook, any
    background ones — children no longer keep spending tokens after Stop.
  * **Bounded fan-out.**  Per-parent and global caps (a busy result instead
    of an unbounded pile of concurrent LLM loops).  Nested delegation is
    still depth-limited (``MAX_DELEGATE_DEPTH``).
  * **Identity flows down.**  Sub-sessions belong to the parent's user and
    tenant, so quotas and ownership checks apply to them too.
  * **Usage rolls up.**  Child tokens/cost are reported in the tool result
    and added to the parent session's ``delegated_*`` counters.

Only the child's final answer crosses back into the parent's context
(context isolation is the point of sub-agents).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Protocol

from astra.agent.agent import MAX_DELEGATE_DEPTH, Agent
from astra.agent.conversation import current_delegate_depth, current_session_id, current_tenant, current_user
from astra.agent.runner import Provenance, build_agent, new_conversation
from astra.concurrency import KeyedLimiters, Limiter, LimiterBusy
from astra.config import AgentConfig
from astra.events import Callbacks
from astra.signals import SignalStore
from astra.storage import Store
from astra.tools.core import ToolContext
from astra.tools.decorator import ToolRegistry, tool

logger = logging.getLogger(__name__)

_DELEGATE_TOOLS = ("delegate_task", "spawn_agent", "await_agents", "cancel_agent")


class SubAgentSpec(Protocol):
    content: str  # system-prompt / instructions text for the sub-agent
    is_agent: bool
    is_enabled: bool
    # Optional (read with getattr): tools, disallowed_tools, model,
    # reasoning_effort, max_turns, timeout_seconds


@dataclass
class DelegateContext:
    """Everything delegate_task needs from the host application.

    resolve_agent: name -> SubAgentSpec | None. Look the name up however
        you like (a dict, a DB table, markdown files under an agents/
        directory — see astra.prompts for a ready-made file-based helper).
    store / config / registry: reused to build the sub-run's Conversation
        + Agent. Pass the same ones the parent run uses unless you want
        the sub-agent on a different model/tool set.
    callbacks_factory: optional, called with the new sub-session_id to
        produce a Callbacks for the sub-run. Defaults to no callbacks
        (sub-run's tokens/tool events are silent — only the final answer
        crosses back), matching the original "no UI noise" behavior.
    signals: optional SignalStore shared with the parent (lets a user Stop
        aimed at the *sub-session id* work too; parent-Stop cancellation
        does not depend on it).
    runtime / pool / policy / quota / context_manager_factory: the same
        shared objects the parent uses (so tool limits, HTTP clients, quotas
        and context policy apply to sub-agents as well).
    max_parallel_per_parent / max_parallel_global / queue_timeout: fan-out
        caps.  The global cap applies to top-level fan-out only (children of
        children take only the per-parent cap) so nested chains can't
        deadlock each other on the global limit.
    enable_background: expose spawn_agent / await_agents / cancel_agent.
    """

    resolve_agent: Callable[[str], SubAgentSpec | None]
    store: Store
    config: AgentConfig
    registry: ToolRegistry
    callbacks_factory: Callable[[str], Callbacks] | None = None
    signals: SignalStore | None = None
    runtime: Any = None
    pool: Any = None
    policy: Any = None
    quota: Any = None
    context_manager_factory: Callable[[], Any] | None = None
    max_parallel_per_parent: int = 4
    max_parallel_global: int = 32
    max_background_per_parent: int = 8
    queue_timeout: float = 30.0
    default_timeout_seconds: float | None = None
    enable_background: bool = True

    def __post_init__(self) -> None:
        self._per_parent = KeyedLimiters(self.max_parallel_per_parent, max_queue=self.max_parallel_per_parent * 4, name="delegate-parent")
        self._global = Limiter(self.max_parallel_global, max_queue=self.max_parallel_global * 8, name="delegate-global")


_context: DelegateContext | None = None


def configure_delegate(context: DelegateContext) -> None:
    """Call once at startup (before running any agent that might use
    delegate_task) to wire this tool to your application."""
    global _context
    _context = context
    if context.runtime is not None:
        context.runtime.add_run_end_hook(_on_parent_run_end)


def _configured() -> bool:
    return _context is not None and _context.enable_background


# ── helpers ────────────────────────────────────────────────────────

def _err(message: str, error_type: str | None = None, **extra: Any) -> str:
    payload: Dict[str, Any] = {"error": message}
    if error_type:
        payload["error_type"] = error_type
    payload.update(extra)
    return json.dumps(payload, default=str)


def _validate_spec(agent_name: str | None) -> tuple[Any, str | None]:
    """Return (spec, error_json)."""
    assert _context is not None
    if not agent_name:
        return None, _err("agent_name is required")
    spec = _context.resolve_agent(agent_name)
    if spec is None:
        return None, _err(f"Agent '{agent_name}' does not exist", "not_found")
    if not getattr(spec, "is_agent", True):
        return None, _err(f"'{agent_name}' is not delegatable.", "not_an_agent")
    if not getattr(spec, "is_enabled", True):
        return None, _err(f"'{agent_name}' is disabled", "disabled")
    return spec, None


def _child_registry(spec: Any) -> ToolRegistry:
    assert _context is not None
    allow = getattr(spec, "tools", None)
    deny = set(getattr(spec, "disallowed_tools", None) or ())
    if allow is None and not deny:
        return _context.registry
    return _context.registry.view(allow=allow, deny=deny)


def _make_child(spec: Any, agent_name: str, task_text: str, ctx: ToolContext | None, depth: int, parent_session_id: str):
    """Build (conversation, agent) for a sub-run."""
    assert _context is not None
    user = (ctx.user if ctx and ctx.user else None) or current_user.get() or "anonymous"
    tenant = (ctx.tenant if ctx and ctx.tenant else None) or current_tenant.get() or None
    provenance = Provenance(
        trigger_type="delegate",
        trigger_source=parent_session_id or "unknown-parent",
        trigger_ref=f"depth-{depth + 1}",
        parent_session=parent_session_id or None,
        delegated_skill=agent_name,
        delegate_depth=depth + 1,
    )
    callbacks = _context.callbacks_factory("pending") if _context.callbacks_factory else None
    conv = new_conversation(
        _context.store,
        user=user,
        tenant=tenant,
        signals=_context.signals,
        callbacks=callbacks,
        provenance=provenance,
        agent_name=agent_name,
    )
    if _context.callbacks_factory:
        conv.callbacks = _context.callbacks_factory(conv.session_id)

    timeout = getattr(spec, "timeout_seconds", None) or _context.default_timeout_seconds
    runtime = (ctx.runtime if ctx and ctx.runtime is not None else None) or _context.runtime
    agent = build_agent(
        conv,
        _child_registry(spec),
        _context.config,
        system_prompt=getattr(spec, "content", "") or "",
        model_override=getattr(spec, "model", None),
        reasoning_effort_override=getattr(spec, "reasoning_effort", None),
        max_turns=getattr(spec, "max_turns", None) or _context.config.max_turns,
        pool=_context.pool,
        runtime=runtime,
        policy=_context.policy,
        quota=_context.quota,
        context_manager=_context.context_manager_factory() if _context.context_manager_factory else None,
        run_timeout=timeout,
    )
    return conv, agent, timeout


def _usage(conv: Any) -> Dict[str, Any]:
    s = conv.session
    return {
        "input_tokens": s.total_input_tokens + s.delegated_input_tokens,
        "output_tokens": s.total_output_tokens + s.delegated_output_tokens,
        "cost": round(s.estimated_cost + s.delegated_cost, 6),
    }


def _rollup(parent_conv: Any, child_usage: Dict[str, Any]) -> None:
    if parent_conv is None:
        return
    ps = parent_conv.session
    ps.delegated_input_tokens += int(child_usage.get("input_tokens") or 0)
    ps.delegated_output_tokens += int(child_usage.get("output_tokens") or 0)
    ps.delegated_cost += float(child_usage.get("cost") or 0.0)


async def _run_child(conv: Any, agent: Agent, task_text: str, timeout: float | None):
    """Run the child to completion, saving a clear terminal state if it is
    cancelled from above (parent Stop) so the sub-session isn't left 'running'."""
    depth_token = current_delegate_depth.set(conv.session.delegate_depth)
    try:
        await conv.add_user_message(task_text)
        return await agent.run()
    except asyncio.CancelledError:
        try:
            conv.cancel_pending_tool_calls("Cancelled: the parent run was stopped.")
            conv.save(ended_reason="cancelled_by_parent")
        except Exception:
            logger.exception("could not save cancelled sub-session %s", conv.session_id)
        raise
    finally:
        current_delegate_depth.reset(depth_token)


def _result_payload(conv: Any, result: Any) -> Dict[str, Any]:
    return {
        "response": result.final_text,
        "ended_reason": result.status,
        "sub_session_id": conv.session_id,
        "usage": _usage(conv),
    }


# ── delegate_task (blocking, parallelizable) ────────────────────────

@tool(schema_name="delegate_task", timeout=0)  # 0 = no executor timeout: the sub-run enforces its own (spec.timeout_seconds / run_timeout)
async def delegate_task(args: dict, ctx: ToolContext | None = None, **kwargs) -> str:
    if _context is None:
        return json.dumps({"error": "delegate_task is not configured — call configure_delegate() at startup."})

    agent_name = args.get("agent_name")
    task = args.get("task")
    if not agent_name:
        return json.dumps({"error": "agent_name is required"})
    if not task:
        return json.dumps({"error": "task is required"})

    spec, err = _validate_spec(agent_name)
    if err:
        return err

    parent_session_id = current_session_id.get()
    depth = current_delegate_depth.get()
    if depth >= MAX_DELEGATE_DEPTH:
        return json.dumps(
            {
                "error": f"Max delegation depth ({MAX_DELEGATE_DEPTH}) reached; cannot delegate further from within a delegate.",
                "error_type": "max_depth_exceeded",
            }
        )

    # Fan-out caps: per parent always; global only for top-level fan-out.
    per_parent = _context._per_parent.get(parent_session_id or "_", _context.max_parallel_per_parent)
    acquired: List[Limiter] = []
    try:
        await per_parent.acquire(_context.queue_timeout)
        acquired.append(per_parent)
        if depth == 0:
            await _context._global.acquire(_context.queue_timeout)
            acquired.append(_context._global)
    except LimiterBusy as exc:
        for lim in reversed(acquired):
            lim.release()
        return _err(
            f"Too many sub-agents are running right now ({exc}).",
            "busy",
            hint="Wait for running sub-agents to finish, or delegate fewer tasks at once.",
        )

    conv = None
    try:
        conv, agent, timeout = _make_child(spec, agent_name, task, ctx, depth, parent_session_id)
        try:
            if timeout:
                result = await asyncio.wait_for(_run_child(conv, agent, task, timeout), timeout + 5)
            else:
                result = await _run_child(conv, agent, task, None)
        except asyncio.TimeoutError:
            return _err(f"Sub-agent '{agent_name}' timed out after {timeout:g}s.", "timeout", sub_session_id=conv.session_id, usage=_usage(conv))
        payload = _result_payload(conv, result)
        _rollup(ctx.conversation if ctx else None, payload["usage"])
        return json.dumps(payload, default=str)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.exception("delegate_task failed: agent=%r task=%r", agent_name, task)
        return json.dumps({"error": str(e)})
    finally:
        for lim in reversed(acquired):
            lim.release()


# ── background sub-agents ───────────────────────────────────────────

@dataclass
class _Bg:
    task_id: str
    parent_session: str
    agent_name: str
    user: str
    task: "asyncio.Task[Any]"
    conv: Any
    started: float = field(default_factory=time.time)
    rolled_up: bool = False
    limiters: List[Limiter] = field(default_factory=list)


_BACKGROUND: Dict[str, _Bg] = {}


def _on_parent_run_end(session_id: str, status: str) -> None:
    """Runtime hook: cancel un-awaited background children when their
    parent run ends (Stop, error, or simply finishing without awaiting).
    A run paused for clarification keeps them — it will resume."""
    if status == "clarification_pending":
        return
    for bg in [b for b in _BACKGROUND.values() if b.parent_session == session_id and not b.task.done()]:
        logger.info("cancelling background agent %s (parent run ended: %s)", bg.task_id, status)
        bg.task.cancel()


def _background_done(bg: _Bg, _fut: Any) -> None:
    for lim in reversed(bg.limiters):
        lim.release()
    bg.limiters = []


@tool(schema_name="spawn_agent", check=_configured)
async def spawn_agent(args: dict, ctx: ToolContext | None = None, **kwargs) -> str:
    if _context is None:
        return json.dumps({"error": "spawn_agent is not configured — call configure_delegate() at startup."})
    agent_name, task = args.get("agent_name"), args.get("task")
    if not agent_name or not task:
        return _err("agent_name and task are required")
    spec, err = _validate_spec(agent_name)
    if err:
        return err
    parent_session_id = current_session_id.get()
    depth = current_delegate_depth.get()
    if depth >= MAX_DELEGATE_DEPTH:
        return _err(f"Max delegation depth ({MAX_DELEGATE_DEPTH}) reached.", "max_depth_exceeded")

    running = [b for b in _BACKGROUND.values() if b.parent_session == parent_session_id and not b.task.done()]
    if len(running) >= _context.max_background_per_parent:
        return _err(
            f"Already {len(running)} background sub-agents running for this session.",
            "busy",
            hint="Use await_agents to collect finished ones first.",
        )
    acquired: List[Limiter] = []
    try:
        if depth == 0:
            await _context._global.acquire(_context.queue_timeout)
            acquired.append(_context._global)
    except LimiterBusy as exc:
        return _err(f"Too many sub-agents are running right now ({exc}).", "busy")

    try:
        conv, agent, timeout = _make_child(spec, agent_name, task, ctx, depth, parent_session_id)
    except Exception as exc:
        for lim in acquired:
            lim.release()
        logger.exception("spawn_agent failed")
        return _err(str(exc))

    async def runner():
        if timeout:
            return await asyncio.wait_for(_run_child(conv, agent, task, timeout), timeout + 5)
        return await _run_child(conv, agent, task, None)

    t = asyncio.get_running_loop().create_task(runner(), name=f"astra-bg-{conv.session_id}")
    bg = _Bg(conv.session_id, parent_session_id, agent_name, conv.session.user, t, conv, limiters=acquired)
    _BACKGROUND[conv.session_id] = bg
    t.add_done_callback(lambda f, b=bg: _background_done(b, f))
    return json.dumps({"task_id": conv.session_id, "status": "running", "agent_name": agent_name})


def _describe_bg(bg: _Bg, parent_conv: Any) -> Dict[str, Any]:
    if not bg.task.done():
        return {"task_id": bg.task_id, "status": "running", "agent_name": bg.agent_name, "elapsed_seconds": round(time.time() - bg.started, 1)}
    if bg.task.cancelled():
        return {"task_id": bg.task_id, "status": "cancelled", "agent_name": bg.agent_name}
    exc = bg.task.exception()
    if isinstance(exc, asyncio.TimeoutError):
        return {"task_id": bg.task_id, "status": "timeout", "agent_name": bg.agent_name, "usage": _usage(bg.conv)}
    if exc is not None:
        return {"task_id": bg.task_id, "status": "error", "agent_name": bg.agent_name, "error": str(exc)}
    result = bg.task.result()
    payload = _result_payload(bg.conv, result)
    if not bg.rolled_up:
        _rollup(parent_conv, payload["usage"])
        bg.rolled_up = True
    return {"task_id": bg.task_id, "status": "done" if result.status == "done" else result.status, "agent_name": bg.agent_name, **payload}


@tool(schema_name="await_agents", check=_configured, read_only=True, timeout=0)
async def await_agents(args: dict, ctx: ToolContext | None = None, **kwargs) -> str:
    if _context is None:
        return json.dumps({"error": "await_agents is not configured — call configure_delegate() at startup."})
    ids = args.get("task_ids") or []
    if not isinstance(ids, list) or not ids:
        return _err("task_ids must be a non-empty list")
    timeout = float(args.get("timeout_seconds", 60) or 0)
    parent_session_id = current_session_id.get()
    parent_conv = ctx.conversation if ctx else None

    mine: Dict[str, _Bg] = {}
    results: List[Dict[str, Any]] = []
    for tid in ids:
        bg = _BACKGROUND.get(tid)
        if bg is not None and bg.parent_session == parent_session_id:
            mine[tid] = bg
        else:
            results.append(_lookup_persisted(tid, parent_session_id))

    pending = {b.task for b in mine.values() if not b.task.done()}
    if pending and timeout > 0:
        await asyncio.wait(pending, timeout=timeout)
    for tid, bg in mine.items():
        results.append(_describe_bg(bg, parent_conv))
        if bg.task.done():
            _BACKGROUND.pop(tid, None)
    return json.dumps({"results": results}, default=str)


def _lookup_persisted(task_id: str, parent_session_id: str) -> Dict[str, Any]:
    """A task we no longer hold in memory (process restarted): report what
    the durable sub-session says, without trusting ids from other parents."""
    assert _context is not None
    sess = _context.store.get(task_id)
    if sess is None or sess.parent_session != parent_session_id:
        return {"task_id": task_id, "status": "not_found"}
    if sess.ended_reason == "completed":
        text = ""
        for m in reversed(sess.messages):
            if m.role == "assistant" and m.content:
                text = m.content
                break
        return {"task_id": task_id, "status": "done", "response": text, "sub_session_id": task_id}
    return {"task_id": task_id, "status": "lost", "hint": "The sub-agent did not finish (process restarted or it was cancelled). Spawn it again if still needed."}


@tool(schema_name="cancel_agent", check=_configured)
async def cancel_agent(args: dict, **kwargs) -> str:
    tid = args.get("task_id")
    bg = _BACKGROUND.get(tid or "")
    if bg is None or bg.parent_session != current_session_id.get():
        return _err(f"No background agent '{tid}' for this session.", "not_found")
    if bg.task.done():
        return json.dumps({"task_id": tid, "status": "already_finished"})
    bg.task.cancel()
    return json.dumps({"task_id": tid, "status": "cancelling"})
