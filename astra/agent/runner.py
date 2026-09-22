# astra/agent/runner.py
"""High-level entry points for running the agent without wiring up
Conversation/Agent/OpenAIProvider by hand every time.

This is the generic stand-in for the original runner.py + verify.py's
"start a chat" endpoint. It has no HTTP/web-framework opinions — call it
from a CLI, a FastAPI route, a Slack bot handler, a test, whatever.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..config import AgentConfig
from ..events import Callbacks
from ..providers.openai_api import OpenAIProvider
from ..signals import SignalStore
from ..storage import Store
from ..tools.decorator import ToolRegistry
from .agent import Agent, AgentResult
from .conversation import Conversation


@dataclass
class Provenance:
    """Optional bookkeeping for *why* a session exists — mirrors the
    original harness's trigger_type/trigger_source/parent_session fields,
    generalized past "Frappe doctype event". Purely informational; the
    agent loop doesn't branch on any of this."""

    trigger_type: str | None = None  # e.g. "manual", "webhook", "schedule", "delegate"
    trigger_source: str | None = None  # e.g. a webhook name, a cron job id
    trigger_ref: str | None = None  # e.g. an external record id
    parent_session: str | None = None
    delegated_skill: str | None = None
    delegate_depth: int = 0


def new_conversation(
    store: Store,
    *,
    session_id: str | None = None,
    user: str = "anonymous",
    signals: SignalStore | None = None,
    callbacks: Callbacks | None = None,
    pricing_lookup=None,
    attachments_builder=None,
    provenance: Provenance | None = None,
    agent_name: str | None = None,
) -> Conversation:
    conv = Conversation(
        store=store,
        session_id=session_id,
        user=user,
        signals=signals,
        callbacks=callbacks,
        pricing_lookup=pricing_lookup,
        attachments_builder=attachments_builder,
    )
    if provenance:
        conv.session.trigger_type = provenance.trigger_type
        conv.session.trigger_source = provenance.trigger_source
        conv.session.trigger_ref = provenance.trigger_ref
        conv.session.parent_session = provenance.parent_session
        conv.session.delegated_skill = provenance.delegated_skill
        conv.session.delegate_depth = provenance.delegate_depth
    if agent_name:
        conv.session.agent_name = agent_name
    return conv


def build_agent(
    conversation: Conversation,
    registry: ToolRegistry,
    config: AgentConfig,
    *,
    system_prompt: str | None = None,
    model_override: str | None = None,
    reasoning_effort_override: str | None = None,
    max_turns: int | None = None,
    max_context_turns: int | None = None,
) -> Agent:
    """Wire a Conversation + ToolRegistry + AgentConfig into a ready-to-run
    Agent. Split out from run_turn() so a caller who wants to hold onto
    the Agent (e.g. to call conversation.request_stop() from another
    coroutine while a long run is in flight) can do so."""
    provider = OpenAIProvider(
        config,
        model_override=model_override,
        reasoning_effort_override=reasoning_effort_override,
    )
    if system_prompt is not None:
        conversation.set_system(system_prompt)
    return Agent(
        conversation=conversation,
        provider=provider,
        registry=registry,
        max_turns=max_turns or config.max_turns,
        max_context_turns=max_context_turns,
    )


async def run_turn(
    conversation: Conversation,
    registry: ToolRegistry,
    config: AgentConfig,
    user_message: str,
    *,
    attachments: list[dict[str, Any]] | None = None,
    system_prompt: str | None = None,
    model_override: str | None = None,
    reasoning_effort_override: str | None = None,
    max_turns: int | None = None,
    max_context_turns: int | None = None,
) -> AgentResult:
    """The common case, in one call: add the user's message, run the
    agent loop to completion (or a pause/stop/error), return the result.
    """
    agent = build_agent(
        conversation,
        registry,
        config,
        system_prompt=system_prompt,
        model_override=model_override,
        reasoning_effort_override=reasoning_effort_override,
        max_turns=max_turns,
        max_context_turns=max_context_turns,
    )
    await conversation.add_user_message(user_message, attachments=attachments)
    return await agent.run()


async def resume_after_clarification(
    store: Store,
    registry: ToolRegistry,
    config: AgentConfig,
    session_id: str,
    clarification_id: str,
    answer: str,
    *,
    signals: SignalStore | None = None,
    callbacks: Callbacks | None = None,
    pricing_lookup=None,
    attachments_builder=None,
    system_prompt: str | None = None,
    max_turns: int | None = None,
) -> AgentResult:
    """Answer a paused clarification and continue the run from where it
    left off."""
    result = await Conversation.resolve_clarification(store, session_id, clarification_id, answer)
    if not result.get("found"):
        raise ValueError(f"No pending clarification '{clarification_id}' found in session '{session_id}'.")

    conversation = Conversation(
        store=store,
        session_id=session_id,
        signals=signals,
        callbacks=callbacks,
        pricing_lookup=pricing_lookup,
        attachments_builder=attachments_builder,
    )
    agent = build_agent(conversation, registry, config, system_prompt=system_prompt, max_turns=max_turns)
    return await agent.run()


async def recover_interrupted_session(
    store: Store,
    registry: ToolRegistry,
    config: AgentConfig,
    session_id: str,
    *,
    signals: SignalStore | None = None,
    callbacks: Callbacks | None = None,
    system_prompt: str | None = None,
    max_turns: int | None = None,
) -> AgentResult | None:
    """If a session was left with dangling pending tool calls (process
    crash mid-run), synthesize error results for them so history stays
    valid, then continue the loop. Returns None if there was nothing to
    recover."""
    conversation = Conversation(store=store, session_id=session_id, signals=signals, callbacks=callbacks)
    if not conversation.can_retry_from_last_state():
        return None
    await conversation.inject_recovery_tool_results()
    agent = build_agent(conversation, registry, config, system_prompt=system_prompt, max_turns=max_turns)
    return await agent.run()
