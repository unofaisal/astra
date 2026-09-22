# tests/test_clarification_and_recovery.py
import json

import pytest

from astra.agent.agent import Agent
from astra.agent.conversation import ClarificationPending, Conversation
from astra.agent.runner import recover_interrupted_session, resume_after_clarification
from astra.config import AgentConfig, ProviderCredentials
from astra.storage import MemoryStore, MessageRow, Session, ToolCallRow
from astra.tools.decorator import ToolRegistry
from astra.tools.loader import load_tools

from .conftest import FakeProvider, clarify_turn, text_turn

pytestmark = pytest.mark.asyncio


def clarification_registry() -> ToolRegistry:
    registry = ToolRegistry()
    load_tools(registry, "astra/tools")  # loader scans subdirectories; pointing at one group's own folder loads nothing
    return registry


async def test_clarification_pauses_and_resumes(monkeypatch):
    store = MemoryStore()
    registry = clarification_registry()
    provider = FakeProvider([clarify_turn("c1", "Which order?", options=["#1", "#2"])])
    conv = Conversation(store=store, user="alice")
    agent = Agent(conversation=conv, provider=provider, registry=registry)
    await conv.add_user_message("cancel my order")
    result = await agent.run()

    assert result.status == "clarification_pending"
    assert result.clarification_id is not None
    tc = conv.session.tool_calls[0]
    assert tc.status == "awaiting_clarification"

    config = AgentConfig(credentials=ProviderCredentials(provider="openai", api_key="fake"))

    from astra.providers.openai_api import OpenAIProvider

    async def fake_generate(self, messages, tools=None, on_token=None, on_reasoning=None):
        return {"role": "assistant", "content": "Cancelled order #1.", "model": "fake"}, None

    monkeypatch.setattr(OpenAIProvider, "generate", fake_generate)

    result2 = await resume_after_clarification(
        store, registry, config, conv.session_id, result.clarification_id, "#1",
    )
    assert result2.status == "done"
    assert result2.final_text == "Cancelled order #1."
    assert result2.session_id == conv.session_id


async def test_clarification_resolve_updates_tool_call_directly():
    """Exercises Conversation.resolve_clarification in isolation, without
    going through a real provider call afterward — this is the piece
    that must work correctly regardless of what the LLM does next."""
    store = MemoryStore()
    registry = clarification_registry()
    provider = FakeProvider([clarify_turn("c1", "Which order?")])
    conv = Conversation(store=store, user="alice")
    agent = Agent(conversation=conv, provider=provider, registry=registry)
    await conv.add_user_message("cancel my order")
    result = await agent.run()
    assert result.status == "clarification_pending"

    resolved = await Conversation.resolve_clarification(store, conv.session_id, result.clarification_id, "#1")
    assert resolved["found"] is True

    reloaded = store.get(conv.session_id)
    tc = reloaded.tool_calls[0]
    assert tc.status == "success"
    assert "User answered: #1" in tc.result


async def test_resolve_clarification_unknown_id_returns_not_found():
    store = MemoryStore()
    conv = Conversation(store=store, user="alice")
    await conv.add_user_message("hi")
    store.save(conv.session)

    resolved = await Conversation.resolve_clarification(store, conv.session_id, "clar_doesnotexist", "answer")
    assert resolved["found"] is False


async def test_resolve_clarification_survives_unrelated_sessions_between():
    """The scenario from the conversation: pause a clarification, run a
    bunch of unrelated sessions to completion, then resolve the original
    one and confirm it's unaffected."""
    store = MemoryStore()
    registry = clarification_registry()

    provider_a = FakeProvider([clarify_turn("c1", "Refund to card or credit?", options=["card", "credit"])])
    conv_a = Conversation(store=store, user="alice")
    agent_a = Agent(conversation=conv_a, provider=provider_a, registry=registry)
    await conv_a.add_user_message("refund my order")
    result_a = await agent_a.run()
    assert result_a.status == "clarification_pending"

    for i in range(10):
        provider_b = FakeProvider([text_turn(f"unrelated reply {i}")])
        conv_b = Conversation(store=store, user="bob")
        agent_b = Agent(conversation=conv_b, provider=provider_b, registry=registry)
        await conv_b.add_user_message(f"unrelated question {i}")
        r = await agent_b.run()
        assert r.status == "done"

    resolved = await Conversation.resolve_clarification(store, conv_a.session_id, result_a.clarification_id, "card")
    assert resolved["found"] is True
    reloaded = store.get(conv_a.session_id)
    assert reloaded.tool_calls[0].status == "success"


async def test_recover_interrupted_session_synthesizes_missing_results():
    """Simulates a process crash: a session persisted with a tool_call
    stuck in 'pending' (assistant message landed, tool never ran)."""
    store = MemoryStore()
    registry = ToolRegistry()

    session = Session(session_id="crashed_session", user="alice")
    session.messages = [
        MessageRow(message_id="m1", role="user", content="do something"),
        MessageRow(message_id="m2", role="assistant", content=""),
    ]
    session.tool_calls = [
        ToolCallRow(call_id="c1", parent_message="m2", tool_name="echo", arguments="{}", status="pending")
    ]
    store.save(session)

    conv = Conversation(store=store, session_id="crashed_session")
    assert conv.can_retry_from_last_state() is True

    injected = await conv.inject_recovery_tool_results()
    assert injected == 1
    reloaded = store.get("crashed_session")
    assert reloaded.tool_calls[0].status == "error"
    assert "interrupted" in reloaded.tool_calls[0].error.lower()


async def test_recover_interrupted_session_full_flow_continues_run(monkeypatch):
    store = MemoryStore()
    registry = ToolRegistry()

    session = Session(session_id="crashed_session_2", user="alice")
    session.messages = [
        MessageRow(message_id="m1", role="user", content="do something"),
        MessageRow(message_id="m2", role="assistant", content=""),
    ]
    session.tool_calls = [
        ToolCallRow(call_id="c1", parent_message="m2", tool_name="echo", arguments="{}", status="pending")
    ]
    store.save(session)

    config = AgentConfig(credentials=ProviderCredentials(provider="openai", api_key="fake"))

    from astra.providers.openai_api import OpenAIProvider

    async def fake_generate(self, messages, tools=None, on_token=None, on_reasoning=None):
        return {"role": "assistant", "content": "recovered and done", "model": "fake"}, None

    monkeypatch.setattr(OpenAIProvider, "generate", fake_generate)

    result = await recover_interrupted_session(store, registry, config, "crashed_session_2")
    assert result is not None
    assert result.status == "done"
    assert result.final_text == "recovered and done"


async def test_recover_on_healthy_session_is_noop():
    store = MemoryStore()
    registry = ToolRegistry()
    conv = Conversation(store=store, user="alice")
    await conv.add_user_message("hi")
    store.save(conv.session)

    config = AgentConfig(credentials=ProviderCredentials(provider="openai", api_key="fake"))
    result = await recover_interrupted_session(store, registry, config, conv.session_id)
    assert result is None
