"""Handlers: HOW a tool runs.

  * ``FunctionHandler`` — wraps a Python function.  Understands the legacy
    conventions (``f(args, **kw)`` where the first parameter is literally
    ``args``, or plain ``f(**schema_properties)``), sync or async, and
    optionally receives the ``ToolContext`` when the function declares a
    parameter named ``ctx``.
  * ``ProcessHandler`` — runs a CLI / any-language program through the one
    shared ``run_process`` (see ``process.py``).  Built from a manifest
    ``ExecSpec``; arguments are rendered into an argv list (never a shell).
"""

from __future__ import annotations

import asyncio
import contextvars
import functools
import inspect
import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence

from .process import Limits, ProcessSpec, build_env, safe_resolve
from .core import (
    BUSY,
    CRASHED,
    DENIED,
    INVALID_ARGS,
    TIMEOUT,
    TOOL_ERROR,
    UNAVAILABLE,
    ToolContext,
    ToolError,
    ToolResult,
)


# ── Python functions ─────────────────────────────────────────────

class FunctionHandler:
    def __init__(self, func: Callable[..., Any], *, blocking: bool = False) -> None:
        self.func = func
        self.blocking = blocking
        self.__wrapped__ = func
        self._introspected = False
        self.use_args_dict = False
        self.ctx_param: Optional[str] = None
        self.is_async = False

    def prepare(self) -> None:
        """Called by the executor *outside* its error-to-result handling:
        a failure here is a harness bug (not a tool failure) and must
        propagate to the agent's safety net."""
        if not self._introspected:
            self._introspect()

    def _introspect(self) -> None:
        """Signature inspection is lazy (first call) and cached."""
        sig = inspect.signature(self.func)
        params = list(sig.parameters)
        self.use_args_dict = len(params) >= 1 and params[0] == "args"
        self.ctx_param = None
        for name, p in sig.parameters.items():
            ann = p.annotation
            if name == "ctx" or ann is ToolContext or (isinstance(ann, str) and ann.endswith("ToolContext")):
                self.ctx_param = name
                break
        self.is_async = inspect.iscoroutinefunction(self.func)
        self._introspected = True

    async def __call__(self, args: Dict[str, Any], ctx: ToolContext) -> Any:
        if not self._introspected:
            self._introspect()
        extra: Dict[str, Any] = {self.ctx_param: ctx} if self.ctx_param else {}
        if self.use_args_dict:
            call = functools.partial(self.func, args=args, **extra)
        else:
            call = functools.partial(self.func, **args, **extra)
        if self.is_async:
            return await call()
        runtime = ctx.runtime
        if self.blocking or (runtime is not None and getattr(runtime, "sync_tools_in_thread", False)):
            loop = asyncio.get_running_loop()
            pool = runtime.thread_pool() if runtime is not None else None
            cvctx = contextvars.copy_context()
            return await loop.run_in_executor(pool, functools.partial(cvctx.run, call))
        result = call()
        if inspect.isawaitable(result):
            result = await result
        return result


# ── Subprocess / CLI ─────────────────────────────────────────────

_PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)([?*]?)\}")
RESERVED = ("workspace", "session_id", "user", "tenant", "call_id")


@dataclass
class ExecSpec:
    command: List[Any]  # str | {"when": param, "args": [...]}
    cwd: Optional[str] = None
    timeout: float = 60.0
    stdin: Optional[str] = None  # None | "json" | template string
    output: str = "text"  # text | json | lines
    env_allow: Optional[List[str]] = None  # None → defaults (PATH, HOME, ...)
    env_set: Dict[str, str] = field(default_factory=dict)
    secrets: List[str] = field(default_factory=list)  # env names resolved from runtime secrets/os.environ
    ok_exit_codes: List[int] = field(default_factory=lambda: [0])
    max_output_bytes: int = 1_000_000
    reject_leading_dash: bool = True
    sandbox: str = "auto"  # auto | none | required
    network: bool = False
    writable: List[str] = field(default_factory=list)  # extra writable paths (besides cwd)
    limits: Optional[Dict[str, int]] = None
    path_params: List[str] = field(default_factory=list)  # params confined to the workspace


def _stringify(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        return json.dumps(value)
    return str(value)


def render_argv(template: Sequence[Any], values: Dict[str, Any], *, reject_leading_dash: bool = True) -> List[str]:
    """Render a command template into an argv list.

    Placeholders: ``{name}`` (required), ``{name?}`` (element omitted when
    the value is missing/empty), ``{name*}`` (a list expands into several
    elements).  A dict element ``{"when": "flag", "args": [...]}`` is
    included only if that parameter is truthy.  Substitution happens per
    argv element — values are never re-parsed by a shell — and a value that
    would land in an element *by itself* may not start with ``-`` (option
    injection) unless ``reject_leading_dash`` is False.
    """
    out: List[str] = []
    for el in template:
        if isinstance(el, dict):
            if values.get(el.get("when")):
                out.extend(render_argv(el.get("args") or [], values, reject_leading_dash=reject_leading_dash))
            continue
        if not isinstance(el, str):
            raise ToolError(f"Bad command template element: {el!r}", error_type=TOOL_ERROR)
        whole = _PLACEHOLDER.fullmatch(el)
        if whole:
            name, mod = whole.group(1), whole.group(2)
            val = values.get(name)
            if mod == "*":
                items = val if isinstance(val, list) else ([] if val in (None, "") else [val])
                for it in items:
                    out.append(_checked(name, _stringify(it), reject_leading_dash))
                continue
            if val is None or val == "":
                if mod == "?":
                    continue
                raise ToolError(f"Missing value for '{name}'", error_type=INVALID_ARGS)
            out.append(_checked(name, _stringify(val), reject_leading_dash))
            continue

        def sub(m: "re.Match[str]") -> str:
            n = m.group(1)
            v = values.get(n)
            if v is None:
                raise ToolError(f"Missing value for '{n}'", error_type=INVALID_ARGS)
            s = _stringify(v)
            if "\x00" in s:
                raise ToolError(f"Invalid character in '{n}'", error_type=INVALID_ARGS)
            return s

        out.append(_PLACEHOLDER.sub(sub, el))
    return out


def _checked(name: str, s: str, reject_dash: bool) -> str:
    if "\x00" in s:
        raise ToolError(f"Invalid character in '{name}'", error_type=INVALID_ARGS)
    if reject_dash and s.startswith("-"):
        raise ToolError(
            f"Value for '{name}' may not start with '-' (looks like a command-line option).",
            error_type=INVALID_ARGS,
            hint="Prefix relative paths with './' or use an absolute path.",
        )
    return s


class ProcessHandler:
    """Runs an ExecSpec through the shared subprocess runner."""

    def __init__(self, spec: ExecSpec, tool_name: str = "") -> None:
        self.spec = spec
        self.tool_name = tool_name

    async def __call__(self, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        from .runtime import get_default_runtime

        runtime = ctx.runtime or get_default_runtime()
        spec = self.spec
        workspace = ctx.workspace or runtime.workspace

        values: Dict[str, Any] = dict(args)
        values.update(
            workspace=workspace or "",
            session_id=ctx.session_id or "",
            user=ctx.user or "",
            tenant=ctx.tenant or "",
            call_id=ctx.call_id or "",
        )

        for p in spec.path_params:
            if values.get(p) in (None, ""):
                continue
            if not workspace:
                raise ToolError("No workspace is configured, so file paths can't be used.", error_type=DENIED)
            vals = values[p] if isinstance(values[p], list) else [values[p]]
            resolved = []
            for v in vals:
                r = safe_resolve(str(v), workspace)
                if r is None:
                    raise ToolError(f"Path '{v}' is outside the allowed workspace.", error_type=DENIED)
                resolved.append(r)
            values[p] = resolved if isinstance(values[p], list) else resolved[0]

        argv = render_argv(spec.command, values, reject_leading_dash=spec.reject_leading_dash)
        cwd = None
        if spec.cwd:
            cwd = render_argv([spec.cwd], values, reject_leading_dash=False)[0]
        elif workspace:
            cwd = workspace
        if cwd and not os.path.isdir(cwd):
            raise ToolError(f"Working directory does not exist: {cwd}", error_type=UNAVAILABLE)

        stdin: Optional[bytes] = None
        if spec.stdin == "json":
            stdin = json.dumps(args).encode()
        elif spec.stdin:
            stdin = render_argv([spec.stdin], values, reject_leading_dash=False)[0].encode()

        env_extra = dict(spec.env_set)
        for name in spec.secrets:
            val = runtime.get_secret(name, ctx)
            if val is not None:
                env_extra[name] = val
        env = build_env(spec.env_allow, env_extra)

        timeout = spec.timeout
        remaining = ctx.remaining()
        if remaining is not None:
            timeout = min(timeout, max(0.1, remaining))

        from .sandbox import SandboxPolicy

        writable = list(spec.writable)
        if cwd and workspace and os.path.realpath(cwd).startswith(os.path.realpath(workspace)):
            writable.append(os.path.realpath(workspace))
        elif cwd:
            writable.append(cwd)
        pspec = ProcessSpec(
            argv=argv,
            cwd=cwd,
            env=env,
            stdin=stdin,
            timeout=timeout,
            max_output_bytes=spec.max_output_bytes,
            limits=Limits(**spec.limits) if spec.limits else runtime.process_limits,
            label=self.tool_name,
            sandbox_policy=SandboxPolicy(network=spec.network, writable=writable),
        )
        runner = runtime.runner_for(spec.sandbox)
        if runner is None:
            raise ToolError(
                "This tool requires a sandbox, but none is available on this host.",
                error_type=DENIED,
                hint="Install bubblewrap (bwrap) or configure a remote sandbox runner.",
            )

        # Bounded process concurrency (queue wait is NOT charged to the tool timeout).
        from ..concurrency import LimiterBusy

        try:
            async with runtime.process_limiter.slot(runtime.queue_timeout):
                res = await runner.run(pspec, on_output=ctx.emit, tracker=runtime.tracker)
        except LimiterBusy as exc:
            raise ToolError(str(exc), error_type=BUSY, retryable=True, hint="The host is busy running other commands; retry shortly.")

        return self._to_result(res, spec)

    @staticmethod
    def _to_result(res, spec: ExecSpec) -> ToolResult:
        if res.spawn_error:
            raise ToolError(res.spawn_error, error_type=UNAVAILABLE, hint="The command may not be installed on this host.")
        if res.timed_out:
            raise ToolError(f"Command timed out after {spec.timeout:.0f}s", error_type=TIMEOUT, retryable=True)
        if res.killed and (res.stdout_truncated or res.stderr_truncated):
            raise ToolError(
                f"Command produced more than {spec.max_output_bytes * 10} bytes of output and was stopped.",
                error_type=TOOL_ERROR,
                hint="Narrow the request (filters, smaller range, quieter flags).",
            )
        if res.returncode not in spec.ok_exit_codes:
            tail = res.text("stderr")[-2000:] or res.text("stdout")[-2000:]
            code = res.returncode
            if code is not None and code < 0:
                raise ToolError(f"Command was killed by signal {-code}. {tail}".strip(), error_type=CRASHED, retryable=True)
            raise ToolError(f"Command exited with status {code}. {tail}".strip(), error_type=TOOL_ERROR, exit_code=code)
        text = res.text("stdout")
        note = " [output truncated]" if res.stdout_truncated else ""
        if spec.output == "json":
            try:
                return ToolResult.success(json.loads(text))
            except ValueError as exc:
                raise ToolError(f"Command output was not valid JSON: {exc}", error_type=TOOL_ERROR, hint=text[:300])
        if spec.output == "lines":
            return ToolResult.success([ln for ln in text.splitlines()])
        return ToolResult.success(text + note)
