# astra/tools/delegate_tools/delegate.py
"""Delegate a sub-task to a named sub-agent, running it as a fresh,
isolated Agent loop that reports back only its final answer.

The original harness resolved `agent_name` against a Frappe "Skill"
doctype (is_agent=1 records) and pulled shared config off frappe.local.
Astra has no database of its own, so the host application registers a
DelegateContext once at startup — a resolver function (name -> a small
object with .content/.is_agent/.is_enabled) plus the store/config/
registry needed to actually spin up the sub-run. Everything else
(depth-limiting via the current_delegate_depth ContextVar, isolated
session id, provenance stamping) is unchanged.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Callable, Protocol

from astra.agent.agent import MAX_DELEGATE_DEPTH, Agent
from astra.agent.conversation import current_delegate_depth, current_session_id
from astra.agent.runner import Provenance, new_conversation
from astra.config import AgentConfig
from astra.events import Callbacks
from astra.signals import SignalStore
from astra.storage import Store
from astra.tools.decorator import ToolRegistry
from astra.tools.decorator import tool

logger = logging.getLogger(__name__)


class SubAgentSpec(Protocol):
    content: str  # system-prompt / instructions text for the sub-agent
    is_agent: bool
    is_enabled: bool


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
    signals: optional SignalStore shared with the parent (needed if you
        want a parent-level stop to also cancel in-flight delegates —
        not wired automatically; check conv.is_stop_requested() yourself
        if you need that).
    """

    resolve_agent: Callable[[str], SubAgentSpec | None]
    store: Store
    config: AgentConfig
    registry: ToolRegistry
    callbacks_factory: Callable[[str], Callbacks] | None = None
    signals: SignalStore | None = None


_context: DelegateContext | None = None


def configure_delegate(context: DelegateContext) -> None:
    """Call once at startup (before running any agent that might use
    delegate_task) to wire this tool to your application."""
    global _context
    _context = context


@tool(schema_name="delegate_task")
async def delegate_task(args: dict, **kwargs) -> str:
    if _context is None:
        return json.dumps({"error": "delegate_task is not configured — call configure_delegate() at startup."})

    agent_name = args.get("agent_name")
    task = args.get("task")
    if not agent_name:
        return json.dumps({"error": "agent_name is required"})
    if not task:
        return json.dumps({"error": "task is required"})

    spec = _context.resolve_agent(agent_name)
    if spec is None:
        return json.dumps({"error": f"Agent '{agent_name}' does not exist", "error_type": "not_found"})
    if not getattr(spec, "is_agent", True):
        return json.dumps({"error": f"'{agent_name}' is not delegatable.", "error_type": "not_an_agent"})
    if not getattr(spec, "is_enabled", True):
        return json.dumps({"error": f"'{agent_name}' is disabled", "error_type": "disabled"})

    # Set once at the top of the caller's Agent.run() (see agent.py) — safe
    # under asyncio.gather's parallel tool calls, no thread/greenlet coupling.
    parent_session_id = current_session_id.get()
    depth = current_delegate_depth.get()

    if depth >= MAX_DELEGATE_DEPTH:
        return json.dumps(
            {
                "error": f"Max delegation depth ({MAX_DELEGATE_DEPTH}) reached; cannot delegate further from within a delegate.",
                "error_type": "max_depth_exceeded",
            }
        )

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
        signals=_context.signals,
        callbacks=callbacks,
        provenance=provenance,
        agent_name=agent_name,
    )
    if _context.callbacks_factory:
        conv.callbacks = _context.callbacks_factory(conv.session_id)
    conv.set_system(getattr(spec, "content", "") or "")

    token = current_delegate_depth.set(depth + 1)
    try:
        await conv.add_user_message(task)
        sub_agent = Agent(conversation=conv, provider=_build_provider(), registry=_context.registry, max_turns=_context.config.max_turns)
        result = await sub_agent.run()
    except Exception as e:
        logger.exception("delegate_task failed: agent=%r task=%r", agent_name, task)
        return json.dumps({"error": str(e)})
    finally:
        current_delegate_depth.reset(token)

    return json.dumps(
        {
            "response": result.final_text,
            "ended_reason": result.status,
            "sub_session_id": conv.session_id,
        },
        default=str,
    )


def _build_provider():
    from astra.providers.openai_api import OpenAIProvider

    return OpenAIProvider(_context.config)
