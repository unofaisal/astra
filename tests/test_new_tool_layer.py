"""Tests for the new tool layer: core/handlers/executor/process/manifest/validation."""

import asyncio
import json
import os
import stat
import sys

import pytest

from astra.tools.core import ToolAnnotations, ToolContext, ToolError, ToolResult, Tool
from astra.tools.decorator import ToolRegistry, define_tool, schema_from_function, tool
from astra.tools.executor import ToolExecutor, truncate_text
from astra.tools.handlers import ExecSpec, FunctionHandler, ProcessHandler, render_argv
from astra.tools.manifest import ManifestError, parse_manifest
from astra.tools.process import ProcessSpec, build_env, run_process, safe_resolve
from astra.tools.runtime import ToolRuntime
from astra.tools.validation import validate_args


# ── ToolResult wire format (must match _is_error_result's expectations) ──

def test_tool_result_success_wire_string():
    assert ToolResult.success("hello").to_wire() == "hello"


def test_tool_result_success_wire_dict_is_json():
    r = ToolResult.success({"a": 1})
    assert json.loads(r.to_wire()) == {"a": 1}


def test_tool_result_error_wire_has_error_key():
    r = ToolResult.error("boom", "tool_error", hint="try again")
    payload = json.loads(r.to_wire())
    assert payload["error"] == "boom"
    assert payload["error_type"] == "tool_error"
    assert payload["hint"] == "try again"


# ── FunctionHandler: legacy + new calling conventions ──

@pytest.mark.asyncio
async def test_function_handler_args_dict_convention():
    async def old_style(args, **kw):
        return {"got": args}

    h = FunctionHandler(old_style)
    result = await h({"x": 1}, ToolContext())
    assert result == {"got": {"x": 1}}


@pytest.mark.asyncio
async def test_function_handler_kwargs_convention():
    async def new_style(city: str, units: str = "c"):
        return f"{city}:{units}"

    h = FunctionHandler(new_style)
    result = await h({"city": "Nairobi"}, ToolContext())
    assert result == "Nairobi:c"


@pytest.mark.asyncio
async def test_function_handler_ctx_injection():
    async def with_ctx(x: int, ctx: ToolContext):
        return ctx.session_id

    h = FunctionHandler(with_ctx)
    result = await h({"x": 1}, ToolContext(session_id="s1"))
    assert result == "s1"


@pytest.mark.asyncio
async def test_function_handler_sync_function_runs():
    def sync_fn(x: int):
        return x * 2

    h = FunctionHandler(sync_fn)
    result = await h({"x": 3}, ToolContext())
    assert result == 6


# ── define_tool: schema from type hints ──

def test_schema_from_function_types_and_required():
    def f(city: str, count: int = 3):
        """Do a thing.

        Args:
            city: the city
        """

    schema = schema_from_function(f)
    assert schema["properties"]["city"]["type"] == "string"
    assert schema["properties"]["city"]["description"] == "the city"
    assert schema["properties"]["count"]["type"] == "integer"
    assert schema["required"] == ["city"]


@pytest.mark.asyncio
async def test_define_tool_end_to_end():
    @define_tool(read_only=True, idempotent=True)
    async def get_thing(name: str) -> dict:
        """Get a thing."""
        return {"name": name}

    assert isinstance(get_thing, Tool)
    assert get_thing.annotations.read_only
    reg = ToolRegistry()
    reg.register(get_thing)
    ex = ToolExecutor(reg)
    result = await ex.execute("get_thing", {"name": "x"})
    assert result.ok
    assert result.content == {"name": "x"}


# ── Executor pipeline ──

@pytest.mark.asyncio
async def test_executor_validates_args_before_calling_handler():
    called = False

    async def handler(args, ctx):
        nonlocal called
        called = True
        return "ok"

    t = Tool(name="needs_x", description="d", parameters={"type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"]}, handler=handler)
    reg = ToolRegistry()
    reg.register(t)
    ex = ToolExecutor(reg)
    result = await ex.execute("needs_x", {})
    assert not result.ok
    assert result.error_type == "invalid_args"
    assert not called


@pytest.mark.asyncio
async def test_executor_unknown_tool_lists_available():
    reg = ToolRegistry()
    reg.register(Tool(name="foo", description="d", parameters={}, handler=lambda a, c: "x"))
    ex = ToolExecutor(reg)
    result = await ex.execute("bar", {})
    assert result.error_type == "not_found"
    assert "foo" in result.message


@pytest.mark.asyncio
async def test_executor_non_idempotent_tool_not_retried_on_failure():
    calls = []

    async def flaky(args, ctx):
        calls.append(1)
        raise ToolError("nope", error_type="tool_error", retryable=True)

    t = Tool(name="flaky", description="d", parameters={"type": "object", "properties": {}}, handler=flaky, annotations=ToolAnnotations(idempotent=False))
    reg = ToolRegistry()
    reg.register(t)
    ex = ToolExecutor(reg, runtime=ToolRuntime(max_retries=5))
    result = await ex.execute("flaky", {})
    assert not result.ok
    assert len(calls) == 1  # NOT retried — this was the pre-existing bug


@pytest.mark.asyncio
async def test_executor_idempotent_tool_is_retried():
    calls = []

    async def flaky(args, ctx):
        calls.append(1)
        if len(calls) < 3:
            raise ToolError("nope", error_type="tool_error", retryable=True)
        return "ok"

    t = Tool(name="flaky", description="d", parameters={"type": "object", "properties": {}}, handler=flaky, annotations=ToolAnnotations(idempotent=True))
    reg = ToolRegistry()
    reg.register(t)
    ex = ToolExecutor(reg, runtime=ToolRuntime(max_retries=5))
    result = await ex.execute("flaky", {})
    assert result.ok
    assert len(calls) == 3


@pytest.mark.asyncio
async def test_executor_timeout():
    async def slow(args, ctx):
        await asyncio.sleep(5)

    t = Tool(name="slow", description="d", parameters={"type": "object", "properties": {}}, handler=slow, timeout=0.05)
    reg = ToolRegistry()
    reg.register(t)
    ex = ToolExecutor(reg)
    result = await ex.execute("slow", {})
    assert result.error_type == "timeout"


@pytest.mark.asyncio
async def test_executor_output_truncation():
    async def big(args, ctx):
        return "x" * 5000

    t = Tool(name="big", description="d", parameters={"type": "object", "properties": {}}, handler=big, max_output_chars=200)
    reg = ToolRegistry()
    reg.register(t)
    ex = ToolExecutor(reg)
    result = await ex.execute("big", {})
    assert result.truncated
    assert len(result.to_wire()) < 1000
    assert "truncated" in result.to_wire()


@pytest.mark.asyncio
async def test_executor_dict_error_convention_still_works():
    """Legacy tools returning {"error": ...} must still be treated as failures."""

    async def old(args, ctx):
        return {"error": "bad input"}

    reg = ToolRegistry()
    reg.register(Tool(name="old", description="d", parameters={"type": "object", "properties": {}}, handler=old))
    ex = ToolExecutor(reg)
    wire = await ex._dispatch("old", {})
    payload = json.loads(wire)
    assert payload["error"] == "bad input"


@pytest.mark.asyncio
async def test_executor_busy_when_concurrency_cap_hit():
    async def slow(args, ctx):
        await asyncio.sleep(0.3)
        return "ok"

    t = Tool(name="slow", description="d", parameters={"type": "object", "properties": {}}, handler=slow, max_concurrency=1)
    reg = ToolRegistry()
    reg.register(t)
    runtime = ToolRuntime(queue_timeout=0.01, max_queue=0)
    ex = ToolExecutor(reg, runtime=runtime)
    r1, r2 = await asyncio.gather(ex.execute("slow", {}), ex.execute("slow", {}))
    statuses = {r1.ok, r2.ok}
    errors = {r.error_type for r in (r1, r2) if not r.ok}
    assert not r1.ok or not r2.ok  # at least one was rejected
    assert "busy" in errors


@pytest.mark.asyncio
async def test_circuit_breaker_opens_after_failures():
    async def always_fails(args, ctx):
        raise ToolError("down", error_type="tool_error")

    t = Tool(name="fails", description="d", parameters={"type": "object", "properties": {}}, handler=always_fails, breaker=True)
    reg = ToolRegistry()
    reg.register(t)
    runtime = ToolRuntime(breaker_threshold=2, breaker_cooldown=60)
    ex = ToolExecutor(reg, runtime=runtime)
    for _ in range(2):
        r = await ex.execute("fails", {})
        assert not r.ok
    r = await ex.execute("fails", {})
    assert r.error_type == "unavailable"  # breaker open, handler not called again


@pytest.mark.asyncio
async def test_policy_hook_denies():
    async def h(args, ctx):
        return "ok"

    def deny_all(tool, args, ctx):
        return "not allowed here"

    reg = ToolRegistry()
    reg.register(Tool(name="t", description="d", parameters={"type": "object", "properties": {}}, handler=h))
    ex = ToolExecutor(reg, policy=deny_all)
    result = await ex.execute("t", {})
    assert result.error_type == "denied"
    assert result.message == "not allowed here"


def test_truncate_text_keeps_head_and_tail():
    text = "A" * 1000 + "B" * 1000
    out = truncate_text(text, 500)
    assert out.startswith("A")
    assert out.endswith("B")
    assert "truncated" in out


# ── Validation ──

def test_validate_args_missing_required():
    schema = {"type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"]}
    errs = validate_args(schema, {})
    assert errs and "x" in errs[0]


def test_validate_args_wrong_type():
    schema = {"type": "object", "properties": {"x": {"type": "integer"}}}
    errs = validate_args(schema, {"x": "not an int"})
    assert errs


def test_validate_args_enum():
    schema = {"type": "object", "properties": {"x": {"enum": ["a", "b"]}}}
    assert validate_args(schema, {"x": "c"})
    assert not validate_args(schema, {"x": "a"})


def test_validate_args_valid_passes():
    schema = {"type": "object", "properties": {"x": {"type": "integer"}}, "required": ["x"]}
    assert validate_args(schema, {"x": 5}) == []


# ── render_argv: no shell, option-injection guard ──

def test_render_argv_basic_substitution():
    argv = render_argv(["git", "show", "{ref}"], {"ref": "HEAD"})
    assert argv == ["git", "show", "HEAD"]


def test_render_argv_optional_omitted():
    argv = render_argv(["ls", "{path?}"], {})
    assert argv == ["ls"]


def test_render_argv_rejects_leading_dash_by_default():
    with pytest.raises(ToolError):
        render_argv(["cat", "{path}"], {"path": "-rf"})


def test_render_argv_allows_leading_dash_when_disabled():
    argv = render_argv(["cat", "{path}"], {"path": "-rf"}, reject_leading_dash=False)
    assert argv == ["cat", "-rf"]


def test_render_argv_list_expansion():
    argv = render_argv(["cmd", "{files*}"], {"files": ["a.txt", "b.txt"]})
    assert argv == ["cmd", "a.txt", "b.txt"]


def test_render_argv_conditional_flag():
    argv = render_argv(["cmd", {"when": "verbose", "args": ["-v"]}], {"verbose": True})
    assert argv == ["cmd", "-v"]
    argv2 = render_argv(["cmd", {"when": "verbose", "args": ["-v"]}], {"verbose": False})
    assert argv2 == ["cmd"]


# ── safe_resolve: path confinement ──

def test_safe_resolve_inside_root(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "file.txt").write_text("hi")
    resolved = safe_resolve("file.txt", str(root))
    assert resolved == str((root / "file.txt").resolve())


def test_safe_resolve_rejects_escape(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    assert safe_resolve("../../etc/passwd", str(root)) is None


def test_safe_resolve_rejects_symlink_escape(tmp_path):
    if os.name != "posix":
        pytest.skip("symlinks")
    root = tmp_path / "workspace"
    root.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("secret")
    link = root / "escape"
    link.symlink_to(outside)
    assert safe_resolve("escape", str(root)) is None


# ── run_process ──

@pytest.mark.asyncio
async def test_run_process_basic():
    r = await run_process(ProcessSpec(argv=["echo", "hello"], env=build_env()))
    assert r.ok
    assert r.text().strip() == "hello"


@pytest.mark.asyncio
async def test_run_process_timeout_kills():
    r = await run_process(ProcessSpec(argv=["sleep", "5"], timeout=0.2, grace=0.2, env=build_env()))
    assert r.timed_out
    assert r.killed


@pytest.mark.asyncio
async def test_run_process_missing_command():
    r = await run_process(ProcessSpec(argv=["this-does-not-exist-xyz"], env=build_env()))
    assert r.spawn_error is not None


@pytest.mark.asyncio
async def test_run_process_output_cap_truncates():
    r = await run_process(ProcessSpec(argv=["python3", "-c", "print('x'*10000)"], max_output_bytes=100, env=build_env()))
    assert r.stdout_truncated
    assert len(r.stdout) <= 100


@pytest.mark.asyncio
async def test_run_process_stdin():
    r = await run_process(ProcessSpec(argv=["cat"], stdin=b"piped in", env=build_env()))
    assert r.text().strip() == "piped in"


@pytest.mark.asyncio
async def test_run_process_no_shell_injection():
    """argv is exec'd directly — shell metacharacters are inert."""
    r = await run_process(ProcessSpec(argv=["echo", "hi; rm -rf /tmp/should-not-run"], env=build_env()))
    assert r.ok
    assert "should-not-run" in r.text()  # printed literally, not executed


@pytest.mark.asyncio
async def test_run_process_env_not_inherited_by_default():
    os.environ["ASTRA_TEST_SECRET"] = "should-not-leak"
    try:
        r = await run_process(ProcessSpec(argv=["python3", "-c", "import os; print(os.environ.get('ASTRA_TEST_SECRET', 'MISSING'))"], env=build_env()))
        assert r.text().strip() == "MISSING"
    finally:
        del os.environ["ASTRA_TEST_SECRET"]


@pytest.mark.asyncio
async def test_run_process_cancellation_kills_process():
    task = asyncio.ensure_future(run_process(ProcessSpec(argv=["sleep", "10"], timeout=30, env=build_env())))
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


# ── Manifest tools ──

def test_parse_manifest_valid():
    data = {
        "name": "run_echo",
        "description": "Echo a string.",
        "parameters": {"type": "object", "properties": {"msg": {"type": "string"}}, "required": ["msg"]},
        "exec": {"command": ["echo", "{msg}"]},
    }
    t = parse_manifest(data)
    assert t.name == "run_echo"
    assert isinstance(t.handler, ProcessHandler)


def test_parse_manifest_rejects_shell_string_command():
    with pytest.raises(ManifestError):
        parse_manifest({"name": "x", "description": "d", "exec": {"command": "echo hi"}})


def test_parse_manifest_rejects_unknown_placeholder():
    with pytest.raises(ManifestError):
        parse_manifest(
            {
                "name": "x",
                "description": "d",
                "parameters": {"type": "object", "properties": {}},
                "exec": {"command": ["echo", "{undeclared}"]},
            }
        )


def test_parse_manifest_rejects_reserved_param_name():
    with pytest.raises(ManifestError):
        parse_manifest(
            {
                "name": "x",
                "description": "d",
                "parameters": {"type": "object", "properties": {"workspace": {"type": "string"}}},
                "exec": {"command": ["echo", "{workspace}"]},
            }
        )


@pytest.mark.asyncio
async def test_manifest_tool_runs_end_to_end(tmp_path):
    data = {
        "name": "greet",
        "description": "Greet someone.",
        "parameters": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
        "exec": {"command": ["echo", "hello {name}"], "output": "text"},
    }
    t = parse_manifest(data)
    reg = ToolRegistry()
    reg.register(t)
    runtime = ToolRuntime(workspace=str(tmp_path), require_sandbox=False)
    ex = ToolExecutor(reg, runtime=runtime)
    result = await ex.execute("greet", {"name": "world"})
    assert result.ok
    assert "hello world" in result.content


@pytest.mark.asyncio
async def test_manifest_tool_path_param_confined_to_workspace(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    data = {
        "name": "cat_file",
        "description": "Cat a file.",
        "parameters": {"type": "object", "properties": {"path": {"type": "string", "x-astra-path": True}}, "required": ["path"]},
        "exec": {"command": ["cat", "{path}"]},
    }
    t = parse_manifest(data)
    reg = ToolRegistry()
    reg.register(t)
    runtime = ToolRuntime(workspace=str(ws))
    ex = ToolExecutor(reg, runtime=runtime)
    result = await ex.execute("cat_file", {"path": "../../../etc/passwd"})
    assert not result.ok  # path escape rejected before exec


# ── Loader: manifest + python tools together, deterministic order ──

def test_loader_loads_manifest_tools(tmp_path):
    from astra.tools.loader import load_tools

    group = tmp_path / "greeter"
    group.mkdir()
    (group / "tool.json").write_text(json.dumps({
        "name": "hello_tool",
        "description": "says hi",
        "exec": {"command": ["echo", "hi"]},
    }))
    reg = ToolRegistry()
    load_tools(reg, tmp_path)
    assert "hello_tool" in reg.tools


def test_registry_schema_order_is_deterministic():
    reg = ToolRegistry()
    for name in ["zebra", "apple", "mango"]:
        reg.register(Tool(name=name, description="d", parameters={"type": "object", "properties": {}}, handler=lambda a, c: "x"))
    names = [s["function"]["name"] for s in reg.get_tool_schemas()]
    assert names == sorted(names)


def test_registry_view_does_not_mutate_original():
    reg = ToolRegistry()
    reg.register(Tool(name="a", description="d", parameters={}, handler=lambda a, c: "x"))
    reg.register(Tool(name="b", description="d", parameters={}, handler=lambda a, c: "x"))
    view = reg.view(deny={"b"})
    assert "b" not in view.tools
    assert "b" in reg.tools  # original untouched
