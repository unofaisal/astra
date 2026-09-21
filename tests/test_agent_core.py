# tests/test_agent_core.py
import asyncio
import json

import pytest

from astra.agent.agent import Agent
from astra.agent.conversation import Conversation
from astra.events import Callbacks
from astra.storage import MemoryStore, SQLiteStore
from astra.tools.decorator import ToolRegistry, tool

from .conftest import FakeProvider, ScriptedTurn, clarify_turn, text_turn, tool_turn

pytestmark = pytest.mark.asyncio


def make_registry_with_echo_tool() -> ToolRegistry:
    registry = ToolRegistry()
    registry.load_schemas(
        {
            "echo": {
                "name": "echo",
                "description": "echoes back",
                "parameters": {"type": "object", "properties": {"text": {"type": "string"}}},
            },
            "slow_echo": {
                "name": "slow_echo",
                "description": "echoes back after a delay",
                "parameters": {"type": "object", "properties": {"text": {"type": "string"}}},
            },
            "boom": {
                "name": "boom",
                "description": "always raises",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    )

    @tool(schema_name="echo")
    def echo(args: dict, **kw):
        return {"echoed": args.get("text")}

    @tool(schema_name="slow_echo")
    async def slow_echo(args: dict, **kw):
        await asyncio.sleep(0.01)
        return {"echoed": args.get("text")}

    @tool(schema_name="boom")
    def boom(args: dict, **kw):
        raise ValueError("kaboom")

    registry.register_tool(echo)
    registry.register_tool(slow_echo)
    registry.register_tool(boom)
    return registry


async def test_basic_chat_done():
    store = MemoryStore()
    conv = Conversation(store=store, user="alice")
    provider = FakeProvider([text_turn("Hi there!")])
    agent = Agent(conversation=conv, provider=provider, registry=ToolRegistry())

    await conv.add_user_message("hello")
    result = await agent.run()

    assert result.status == "done"
    assert result.final_text == "Hi there!"
    assert result.session_id == conv.session_id
    assert len(conv.session.messages) == 2  # user + assistant


async def test_multi_turn_continuation():
    store = MemoryStore()
    provider = FakeProvider([text_turn("first reply")])

    conv1 = Conversation(store=store, user="alice")
    agent1 = Agent(conversation=conv1, provider=provider, registry=ToolRegistry())
    await conv1.add_user_message("first message")
    r1 = await agent1.run()
    assert r1.status == "done"

    # New Conversation object, same store + session_id — simulates a
    # brand new request continuing an existing chat.
    provider.script([text_turn("second reply")])
    conv2 = Conversation(store=store, session_id=conv1.session_id, user="alice")
    agent2 = Agent(conversation=conv2, provider=provider, registry=ToolRegistry())
    await conv2.add_user_message("second message")
    r2 = await agent2.run()

    assert r2.status == "done"
    assert r2.final_text == "second reply"
    assert len(conv2.session.messages) == 4  # 2 user + 2 assistant, history preserved


async def test_parallel_tool_calls_both_execute():
    store = MemoryStore()
    registry = make_registry_with_echo_tool()
    provider = FakeProvider(
        [
            tool_turn(("c1", "echo", {"text": "A"}), ("c2", "slow_echo", {"text": "B"})),
            text_turn("done"),
        ]
    )
    conv = Conversation(store=store, user="alice")
    agent = Agent(conversation=conv, provider=provider, registry=registry)
    await conv.add_user_message("run both")
    result = await agent.run()

    assert result.status == "done"
    statuses = {tc.call_id: tc.status for tc in conv.session.tool_calls}
    assert statuses == {"c1": "success", "c2": "success"}
    results = {tc.call_id: json.loads(tc.result) for tc in conv.session.tool_calls}
    assert results["c1"]["echoed"] == "A"
    assert results["c2"]["echoed"] == "B"


async def test_tool_exception_becomes_error_result_not_crash():
    store = MemoryStore()
    registry = make_registry_with_echo_tool()
    provider = FakeProvider([tool_turn(("c1", "boom", {})), text_turn("recovered")])
    conv = Conversation(store=store, user="alice")
    agent = Agent(conversation=conv, provider=provider, registry=registry)
    await conv.add_user_message("break it")
    result = await agent.run()

    assert result.status == "done"  # agent kept going despite the tool raising
    tc = conv.session.tool_calls[0]
    assert tc.status == "error"
    assert "kaboom" in tc.error


async def test_loop_detection_prevents_reexecution_and_allows_recovery():
    """Loop detection's job: once the same (tool, args) pair repeats 3x
    consecutively, the 3rd+ call is marked as an error WITHOUT actually
    invoking the tool again, and the model gets a clear error message to
    react to. It does not itself force the run to end — that's max_turns'
    job if the model never listens. Here we simulate a model that DOES
    listen (stops repeating after seeing the error), and confirm the run
    completes normally rather than being forced to fail."""
    store = MemoryStore()
    registry = ToolRegistry()
    registry.load_schemas(
        {"echo": {"name": "echo", "description": "x", "parameters": {"type": "object", "properties": {"text": {"type": "string"}}}}}
    )
    call_count = {"n": 0}

    @tool(schema_name="echo")
    def echo(args: dict, **kw):
        call_count["n"] += 1
        return {"echoed": args.get("text")}

    registry.register_tool(echo)

    same_call = tool_turn(("c", "echo", {"text": "same"}))
    provider = FakeProvider([same_call, same_call, same_call, text_turn("ok, stopping now")])
    conv = Conversation(store=store, user="alice")
    agent = Agent(conversation=conv, provider=provider, registry=registry, max_turns=10)
    await conv.add_user_message("loop please")
    result = await agent.run()

    assert result.status == "done"
    assert result.final_text == "ok, stopping now"

    # The 3rd consecutive identical call must be flagged as a loop strike
    # and NOT actually re-invoke the underlying tool function.
    tool_call_statuses = [tc.status for tc in conv.session.tool_calls]
    assert "error" in tool_call_statuses
    loop_strikes = [tc for tc in conv.session.tool_calls if tc.was_loop_strike]
    assert len(loop_strikes) >= 1
    # echo() itself should have run fewer times than there were tool_call rows
    assert call_count["n"] < len(conv.session.tool_calls)


async def test_loop_detection_falls_back_to_max_turns_if_model_never_listens():
    """If the model ignores the loop-detection error and keeps repeating
    forever, max_turns is the real backstop — the run must still end
    cleanly rather than looping forever."""
    store = MemoryStore()
    registry = make_registry_with_echo_tool()
    provider = FakeProvider([tool_turn(("c1", "echo", {"text": "same"}))])  # FakeProvider repeats this forever
    conv = Conversation(store=store, user="alice")
    agent = Agent(conversation=conv, provider=provider, registry=registry, max_turns=6)
    await conv.add_user_message("loop please")
    result = await agent.run()

    assert result.status == "max_turns"
    assert result.turns_used == 6


async def test_max_turns_reached_without_crashing():
    store = MemoryStore()
    registry = make_registry_with_echo_tool()
    # Always returns tool calls with slightly different args each time,
    # so loop detection never trips — should hit max_turns cleanly instead.
    turns = [tool_turn((f"c{i}", "echo", {"text": f"v{i}"})) for i in range(10)]
    provider = FakeProvider(turns)
    conv = Conversation(store=store, user="alice")
    agent = Agent(conversation=conv, provider=provider, registry=registry, max_turns=3)
    await conv.add_user_message("go forever")
    result = await agent.run()

    assert result.status == "max_turns"
    assert result.turns_used == 3


async def test_provider_error_returns_clean_error_result():
    store = MemoryStore()
    provider = FakeProvider([ScriptedTurn(raise_error=RuntimeError("network exploded"))])
    conv = Conversation(store=store, user="alice")
    agent = Agent(conversation=conv, provider=provider, registry=ToolRegistry())
    await conv.add_user_message("hi")
    result = await agent.run()

    assert result.status == "error"
    assert "network exploded" in result.error


async def test_chain_broken_handled_gracefully():
    store = MemoryStore()
    provider = FakeProvider([ScriptedTurn(content="partial...", chain_break={"reason": "content_filter"})])
    conv = Conversation(store=store, user="alice")
    agent = Agent(conversation=conv, provider=provider, registry=ToolRegistry())
    await conv.add_user_message("trigger a filter")
    result = await agent.run()

    assert result.status == "error"
    assert result.error == "content_filter"
    # the partial content should still have been persisted
    assert conv.session.messages[-1].content == "partial..."


async def test_stop_requested_halts_run():
    store = MemoryStore()
    from astra.signals import InMemorySignalStore

    signals = InMemorySignalStore()
    registry = make_registry_with_echo_tool()
    provider = FakeProvider([tool_turn(("c1", "echo", {"text": "x"})), text_turn("should not get here")])
    conv = Conversation(store=store, user="alice", signals=signals)
    conv.request_stop()  # simulate the user hitting "stop" before the run even starts

    agent = Agent(conversation=conv, provider=provider, registry=registry)
    await conv.add_user_message("go")
    result = await agent.run()

    assert result.status == "stopped"


async def test_broken_callback_does_not_crash_run():
    store = MemoryStore()

    def broken_on_token(delta, **kw):
        raise RuntimeError("callback bug")

    def broken_on_done(response, **kw):
        raise RuntimeError("another callback bug")

    callbacks = Callbacks(on_token=broken_on_token, on_done=broken_on_done)
    provider = FakeProvider([text_turn("fine despite broken callbacks")])
    conv = Conversation(store=store, user="alice", callbacks=callbacks)
    agent = Agent(conversation=conv, provider=provider, registry=ToolRegistry())
    await conv.add_user_message("hi")
    result = await agent.run()

    assert result.status == "done"
    assert result.final_text == "fine despite broken callbacks"


async def test_all_callback_hooks_fire():
    store = MemoryStore()
    registry = make_registry_with_echo_tool()
    events = []
    callbacks = Callbacks(on_event=lambda type, **kw: events.append(type))
    provider = FakeProvider([tool_turn(("c1", "echo", {"text": "x"})), text_turn("done")])
    conv = Conversation(store=store, user="alice", callbacks=callbacks)
    agent = Agent(conversation=conv, provider=provider, registry=registry)
    await conv.add_user_message("hi")
    await agent.run()

    assert "on_message" in events  # user message
    assert "on_tool_start" in events
    assert "on_tool_result" in events
    assert "on_done" in events


async def test_sync_and_async_callbacks_both_work():
    store = MemoryStore()
    seen = []

    def sync_hook(response, **kw):
        seen.append(("sync", response))

    async def async_hook(delta, **kw):
        seen.append(("async", delta))

    callbacks = Callbacks(on_done=sync_hook, on_token=async_hook)
    provider = FakeProvider([text_turn("hello")])
    conv = Conversation(store=store, user="alice", callbacks=callbacks)
    agent = Agent(conversation=conv, provider=provider, registry=ToolRegistry())
    await conv.add_user_message("hi")
    await agent.run()

    kinds = {k for k, _ in seen}
    assert kinds == {"sync", "async"}


async def test_default_memory_store_when_none_passed():
    conv = Conversation(user="alice")  # no store= at all
    await conv.add_user_message("hello, defaulting")
    assert isinstance(conv.store, MemoryStore)
    # regression test: add_user_message must checkpoint immediately
    reloaded = conv.store.get(conv.session_id)
    assert reloaded is not None
    assert reloaded.messages[-1].content == "hello, defaulting"


async def test_sqlite_store_persists_across_instances(tmp_path):
    db_path = tmp_path / "sessions.db"
    store1 = SQLiteStore(str(db_path))
    conv1 = Conversation(store=store1, user="alice")
    provider = FakeProvider([text_turn("saved to disk")])
    agent1 = Agent(conversation=conv1, provider=provider, registry=ToolRegistry())
    await conv1.add_user_message("hi")
    await agent1.run()
    store1.close()

    # A fresh SQLiteStore instance pointing at the same file — proves
    # this is real durability, not just a shared in-process object.
    store2 = SQLiteStore(str(db_path))
    reloaded = store2.get(conv1.session_id)
    assert reloaded is not None
    assert reloaded.messages[-1].content == "saved to disk"
