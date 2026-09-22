"""Tests for the scale/safety layer: concurrency primitives, session
guard/ownership, optimistic-concurrency storage, context management,
and the shared ToolRuntime wired through a real multi-user Agent run."""

import asyncio
import time

import pytest

from astra.agent.agent import Agent
from astra.agent.conversation import Conversation
from astra.concurrency import CircuitBreaker, Limiter, LimiterBusy
from astra.context import ContextManager, ContextPolicy
from astra.sessions import SessionBusyError, SessionGuard, SessionOwnershipError, check_owner
from astra.storage import ConcurrentModificationError, MemoryStore, SQLiteStore
from astra.tools.decorator import ToolRegistry, tool

from .conftest import FakeProvider, text_turn, tool_turn


# ── Limiter ──

@pytest.mark.asyncio
async def test_limiter_caps_concurrency():
    lim = Limiter(2)
    active = 0
    peak = 0

    async def work():
        nonlocal active, peak
        async with lim.slot():
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.02)
            active -= 1

    await asyncio.gather(*(work() for _ in range(10)))
    assert peak == 2


@pytest.mark.asyncio
async def test_limiter_busy_on_full_queue():
    lim = Limiter(1, max_queue=0)
    async with lim.slot():
        with pytest.raises(LimiterBusy):
            await lim.acquire(timeout=0.05)


@pytest.mark.asyncio
async def test_limiter_no_leak_on_cancel():
    lim = Limiter(1)
    await lim.acquire()
    waiter = asyncio.ensure_future(lim.acquire())
    await asyncio.sleep(0.01)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    lim.release()
    assert lim.active == 0
    assert lim.waiting == 0


def test_limiter_works_across_separate_event_loops():
    lim = Limiter(1)

    async def use():
        async with lim.slot():
            await asyncio.sleep(0.001)

    for _ in range(3):
        asyncio.run(use())
    assert lim.active == 0


# ── CircuitBreaker ──

def test_circuit_breaker_opens_and_recovers():
    br = CircuitBreaker(failure_threshold=2, cooldown=0.05)
    assert br.allow()
    br.record_failure()
    assert br.allow()  # still under threshold
    br.record_failure()
    assert not br.allow()  # now open
    time.sleep(0.06)
    assert br.allow()  # half-open: exactly one probe allowed
    assert not br.allow()  # second concurrent probe denied
    br.record_success()
    assert br.allow()
    assert br.state == "closed"


# ── SessionGuard: the lost-update fix ──

@pytest.mark.asyncio
async def test_session_guard_prevents_concurrent_runs():
    guard = SessionGuard()
    order = []

    async def hold(tag):
        async with guard.hold("s1"):
            order.append(f"{tag}-start")
            await asyncio.sleep(0.05)
            order.append(f"{tag}-end")

    t1 = asyncio.ensure_future(hold("A"))
    await asyncio.sleep(0.01)  # let A acquire the lock
    with pytest.raises(SessionBusyError):
        async with guard.hold("s1", wait=0.0):
            pass  # a second run on the SAME session is rejected, not interleaved
    await t1
    assert order == ["A-start", "A-end"]


@pytest.mark.asyncio
async def test_session_guard_releases_after_use():
    guard = SessionGuard()
    async with guard.hold("s1"):
        pass
    # should be immediately acquirable again
    async with guard.hold("s1", wait=0.0):
        pass


def test_check_owner_rejects_mismatch():
    with pytest.raises(SessionOwnershipError):
        check_owner("alice", "mallory", "sess1")


def test_check_owner_requires_user():
    with pytest.raises(SessionOwnershipError):
        check_owner("alice", None, "sess1")


def test_check_owner_allows_match():
    check_owner("alice", "alice", "sess1")  # does not raise


# ── Conversation ownership enforcement ──

def test_conversation_enforce_owner_blocks_other_user():
    store = MemoryStore()
    c1 = Conversation(store=store, user="alice")
    c1.session.messages  # touch
    c1.save()
    sid = c1.session_id
    with pytest.raises(SessionOwnershipError):
        Conversation(store=store, session_id=sid, user="mallory", enforce_owner=True)
    # legitimate owner still works
    Conversation(store=store, session_id=sid, user="alice", enforce_owner=True)


def test_conversation_enforce_owner_off_by_default_matches_old_behavior():
    store = MemoryStore()
    c1 = Conversation(store=store, user="alice")
    c1.save()
    sid = c1.session_id
    # No exception without enforce_owner=True (backward compatible)
    Conversation(store=store, session_id=sid, user="mallory")


# ── SQLiteStore: WAL + optimistic concurrency ──

def test_sqlite_store_wal_mode_enabled(tmp_path):
    store = SQLiteStore(str(tmp_path / "t.db"))
    mode = store._conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"


def test_sqlite_store_detects_lost_update(tmp_path):
    path = str(tmp_path / "t.db")
    store = SQLiteStore(path)
    c1 = Conversation(store=store, user="alice")
    c1.save()
    sid = c1.session_id

    # Two independent loads of the same session
    a = Conversation(store=store, session_id=sid, user="alice")
    b = Conversation(store=store, session_id=sid, user="alice")
    a.save()  # a writes first: version bumps
    with pytest.raises(ConcurrentModificationError):
        b.save()  # b's base version is now stale


def test_sqlite_store_roundtrip_after_migration(tmp_path):
    """A DB file from a version without the `version` column still opens."""
    path = str(tmp_path / "old.db")
    import sqlite3

    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE astra_sessions (
            session_id TEXT PRIMARY KEY,
            user TEXT,
            last_active REAL,
            ended_reason TEXT,
            data TEXT NOT NULL
        );
        """
    )
    conn.commit()
    conn.close()
    store = SQLiteStore(path)  # should migrate, not crash
    c = Conversation(store=store, user="bob")
    c.save()
    assert store.get(c.session_id) is not None


# ── ContextManager ──

def test_context_manager_no_trim_under_budget():
    cm = ContextManager(ContextPolicy(max_chars=10_000))
    messages = [{"role": "user", "content": "hi"}]
    assert cm.prepare(messages) == messages


def test_context_manager_trims_old_tool_results_over_budget():
    policy = ContextPolicy(max_chars=500, keep_last_tool_results=1, trigger_ratio=0.5)
    cm = ContextManager(policy)
    messages = [{"role": "user", "content": "go"}]
    for i in range(10):
        messages.append({"role": "assistant", "content": "", "tool_calls": [{"id": f"c{i}"}]})
        messages.append({"role": "tool", "tool_call_id": f"c{i}", "content": "y" * 100})
    out = cm.prepare(messages, None)
    tool_msgs = [m for m in out if m["role"] == "tool"]
    cleared = [m for m in tool_msgs if m["content"] == policy.clear_placeholder]
    kept = [m for m in tool_msgs if m["content"] != policy.clear_placeholder]
    assert cleared  # something was cleared
    assert len(kept) <= policy.keep_last_tool_results + 1  # newest preserved


def test_context_manager_caps_giant_single_result():
    policy = ContextPolicy(max_chars=10_000, max_tool_result_chars=50, trigger_ratio=0.01)
    cm = ContextManager(policy)
    messages = [{"role": "tool", "tool_call_id": "c1", "content": "z" * 5000}]
    out = cm.prepare(messages, None)
    assert len(out[0]["content"]) < 5000


# ── Full multi-user Agent runs on a shared ToolRuntime ──

@pytest.mark.asyncio
async def test_many_concurrent_sessions_share_one_runtime():
    from astra.tools.runtime import ToolRuntime

    reg = ToolRegistry()
    from astra.tools.core import Tool

    async def echo(args, ctx):
        await asyncio.sleep(0.01)
        return {"echoed": args.get("n")}

    reg.register(Tool(name="echo", description="d", parameters={"type": "object", "properties": {"n": {"type": "integer"}}}, handler=echo))
    runtime = ToolRuntime(max_concurrent_tools=1000)
    store = MemoryStore()

    async def one_session(i):
        conv = Conversation(store=store, user=f"user{i}")
        provider = FakeProvider([tool_turn((f"c{i}", "echo", {"n": i})), text_turn("done")])
        agent = Agent(conversation=conv, provider=provider, registry=reg, runtime=runtime)
        await conv.add_user_message("go")
        return await agent.run()

    results = await asyncio.gather(*(one_session(i) for i in range(100)))
    assert all(r.status == "done" for r in results)
    assert runtime.tool_limiter.active == 0  # no leaked slots


@pytest.mark.asyncio
async def test_run_timeout_aborts_and_records_pending_tool_calls():
    reg = ToolRegistry()
    from astra.tools.core import Tool

    async def slow(args, ctx):
        await asyncio.sleep(10)

    reg.register(Tool(name="slow", description="d", parameters={"type": "object", "properties": {}}, handler=slow))
    store = MemoryStore()
    conv = Conversation(store=store, user="u")
    provider = FakeProvider([tool_turn(("c1", "slow", {}))])
    agent = Agent(conversation=conv, provider=provider, registry=reg, run_timeout=0.2)
    await conv.add_user_message("go")
    result = await agent.run()
    assert result.status == "error"
    assert result.error == "run_timeout"
    # the tool call should be resolved (not left "pending" forever)
    assert all(tc.status != "pending" for tc in conv.session.tool_calls)
