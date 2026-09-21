# tests/test_pluggability.py
"""Proves the architecture is genuinely entry-point-agnostic:
  - a custom Store, written with zero astra imports beyond Session,
    works with no special registration (duck typing).
  - Provenance correctly tags a session's origin, so a cron/webhook/
    delegate-triggered run is distinguishable from a manual chat one
    purely via stored data — nothing in the agent loop branches on it,
    proving it's optional bookkeeping, not a hard dependency.
  - a "cron-style" invocation (no user waiting, no chat UI, fire the
    agent once and inspect the result) works with the same Agent/
    Conversation/Store code as chat does — no separate code path needed.
  - an unexpected exception during a run resolves to AgentResult(error),
    never propagates raw — required for unattended entry points (cron,
    webhook handlers) with no caller-side try/except of their own.
"""
import pytest

from astra.agent.agent import Agent
from astra.agent.conversation import Conversation
from astra.agent.runner import Provenance, new_conversation
from astra.storage import Session
from astra.tools.decorator import ToolRegistry, tool

from .conftest import FakeProvider, ScriptedTurn, text_turn, tool_turn

pytestmark = pytest.mark.asyncio


# ── A Store with ZERO astra imports beyond Session — proves the
# ── Protocol is truly structural, not requiring inheritance/registration.


class DuckTypedStore:
    def __init__(self):
        self._data = {}

    def get(self, session_id):
        return self._data.get(session_id)

    def save(self, session):
        self._data[session.session_id] = session

    def delete(self, session_id):
        self._data.pop(session_id, None)

    def list_sessions(self, user=None, limit=50, offset=0):
        rows = [s for s in self._data.values() if user is None or s.user == user]
        return rows[offset : offset + limit]


async def test_duck_typed_store_works_with_no_inheritance():
    store = DuckTypedStore()
    conv = Conversation(store=store, user="alice")
    provider = FakeProvider([text_turn("duck typed works")])
    agent = Agent(conversation=conv, provider=provider, registry=ToolRegistry())
    await conv.add_user_message("hi")
    result = await agent.run()

    assert result.status == "done"
    assert store.get(conv.session_id) is not None
    assert isinstance(store.get(conv.session_id), Session)


async def test_provenance_tags_cron_style_invocation():
    """Simulates what a cron-triggered entry point would do: no chat UI,
    no user waiting — build a Conversation with Provenance describing
    the trigger, fire it once, inspect the stored result afterward."""
    store = DuckTypedStore()
    conv = new_conversation(
        store,
        user="system",
        provenance=Provenance(trigger_type="cron", trigger_source="nightly-report-job", trigger_ref="2026-09-14"),
        agent_name="report-generator",
    )
    provider = FakeProvider([text_turn("Report generated.")])
    agent = Agent(conversation=conv, provider=provider, registry=ToolRegistry())
    await conv.add_user_message("Generate the nightly report.")
    result = await agent.run()

    assert result.status == "done"
    stored = store.get(conv.session_id)
    assert stored.trigger_type == "cron"
    assert stored.trigger_source == "nightly-report-job"
    assert stored.trigger_ref == "2026-09-14"
    assert stored.agent_name == "report-generator"


async def test_provenance_tags_webhook_style_invocation():
    store = DuckTypedStore()
    conv = new_conversation(
        store,
        user="external-system",
        provenance=Provenance(trigger_type="webhook", trigger_source="stripe/payment_intent.succeeded", trigger_ref="pi_12345"),
    )
    provider = FakeProvider([text_turn("Processed payment webhook.")])
    agent = Agent(conversation=conv, provider=provider, registry=ToolRegistry())
    await conv.add_user_message("A payment succeeded: pi_12345")
    result = await agent.run()

    assert result.status == "done"
    stored = store.get(conv.session_id)
    assert stored.trigger_type == "webhook"
    assert stored.trigger_source == "stripe/payment_intent.succeeded"


async def test_agent_loop_does_not_branch_on_provenance():
    """Provenance is pure bookkeeping — a run with NO provenance set at
    all must behave identically to one with it set, proving nothing in
    the agent loop actually depends on it being present."""
    store = DuckTypedStore()
    conv = Conversation(store=store, user="alice")  # no Provenance at all
    provider = FakeProvider([text_turn("still works")])
    agent = Agent(conversation=conv, provider=provider, registry=ToolRegistry())
    await conv.add_user_message("hi")
    result = await agent.run()
    assert result.status == "done"
    assert result.final_text == "still works"


async def test_unexpected_exception_resolves_to_error_result_not_raised():
    """Required for unattended entry points: a cron job or webhook
    handler typically has no try/except wrapping the agent call the way
    a chat UI request handler might — an uncaught exception here would
    crash the whole worker process, not just this one run."""
    store = DuckTypedStore()
    registry = ToolRegistry()
    registry.load_schemas(
        {"broken_tool": {"name": "broken_tool", "description": "x", "parameters": {"type": "object", "properties": {}}}}
    )

    @tool(schema_name="broken_tool")
    def broken_tool(args: dict, **kw):
        return {"ok": True}

    registry.register_tool(broken_tool)

    # Force a genuinely unexpected failure deep inside the harness
    # (not a normal tool exception, which is already handled) by
    # breaking signature introspection for this one tool.
    import inspect

    real_signature = inspect.signature

    def broken_signature(obj):
        if getattr(obj, "__schema_name__", None) == "broken_tool":
            raise RuntimeError("simulated unexpected internal failure")
        return real_signature(obj)

    from astra.tools import executor as executor_module

    original = executor_module.inspect.signature
    executor_module.inspect.signature = broken_signature
    try:
        provider = FakeProvider([tool_turn(("c1", "broken_tool", {}))])
        conv = Conversation(store=store, user="alice")
        agent = Agent(conversation=conv, provider=provider, registry=registry)
        await conv.add_user_message("trigger the bug")
        result = await agent.run()  # must NOT raise
    finally:
        executor_module.inspect.signature = original

    assert result.status == "error"
    assert result.session_id == conv.session_id


async def test_multiple_independent_runs_do_not_interfere():
    """Simulates several cron/webhook invocations firing back to back
    against the same store, in the same process — nothing should leak
    between them (loop-detection state, contextvars, etc.)."""
    store = DuckTypedStore()
    registry = ToolRegistry()

    for i in range(5):
        provider = FakeProvider([text_turn(f"run {i} done")])
        conv = Conversation(store=store, user=f"user-{i}")
        agent = Agent(conversation=conv, provider=provider, registry=registry)
        await conv.add_user_message(f"task {i}")
        result = await agent.run()
        assert result.status == "done"
        assert result.final_text == f"run {i} done"

    all_sessions = store.list_sessions()
    assert len(all_sessions) == 5
