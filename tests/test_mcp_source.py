"""Tests for McpSource against a real (local, stdio) MCP server.

Skipped automatically if the `mcp` package isn't installed — it's an
optional extra (pip install astra[mcp]).
"""

import asyncio
import sys
import time
from pathlib import Path

import pytest
import pytest_asyncio

mcp = pytest.importorskip("mcp")

from astra.tools.core import ToolContext
from astra.tools.decorator import ToolRegistry
from astra.tools.executor import ToolExecutor
from astra.tools.mcp_source import McpServerConfig, McpSource, namespaced, schema_digest

FIXTURE_SERVER = Path(__file__).parent / "fixtures" / "mcp_demo_server.py"


@pytest_asyncio.fixture
async def demo_source():
    src = McpSource([McpServerConfig(name="demo", command=sys.executable, args=[str(FIXTURE_SERVER)], timeout=5)])
    reg = ToolRegistry()
    await src.start(reg)
    yield src, reg
    await src.aclose()


def test_namespacing_is_collision_resistant_and_sanitized():
    assert namespaced("my server", "do thing") == "my_server__do_thing"
    long_name = namespaced("a" * 100, "tool")
    assert len(long_name) <= 64


def test_schema_digest_changes_with_description():
    d1 = schema_digest("t", "desc one", {"type": "object"})
    d2 = schema_digest("t", "desc two", {"type": "object"})
    assert d1 != d2


@pytest.mark.asyncio
async def test_mcp_tools_registered_and_sorted(demo_source):
    src, reg = demo_source
    names = [s["function"]["name"] for s in reg.get_tool_schemas()]
    assert "demo__add" in names
    assert names == sorted(names)


@pytest.mark.asyncio
async def test_mcp_call_success(demo_source):
    src, reg = demo_source
    ex = ToolExecutor(reg)
    wire = await ex._dispatch("demo__add", {"a": 2, "b": 3})
    assert wire == "5"


@pytest.mark.asyncio
async def test_mcp_call_error_surfaces_as_tool_error(demo_source):
    src, reg = demo_source
    ex = ToolExecutor(reg)
    result = await ex.execute("demo__fail", {})
    assert not result.ok
    assert result.error_type == "tool_error"


@pytest.mark.asyncio
async def test_mcp_call_bad_args_is_invalid_args(demo_source):
    src, reg = demo_source
    ex = ToolExecutor(reg)
    result = await ex.execute("demo__add", {"a": "not a number"})
    assert not result.ok
    assert result.error_type == "invalid_args"


@pytest.mark.asyncio
async def test_mcp_call_deadline_times_out(demo_source):
    src, reg = demo_source
    ex = ToolExecutor(reg)
    ctx = ToolContext(deadline=time.monotonic() + 0.3)
    result = await ex.execute("demo__slow", {"seconds": 3}, ctx=ctx)
    assert not result.ok
    assert result.error_type == "timeout"
    # connection survives a timed-out call — subsequent calls still work
    result2 = await ex.execute("demo__add", {"a": 1, "b": 1})
    assert result2.ok


@pytest.mark.asyncio
async def test_mcp_server_that_fails_to_start_does_not_block_others():
    src = McpSource(
        [
            McpServerConfig(name="demo", command=sys.executable, args=[str(FIXTURE_SERVER)], timeout=5),
            McpServerConfig(name="bad", command="/definitely/not/a/real/binary"),
        ]
    )
    reg = ToolRegistry()
    names = await src.start(reg)
    assert any(n.startswith("demo__") for n in names)
    assert "bad" in src.errors
    await src.aclose()


@pytest.mark.asyncio
async def test_mcp_schema_change_quarantines_tool(tmp_path):
    """A tool whose description changes after first connect (default
    on_schema_change='block') is removed rather than silently updated —
    protection against a compromised/updated server changing what it
    tells the model to do."""
    server_v1 = tmp_path / "srv.py"
    server_v1.write_text(FIXTURE_SERVER.read_text())
    cfg = McpServerConfig(name="demo", command=sys.executable, args=[str(server_v1)], timeout=5, refresh_seconds=0)
    src = McpSource([cfg])
    reg = ToolRegistry()
    await src.start(reg)
    assert "demo__add" in reg.tools

    # Simulate the server changing its description for the same tool name.
    original = server_v1.read_text()
    server_v1.write_text(original.replace('"""Add two numbers."""', '"""Add two numbers (now with extra side effects)."""'))
    await src.aclose()
    src2 = McpSource([cfg])
    reg2 = ToolRegistry()
    reg2.tools_baseline_hack = None
    # reuse baseline from src to simulate "already connected once before"
    src2.baseline = dict(src.baseline)
    await src2.start(reg2)
    assert "demo__add" not in reg2.tools
    assert "demo__add" in src2.quarantined
    await src2.aclose()
