"""Tests for astra.group — the multi-agent shared-thread chat feature."""

import asyncio
import json

import pytest

from astra.group.chat import GroupChat, GroupParticipant
from astra.group.models import GroupMessage, GroupSession
from astra.group.projection import USER_AUTHOR, project_for_speaker
from astra.group.selection import (
    FunctionSelector,
    MentionSelector,
    ModeratorSelector,
    RoundRobinSelector,
    StaticSelector,
    parse_mentions,
)
from astra.group.store import GroupMemoryStore, GroupSQLiteStore
from astra.group.termination import KeywordTermination, MaxRoundsTermination, with_hard_cap
from astra.sessions import SessionBusyError, SessionOwnershipError
from astra.tools.core import Tool
from astra.tools.decorator import ToolRegistry

from .conftest import FakeProvider, text_turn, tool_turn


def make_participant(name, script, registry=None):
    return GroupParticipant(
        name=name,
        system_prompt=f"You are {name}.",
        provider=FakeProvider(script),
        registry=registry or ToolRegistry(),
        description=f"{name} the agent",
    )


def new_gc(names, scripts, **kwargs):
    group = GroupSession(group_id="g1", user="alice", participants=names)
    parts = {n: make_participant(n, scripts[n]) for n in names}
    return GroupChat(group, parts, **kwargs)


# ── projection: the crux (role re-projection + no cross-provider tool replay) ──

def test_projection_own_turns_are_assistant_others_are_user():
    g = GroupSession(group_id="g", participants=["alice", "bob"])
    g.messages = [
        GroupMessage(message_id="1", author=USER_AUTHOR, content="hi"),
        GroupMessage(message_id="2", author="alice", content="hello from alice"),
        GroupMessage(message_id="3", author="bob", content="hello from bob"),
    ]
    view = project_for_speaker(g, "alice")
    roles = [(m["role"], m["content"]) for m in view]
    assert ("user", "hi") in roles
    assert ("assistant", "hello from alice") in roles
    assert any(r == "user" and "[bob]:" in c for r, c in roles)  # bob's turn is "user" from alice's POV


def test_projection_tool_activity_flattened_to_text_not_structured_blocks():
    from astra.group.models import GroupToolCall

    g = GroupSession(group_id="g", participants=["alice"])
    g.messages = [
        GroupMessage(message_id="1", author="alice", content="done", tool_calls=[GroupToolCall(tool_name="search", summary="3 results", ok=True)]),
    ]
    view = project_for_speaker(g, "bob")
    assert len(view) == 1
    assert view[0]["role"] == "user"
    assert "search" in view[0]["content"]
    # No tool_calls / tool-role blocks anywhere — flattened to plain text only.
    assert "tool_calls" not in view[0]
    assert not any(m["role"] == "tool" for m in view)


def test_projection_empty_messages_skipped():
    g = GroupSession(group_id="g", participants=["alice"])
    g.messages = [GroupMessage(message_id="1", author="alice", content="")]
    assert project_for_speaker(g, "alice") == []


# ── selection ──

def test_mention_selector_no_mention_uses_default():
    g = GroupSession(group_id="g", participants=["alice", "bob"])
    g.messages = [GroupMessage(message_id="1", author=USER_AUTHOR, content="hello")]
    sel = MentionSelector(["alice", "bob"])
    result = asyncio.run(sel.select(g, trigger_author=USER_AUTHOR))
    assert result == ["alice"]


def test_mention_selector_respects_mentions_and_order():
    g = GroupSession(group_id="g", participants=["alice", "bob", "carl"])
    g.messages = [GroupMessage(message_id="1", author=USER_AUTHOR, content="@bob then @alice please")]
    sel = MentionSelector(["alice", "bob", "carl"])
    result = asyncio.run(sel.select(g, trigger_author=USER_AUTHOR))
    assert result == ["bob", "alice"]


def test_round_robin_selector_cycles():
    g = GroupSession(group_id="g", participants=["a", "b", "c"])
    sel = RoundRobinSelector(["a", "b", "c"])
    g.last_speaker = None
    assert asyncio.run(sel.select(g, trigger_author="")) == ["a"]
    g.last_speaker = "a"
    assert asyncio.run(sel.select(g, trigger_author="")) == ["b"]
    g.last_speaker = "c"
    assert asyncio.run(sel.select(g, trigger_author="")) == ["a"]


def test_function_selector_wraps_custom_logic():
    def picker(session, trigger):
        return ["bob"] if len(session.messages) < 2 else []

    sel = FunctionSelector(picker)
    g = GroupSession(group_id="g", participants=["bob"])
    assert asyncio.run(sel.select(g, trigger_author="")) == ["bob"]


@pytest.mark.asyncio
async def test_moderator_selector_uses_llm_and_matches_name():
    provider = FakeProvider([text_turn("bob")])
    sel = ModeratorSelector(["alice", "bob"], {"alice": "a", "bob": "b"}, provider)
    g = GroupSession(group_id="g", participants=["alice", "bob"])
    result = await sel.select(g, trigger_author=USER_AUTHOR)
    assert result == ["bob"]


@pytest.mark.asyncio
async def test_moderator_selector_returns_empty_on_hallucinated_name():
    provider = FakeProvider([text_turn("nonexistent-agent")])
    sel = ModeratorSelector(["alice", "bob"], {}, provider)
    g = GroupSession(group_id="g", participants=["alice", "bob"])
    result = await sel.select(g, trigger_author=USER_AUTHOR)
    assert result == []


# ── termination ──

def test_max_rounds_always_applies_even_with_custom_condition():
    cond = with_hard_cap(KeywordTermination(["never matches"]), max_rounds=2)
    g = GroupSession(group_id="g", round=2)
    assert cond.should_stop(g) == "max_rounds"


def test_keyword_termination_fires():
    cond = KeywordTermination(["APPROVED"])
    g = GroupSession(group_id="g")
    g.messages = [GroupMessage(message_id="1", author="bob", content="Looks good, APPROVED")]
    assert cond.should_stop(g) == "keyword"


# ── store ──

def test_group_memory_store_roundtrip():
    store = GroupMemoryStore()
    g = GroupSession(group_id="g1", user="alice")
    store.save(g)
    loaded = store.get("g1")
    assert loaded.user == "alice"


def test_group_sqlite_store_roundtrip(tmp_path):
    store = GroupSQLiteStore(str(tmp_path / "g.db"))
    g = GroupSession(group_id="g1", user="bob", participants=["x", "y"])
    g.messages.append(GroupMessage(message_id="m1", author="x", content="hi"))
    store.save(g)
    loaded = store.get("g1")
    assert loaded.participants == ["x", "y"]
    assert loaded.messages[0].content == "hi"
    store.close()


def test_group_sqlite_store_wal_mode(tmp_path):
    store = GroupSQLiteStore(str(tmp_path / "g.db"))
    mode = store._conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"
    store.close()


# ── GroupChat end-to-end ──

@pytest.mark.asyncio
async def test_send_default_mention_routes_to_default_participant():
    gc = new_gc(["alice", "bob"], {"alice": [text_turn("hi from alice")], "bob": [text_turn("hi from bob")]})
    result = await gc.send("hello team")
    assert result.status == "done"
    assert len(result.turns) == 1
    assert result.turns[0].speaker == "alice"  # default = first participant
    assert result.final_text == "hi from alice"


@pytest.mark.asyncio
async def test_send_explicit_mention_routes_correctly():
    gc = new_gc(["alice", "bob"], {"alice": [text_turn("nope")], "bob": [text_turn("hi from bob")]})
    result = await gc.send("@bob help me")
    assert [t.speaker for t in result.turns] == ["bob"]
    assert result.final_text == "hi from bob"


@pytest.mark.asyncio
async def test_send_at_everyone_runs_all_sequentially_once():
    gc = new_gc(["alice", "bob"], {"alice": [text_turn("A")], "bob": [text_turn("B")]})
    result = await gc.send("@everyone status update")
    assert [t.speaker for t in result.turns] == ["alice", "bob"]


@pytest.mark.asyncio
async def test_single_pass_default_does_not_chain_replies():
    """Default mode: selector runs once per user message. A bot's reply
    does not itself trigger another selection round."""
    gc = new_gc(["alice"], {"alice": [text_turn("ok")]})
    result = await gc.send("hi")
    assert len(result.turns) == 1
    assert gc.group.round == 1


@pytest.mark.asyncio
async def test_agent_triggered_rounds_opt_in_chains_with_hard_cap():
    gc = new_gc(
        ["alice", "bob"],
        {"alice": [text_turn("passing to bob")] * 5, "bob": [text_turn("passing to alice")] * 5},
        selector=RoundRobinSelector(["alice", "bob"]),
        allow_agent_triggered_rounds=True,
        max_rounds=4,
    )
    result = await gc.send("start")
    assert result.status == "max_rounds"
    assert len(result.turns) == 4  # capped, never ran away


@pytest.mark.asyncio
async def test_no_immediate_repeat_guard_blocks_self_retrigger():
    """RoundRobin with a single participant would otherwise select the
    same speaker forever; the guard should stop it after one turn even
    in agent-triggered mode."""
    gc = new_gc(
        ["alice"],
        {"alice": [text_turn("talking to myself")] * 3},
        selector=StaticSelector(["alice"]),
        allow_agent_triggered_rounds=True,
        max_rounds=10,
    )
    result = await gc.send("start")
    assert len(result.turns) == 1  # blocked from re-triggering itself


@pytest.mark.asyncio
async def test_group_tool_calls_execute_and_replay_as_summary():
    registry = ToolRegistry()

    async def lookup(args, ctx):
        return {"found": args.get("q")}

    registry.register(Tool(name="lookup", description="d", parameters={"type": "object", "properties": {"q": {"type": "string"}}}, handler=lookup))
    alice = make_participant(
        "alice",
        [tool_turn(("c1", "lookup", {"q": "widgets"})), text_turn("found it")],
        registry=registry,
    )
    group = GroupSession(group_id="g1", user="u", participants=["alice"])
    gc = GroupChat(group, {"alice": alice})
    result = await gc.send("find widgets")
    assert result.status == "done"
    msg = result.turns[0].message
    assert msg.content == "found it"
    assert msg.tool_calls and msg.tool_calls[0].tool_name == "lookup"
    assert msg.tool_calls[0].ok


@pytest.mark.asyncio
async def test_group_checkpoints_after_every_speaker_turn():
    """Crash-safety: a group's state is persisted incrementally, not only
    at the very end (same discipline as Conversation._checkpoint)."""
    store = GroupMemoryStore()
    gc = new_gc(["alice", "bob"], {"alice": [text_turn("A")], "bob": [text_turn("B")]}, store=store)
    saved_versions = []
    orig_save = store.save

    def spy(g):
        saved_versions.append(len(g.messages))
        orig_save(g)

    store.save = spy
    await gc.send("@everyone go")
    assert len(saved_versions) >= 3  # user msg + alice turn + bob turn, each its own save


@pytest.mark.asyncio
async def test_second_concurrent_send_on_same_group_is_rejected():
    gc = new_gc(["alice"], {"alice": [text_turn("slow reply")]})

    async def slow_generate(messages, tools=None, **kw):
        await asyncio.sleep(0.2)
        return {"role": "assistant", "content": "done"}, None

    gc.participants["alice"].provider.generate = slow_generate
    t1 = asyncio.ensure_future(gc.send("go"))
    await asyncio.sleep(0.02)
    result2 = await gc.send("also go")
    assert result2.status == "busy"
    await t1


@pytest.mark.asyncio
async def test_ownership_enforced_when_enabled():
    group = GroupSession(group_id="g1", user="alice", participants=["a"])
    parts = {"a": make_participant("a", [text_turn("hi")])}
    gc = GroupChat(group, parts, enforce_owner=True)
    with pytest.raises(SessionOwnershipError):
        await gc.send("hi", user="mallory")
    result = await gc.send("hi", user="alice")
    assert result.status == "done"


@pytest.mark.asyncio
async def test_load_resumes_from_store_and_checks_ownership(tmp_path):
    store = GroupSQLiteStore(str(tmp_path / "g.db"))
    group = GroupSession(group_id="g1", user="alice", participants=["a"])
    store.save(group)
    parts = {"a": make_participant("a", [text_turn("hi")])}
    gc = GroupChat.load("g1", parts, store=store, user="alice", enforce_owner=True)
    result = await gc.send("hi", user="alice")
    assert result.status == "done"
    with pytest.raises(SessionOwnershipError):
        GroupChat.load("g1", parts, store=store, user="mallory", enforce_owner=True)
    store.close()


@pytest.mark.asyncio
async def test_quota_exceeded_stops_the_turn_cleanly():
    from astra.quota import InMemoryQuota, QuotaLimits

    quota = InMemoryQuota(per_user=QuotaLimits(max_requests=0))
    gc = new_gc(["alice"], {"alice": [text_turn("should not run")]}, quota=quota)
    result = await gc.send("hi")
    assert result.turns[0].stopped
    assert "quota" in result.turns[0].message.content


@pytest.mark.asyncio
async def test_broadcast_storm_shape_is_capped_not_exponential():
    """Regression guard for the documented AutoGen-style failure mode: a
    selector that always nominates every participant (worst-case shape
    for exponential blow-up: N agents, each turn re-triggers all N) must
    still be bounded by max_rounds, not grow without limit."""
    names = ["alice", "bob", "carl"]
    scripts = {n: [text_turn(f"reply from {n}")] * 20 for n in names}
    gc = new_gc(
        names,
        scripts,
        selector=StaticSelector(names),  # "everyone always speaks" — the storm shape
        allow_agent_triggered_rounds=True,
        max_rounds=9,
    )
    result = await gc.send("go")
    assert len(result.turns) <= 9
    assert result.status == "max_rounds"
    assert gc.group.round <= 9


# ── Astra integration (make_group / load_group share the app's runtime) ──

@pytest.mark.asyncio
async def test_astra_make_group_shares_runtime_and_defaults_mention_selector():
    from astra import create_astra
    from astra.group.chat import GroupParticipant

    app = create_astra(provider="openai", api_key="sk-test", tools_dir=None)
    alice = GroupParticipant(name="alice", system_prompt="You are alice.", provider=FakeProvider([text_turn("hi")]), registry=ToolRegistry())
    gc = app.make_group({"alice": alice}, user="bob")
    assert gc.runtime is app.runtime  # shared, not a second independent limiter set
    result = await gc.send("hello")
    assert result.status == "done"
    assert result.turns[0].speaker == "alice"


@pytest.mark.asyncio
async def test_astra_load_group_resumes_by_id():
    from astra import create_astra
    from astra.group.chat import GroupParticipant

    app = create_astra(provider="openai", api_key="sk-test", tools_dir=None)
    alice = GroupParticipant(name="alice", system_prompt="You are alice.", provider=FakeProvider([text_turn("first")]), registry=ToolRegistry())
    gc = app.make_group({"alice": alice}, user="bob", group_id="fixed-id")
    await gc.send("hi")

    alice2 = GroupParticipant(name="alice", system_prompt="You are alice.", provider=FakeProvider([text_turn("second")]), registry=ToolRegistry())
    gc2 = app.load_group("fixed-id", {"alice": alice2}, user="bob")
    assert len(gc2.group.messages) == 2  # user msg + alice's first reply, carried over
    result = await gc2.send("again")
    assert result.turns[0].message.content == "second"
