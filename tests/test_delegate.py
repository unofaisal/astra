# tests/test_delegate.py
"""Tests for delegate_task: an agent calling another agent as a tool,
with results correctly passed back to the delegator, and depth limiting
enforced correctly even under concurrent/parallel delegate calls.
"""
import json
from dataclasses import dataclass

import pytest

from astra.agent.agent import MAX_DELEGATE_DEPTH, Agent
from astra.agent.conversation import Conversation
from astra.config import AgentConfig, ProviderCredentials
from astra.storage import MemoryStore
from astra.tools.decorator import ToolRegistry
from astra.tools.delegate_tools.delegate import DelegateContext, configure_delegate
from astra.tools.loader import load_tools

from .conftest import FakeProvider, text_turn, tool_turn

pytestmark = pytest.mark.asyncio


@dataclass
class FakeAgentSpec:
    content: str
    is_agent: bool = True
    is_enabled: bool = True


def delegate_registry() -> ToolRegistry:
    registry = ToolRegistry()
    load_tools(registry, "astra/tools")  # loader scans subdirectories; pointing at one group's own folder loads nothing
    return registry


@pytest.fixture(autouse=True)
def reset_delegate_context():
    """delegate_task reads a module-level context — reset it around every
    test so tests don't leak configuration into each other."""
    yield
    import astra.tools.delegate_tools.delegate as delegate_module

    delegate_module._context = None


async def test_delegate_basic_result_passed_back(monkeypatch):
    store = MemoryStore()
    registry = delegate_registry()
    config = AgentConfig(credentials=ProviderCredentials(provider="openai", api_key="fake"))

    agents = {
        "refund-bot": FakeAgentSpec(content="You are a refund specialist."),
    }

    configure_delegate(
        DelegateContext(
            resolve_agent=lambda name: agents.get(name),
            store=store,
            config=config,
            registry=registry,
        )
    )

    from astra.providers.openai_api import OpenAIProvider

    # The sub-agent (refund-bot) always just answers directly, no tools.
    async def sub_agent_generate(self, messages, tools=None, on_token=None, on_reasoning=None):
        return {"role": "assistant", "content": "Refund of $50 processed.", "model": "fake"}, None

    monkeypatch.setattr(OpenAIProvider, "generate", sub_agent_generate)

    # The delegator (parent) calls delegate_task once, then finishes.
    parent_provider = FakeProvider(
        [
            tool_turn(("c1", "delegate_task", {"agent_name": "refund-bot", "task": "Refund $50 for order #123"})),
            text_turn("Done — the refund bot handled it."),
        ]
    )
    conv = Conversation(store=store, user="alice")
    agent = Agent(conversation=conv, provider=parent_provider, registry=registry)
    await conv.add_user_message("please refund order #123")
    result = await agent.run()

    assert result.status == "done"
    assert result.final_text == "Done — the refund bot handled it."

    # The critical assertion: the sub-agent's actual answer made it back
    # into the parent conversation as the tool's result.
    tc = conv.session.tool_calls[0]
    assert tc.status == "success"
    payload = json.loads(tc.result)
    assert payload["ended_reason"] == "done"
    assert "Refund of $50 processed." in payload["response"]
    assert payload["sub_session_id"] != conv.session_id  # ran as its own isolated session


async def test_delegate_unknown_agent_returns_clean_error():
    store = MemoryStore()
    registry = delegate_registry()
    config = AgentConfig(credentials=ProviderCredentials(provider="openai", api_key="fake"))

    configure_delegate(DelegateContext(resolve_agent=lambda name: None, store=store, config=config, registry=registry))

    provider = FakeProvider(
        [
            tool_turn(("c1", "delegate_task", {"agent_name": "nonexistent", "task": "do something"})),
            text_turn("Couldn't find that agent."),
        ]
    )
    conv = Conversation(store=store, user="alice")
    agent = Agent(conversation=conv, provider=provider, registry=registry)
    await conv.add_user_message("delegate to a fake agent")
    result = await agent.run()

    assert result.status == "done"  # the harness itself doesn't crash
    tc = conv.session.tool_calls[0]
    payload = json.loads(tc.result) if tc.status == "success" else json.loads(tc.error)
    assert payload.get("error_type") == "not_found"


async def test_delegate_disabled_agent_rejected():
    store = MemoryStore()
    registry = delegate_registry()
    config = AgentConfig(credentials=ProviderCredentials(provider="openai", api_key="fake"))
    agents = {"disabled-bot": FakeAgentSpec(content="...", is_enabled=False)}
    configure_delegate(DelegateContext(resolve_agent=agents.get, store=store, config=config, registry=registry))

    provider = FakeProvider(
        [tool_turn(("c1", "delegate_task", {"agent_name": "disabled-bot", "task": "x"})), text_turn("done")]
    )
    conv = Conversation(store=store, user="alice")
    agent = Agent(conversation=conv, provider=provider, registry=registry)
    await conv.add_user_message("go")
    result = await agent.run()

    tc = conv.session.tool_calls[0]
    content = tc.result if tc.status == "success" else tc.error
    payload = json.loads(content)
    assert payload.get("error_type") == "disabled"


async def test_delegate_not_configured_returns_error_not_crash():
    """If configure_delegate() was never called, delegate_task must fail
    cleanly, not raise an unhandled exception into the agent loop."""
    store = MemoryStore()
    registry = delegate_registry()
    provider = FakeProvider(
        [tool_turn(("c1", "delegate_task", {"agent_name": "anything", "task": "x"})), text_turn("done")]
    )
    conv = Conversation(store=store, user="alice")
    agent = Agent(conversation=conv, provider=provider, registry=registry)
    await conv.add_user_message("go")
    result = await agent.run()

    assert result.status == "done"
    tc = conv.session.tool_calls[0]
    content = tc.result if tc.status == "success" else tc.error
    assert "not configured" in content.lower()


async def test_delegate_depth_limit_enforced(monkeypatch):
    """A chain of agents that each delegate to the next must be stopped
    at MAX_DELEGATE_DEPTH instead of recursing forever."""
    store = MemoryStore()
    registry = delegate_registry()
    config = AgentConfig(credentials=ProviderCredentials(provider="openai", api_key="fake"))

    # Every agent in the chain always tries to delegate to "next-bot"
    # again — an infinite chain if depth weren't enforced.
    agents = {f"agent-{i}": FakeAgentSpec(content=f"agent {i}") for i in range(MAX_DELEGATE_DEPTH + 3)}
    configure_delegate(DelegateContext(resolve_agent=agents.get, store=store, config=config, registry=registry))

    from astra.providers.openai_api import OpenAIProvider

    call_depth = {"n": 0}

    async def chain_generate(self, messages, tools=None, on_token=None, on_reasoning=None):
        call_depth["n"] += 1
        i = call_depth["n"]
        if i > MAX_DELEGATE_DEPTH + 2:
            return {"role": "assistant", "content": "bottomed out", "model": "fake"}, None
        args = json.dumps({"agent_name": f"agent-{i}", "task": "keep going"})
        msg = {
            "role": "assistant", "content": "", "model": "fake",
            "tool_calls": [{"id": f"c{i}", "type": "function", "function": {"name": "delegate_task", "arguments": args}}],
        }

        class _Fn:
            name = "delegate_task"
            arguments = args

        class _TC:
            id = f"c{i}"
            function = _Fn()

        return msg, [_TC()]

    monkeypatch.setattr(OpenAIProvider, "generate", chain_generate)

    parent_provider = FakeProvider(
        [tool_turn(("c0", "delegate_task", {"agent_name": "agent-0", "task": "start the chain"})), text_turn("done")]
    )
    conv = Conversation(store=store, user="alice")
    agent = Agent(conversation=conv, provider=parent_provider, registry=registry)
    await conv.add_user_message("start")
    result = await agent.run()

    # Must resolve cleanly — either the top-level run finishes, or if
    # something propagates unexpectedly it should still be an "error"
    # AgentResult (thanks to the safety net), never a raised exception.
    assert result.status in ("done", "error")

    # Somewhere in the chain, max-depth must actually have been hit —
    # walk every sub-session created and check for the max_depth marker.
    found_depth_limit_error = False

    def walk(session_id):
        nonlocal found_depth_limit_error
        session = store.get(session_id)
        if session is None:
            return
        for tc in session.tool_calls:
            if tc.tool_name == "delegate_task" and tc.status in ("success", "error"):
                content = tc.result if tc.status == "success" else tc.error
                try:
                    payload = json.loads(content)
                except Exception:
                    continue
                if payload.get("error_type") == "max_depth_exceeded":
                    found_depth_limit_error = True
                sub_id = payload.get("sub_session_id")
                if sub_id:
                    walk(sub_id)

    walk(conv.session_id)
    assert found_depth_limit_error, "expected delegate chain to hit MAX_DELEGATE_DEPTH somewhere"


# ── New capabilities: tool allow-list, fan-out caps, background mode ──


@dataclass
class ScopedAgentSpec:
    content: str
    is_agent: bool = True
    is_enabled: bool = True
    tools: list | None = None
    disallowed_tools: list | None = None
    max_turns: int | None = None
    timeout_seconds: float | None = None


async def test_delegate_child_gets_restricted_toolset(monkeypatch):
    from astra.tools.core import Tool

    store = MemoryStore()
    registry = delegate_registry()

    seen_tool_names = {}

    async def probe(args, ctx):
        return "ok"

    registry.register(Tool(name="secret_tool", description="d", parameters={"type": "object", "properties": {}}, handler=probe))
    registry.register(Tool(name="public_tool", description="d", parameters={"type": "object", "properties": {}}, handler=probe))

    config = AgentConfig(credentials=ProviderCredentials(provider="openai", api_key="fake"))
    agents = {"scoped-bot": ScopedAgentSpec(content="scoped", tools=["public_tool"])}

    configure_delegate(DelegateContext(resolve_agent=lambda n: agents.get(n), store=store, config=config, registry=registry))

    from astra.providers.openai_api import OpenAIProvider

    async def sub_agent_generate(self, messages, tools=None, on_token=None, on_reasoning=None):
        seen_tool_names["tools"] = {t["function"]["name"] for t in (tools or [])}
        return {"role": "assistant", "content": "done", "model": "fake"}, None

    monkeypatch.setattr(OpenAIProvider, "generate", sub_agent_generate)

    parent_provider = FakeProvider([tool_turn(("c1", "delegate_task", {"agent_name": "scoped-bot", "task": "x"})), text_turn("ok")])
    conv = Conversation(store=store, user="alice")
    agent = Agent(conversation=conv, provider=parent_provider, registry=registry)
    await conv.add_user_message("go")
    result = await agent.run()

    assert result.status == "done"
    assert "public_tool" in seen_tool_names["tools"]
    assert "secret_tool" not in seen_tool_names["tools"]


async def test_delegate_task_reports_unknown_agent():
    store = MemoryStore()
    registry = delegate_registry()
    config = AgentConfig(credentials=ProviderCredentials(provider="openai", api_key="fake"))
    configure_delegate(DelegateContext(resolve_agent=lambda n: None, store=store, config=config, registry=registry))

    parent_provider = FakeProvider([tool_turn(("c1", "delegate_task", {"agent_name": "ghost", "task": "x"})), text_turn("ok")])
    conv = Conversation(store=store, user="alice")
    agent = Agent(conversation=conv, provider=parent_provider, registry=registry)
    await conv.add_user_message("go")
    result = await agent.run()
    assert result.status == "done"
    tc = conv.session.tool_calls[0]
    payload = json.loads(tc.result or tc.error)
    assert payload["error_type"] == "not_found"


async def test_delegate_fan_out_cap_returns_busy(monkeypatch):
    """Several parallel delegate_task calls beyond max_parallel_per_parent
    get a busy result instead of an unbounded pile of concurrent sub-runs."""
    store = MemoryStore()
    registry = delegate_registry()
    config = AgentConfig(credentials=ProviderCredentials(provider="openai", api_key="fake"))
    agents = {"worker": FakeAgentSpec(content="worker")}

    configure_delegate(
        DelegateContext(
            resolve_agent=lambda n: agents.get(n),
            store=store,
            config=config,
            registry=registry,
            max_parallel_per_parent=1,
            queue_timeout=0.05,
        )
    )

    from astra.providers.openai_api import OpenAIProvider
    import asyncio

    async def slow_sub_agent(self, messages, tools=None, on_token=None, on_reasoning=None):
        await asyncio.sleep(0.3)
        return {"role": "assistant", "content": "done", "model": "fake"}, None

    monkeypatch.setattr(OpenAIProvider, "generate", slow_sub_agent)

    parent_provider = FakeProvider(
        [
            tool_turn(
                ("c1", "delegate_task", {"agent_name": "worker", "task": "a"}),
                ("c2", "delegate_task", {"agent_name": "worker", "task": "b"}),
            ),
            text_turn("ok"),
        ]
    )
    conv = Conversation(store=store, user="alice")
    agent = Agent(conversation=conv, provider=parent_provider, registry=registry)
    await conv.add_user_message("go")
    result = await agent.run()

    assert result.status == "done"
    results = [json.loads(tc.result or tc.error) for tc in conv.session.tool_calls]
    error_types = {r.get("error_type") for r in results if "error" in r}
    assert "busy" in error_types  # one of the two was capped


async def test_spawn_and_await_background_agent(monkeypatch):
    store = MemoryStore()
    registry = delegate_registry()
    config = AgentConfig(credentials=ProviderCredentials(provider="openai", api_key="fake"))
    agents = {"worker": FakeAgentSpec(content="worker")}
    configure_delegate(DelegateContext(resolve_agent=lambda n: agents.get(n), store=store, config=config, registry=registry))

    from astra.providers.openai_api import OpenAIProvider

    async def sub_agent_generate(self, messages, tools=None, on_token=None, on_reasoning=None):
        return {"role": "assistant", "content": "background result", "model": "fake"}, None

    monkeypatch.setattr(OpenAIProvider, "generate", sub_agent_generate)

    from astra.tools.delegate_tools.delegate import spawn_agent, await_agents
    from astra.tools.core import ToolContext
    from astra.agent.conversation import current_session_id

    conv = Conversation(store=store, user="alice")
    token = current_session_id.set(conv.session_id)
    try:
        raw = await spawn_agent({"agent_name": "worker", "task": "background work"}, ToolContext(conversation=conv))
        spawned = json.loads(raw)
        assert spawned["status"] == "running"
        task_id = spawned["task_id"]

        raw2 = await await_agents({"task_ids": [task_id], "timeout_seconds": 5}, ToolContext(conversation=conv))
        collected = json.loads(raw2)["results"][0]
        assert collected["status"] == "done"
        assert collected["response"] == "background result"
    finally:
        current_session_id.reset(token)


async def test_max_depth_respected_in_new_delegate():
    """Depth limit still enforced (regression guard for the rewrite)."""
    store = MemoryStore()
    registry = delegate_registry()
    config = AgentConfig(credentials=ProviderCredentials(provider="openai", api_key="fake"))
    agents = {"loopy": FakeAgentSpec(content="loops forever")}
    configure_delegate(DelegateContext(resolve_agent=lambda n: agents.get(n), store=store, config=config, registry=registry))

    from astra.agent.conversation import current_delegate_depth

    token = current_delegate_depth.set(MAX_DELEGATE_DEPTH)
    try:
        from astra.tools.delegate_tools.delegate import delegate_task
        from astra.tools.core import ToolContext
        from astra.agent.conversation import current_session_id

        stoken = current_session_id.set("parent-sess")
        try:
            raw = await delegate_task({"agent_name": "loopy", "task": "x"}, ToolContext())
            payload = json.loads(raw)
            assert payload["error_type"] == "max_depth_exceeded"
        finally:
            current_session_id.reset(stoken)
    finally:
        current_delegate_depth.reset(token)
