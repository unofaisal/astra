# astra/tools/executor.py
"""ToolExecutor — runs one tool call through the full pipeline.

    resolve → parse/validate args → policy → circuit breaker → admission
    (global/tenant/tool caps) → run with timeout → normalize → retry (only
    idempotent tools) → truncate/spill → metrics + tracing → wire string

Public API:
  * ``await execute(name, raw_args, ctx=...) -> ToolResult``  (new)
  * ``await _dispatch(name, raw_args) -> str``                (legacy: the
    wire string astra stores as the tool result; kept because existing
    callers and tests use it)

Retries are OPT-IN (``@tool(idempotent=True)`` / ``annotations.idempotent``).
The previous behaviour — retrying any failing tool up to 3 times — was
unsafe for side-effecting tools (a CLI that creates something would run
three times).
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import random
import time
from typing import Any, Callable, Dict, Optional, Union

from ..concurrency import LimiterBusy
from ..telemetry import metrics, span
from .core import (
    BREAKER_ERRORS,
    BUSY,
    CANCELLED,
    DENIED,
    INVALID_ARGS,
    NOT_FOUND,
    TIMEOUT,
    TOOL_ERROR,
    UNAVAILABLE,
    Tool,
    ToolAnnotations,
    ToolContext,
    ToolError,
    ToolResult,
)
from .decorator import ToolRegistry
from .handlers import FunctionHandler
from .runtime import ToolRuntime, get_default_runtime
from .validation import describe_parameters, validate_args

logger = logging.getLogger(__name__)

_BASE_BACKOFF = 0.5
_TIMEOUT_EXC = (asyncio.TimeoutError, TimeoutError)


def truncate_text(text: str, limit: int, extra: str = "") -> str:
    """Head+tail truncation with a steering note the model can act on."""
    head = int(limit * 0.7)
    tail = max(0, limit - head)
    omitted = len(text) - head - tail
    note = (
        f"\n…[truncated {omitted} of {len(text)} characters. Narrow the request "
        f"(filters, pagination, a smaller range) to see the rest.{extra}]…\n"
    )
    return text[:head] + note + (text[-tail:] if tail else "")


def _is_special_payload(wire: str) -> bool:
    """Payloads the agent loop parses structurally must never be cut."""
    if not wire.startswith("{"):
        return False
    try:
        obj = json.loads(wire)
    except ValueError:
        return False
    if not isinstance(obj, dict):
        return False
    from ..agent.conversation import CLARIFICATION_PENDING_KEY

    return bool(obj.get(CLARIFICATION_PENDING_KEY)) or "attachments" in obj


class ToolExecutor:
    def __init__(
        self,
        registry: ToolRegistry,
        runtime: Optional[ToolRuntime] = None,
        policy: Optional[Callable[..., Any]] = None,
    ) -> None:
        self.registry = registry
        self.runtime = runtime or get_default_runtime()
        self.policy = policy or self.runtime.policy

    # ── legacy entry point ───────────────────────────────────────
    async def _dispatch(self, func_name: str, raw_args: Any, ctx: Optional[ToolContext] = None) -> str:
        return (await self.execute(func_name, raw_args, ctx=ctx)).to_wire()

    # ── public entry point ───────────────────────────────────────
    async def execute(self, name: str, raw_args: Any, *, ctx: Optional[ToolContext] = None) -> ToolResult:
        ctx = ctx or ToolContext()
        ctx.tool_name = name
        ctx.runtime = self.runtime
        if ctx.registry is None:
            ctx.registry = self.registry
        if not ctx.services:
            ctx.services = self.runtime.services
        t0 = time.perf_counter()
        with span(
            f"execute_tool {name}",
            **{
                "gen_ai.operation.name": "execute_tool",
                "gen_ai.tool.name": name,
                "gen_ai.tool.type": "function",
                "gen_ai.tool.call.id": ctx.call_id,
                "gen_ai.conversation.id": ctx.session_id,
            },
        ) as sp:
            result = await self._run(name, raw_args, ctx)
            dt = time.perf_counter() - t0
            sp.set_attribute("astra.tool.ok", result.ok)
            if not result.ok:
                sp.set_attribute("error.type", result.error_type or TOOL_ERROR)
            metrics.observe("astra_tool_duration_seconds", dt, tool=name)
            metrics.inc("astra_tool_calls_total", tool=name, status="ok" if result.ok else (result.error_type or TOOL_ERROR))
        return result

    # ── pipeline ─────────────────────────────────────────────────
    async def _run(self, name: str, raw_args: Any, ctx: ToolContext) -> ToolResult:
        # 1. parse
        if isinstance(raw_args, dict):
            args = raw_args
        else:
            try:
                args = json.loads(raw_args) if (raw_args or "").strip() else {}
            except (json.JSONDecodeError, TypeError) as e:
                return ToolResult.error(f"Could not parse arguments for '{name}': {e}", INVALID_ARGS)
        if not isinstance(args, dict):
            return ToolResult.error(f"Arguments for '{name}' must be a JSON object.", INVALID_ARGS)

        # 2. resolve
        tool = self._resolve(name)
        if tool is None:
            available = ", ".join(self.registry.get_tool_names()) or "none"
            return ToolResult.error(f"Tool '{name}' not found. Available tools: {available}", NOT_FOUND)
        if not tool.available():
            return ToolResult.error(f"Tool '{name}' is currently unavailable.", UNAVAILABLE)

        prepare = getattr(tool.handler, "prepare", None)
        if prepare is not None:
            prepare()  # harness-level failures propagate (see FunctionHandler.prepare)

        # 3. validate
        if self.runtime.validate_args and tool.validate:
            problems = validate_args(tool.parameters, args)
            if problems:
                return ToolResult.error(
                    f"Invalid arguments for '{name}': " + "; ".join(problems),
                    INVALID_ARGS,
                    hint=f"Expected: {describe_parameters(tool.parameters)}",
                )

        # 4. policy (allow/deny hook)
        if self.policy is not None:
            try:
                verdict = self.policy(tool, args, ctx)
                if inspect.isawaitable(verdict):
                    verdict = await verdict
            except Exception as exc:  # fail closed
                logger.exception("tool policy raised")
                return ToolResult.error(f"Policy check failed for '{name}': {exc}", DENIED)
            if verdict is not None and verdict is not True:
                msg = verdict if isinstance(verdict, str) else f"Tool '{name}' is not permitted here."
                return ToolResult.error(msg, DENIED)

        # 5. circuit breaker
        breaker = self.runtime.breaker_for(tool)
        if breaker is not None and not breaker.allow():
            return ToolResult.error(
                f"Tool '{name}' is temporarily disabled after repeated failures.",
                UNAVAILABLE,
                retryable=True,
                hint=f"Try again in about {int(breaker.retry_after()) + 1}s, or use a different approach.",
            )

        # 6. admission + run (+ retries for idempotent tools)
        try:
            async with self.runtime.admit(tool, ctx):
                result = await self._attempts(tool, args, ctx)
        except LimiterBusy as exc:
            metrics.inc("astra_tool_rejected_total", tool=name)
            return ToolResult.error(
                f"Too many tool calls are running right now ({exc}).",
                BUSY,
                retryable=True,
                hint="Wait a moment and retry, or reduce parallel calls.",
            )

        if breaker is not None:
            if result.ok or result.error_type not in BREAKER_ERRORS:
                breaker.record_success() if result.ok else None
            else:
                breaker.record_failure()

        return self._finalize(tool, result, ctx)

    def _resolve(self, name: str) -> Optional[Tool]:
        t = self.registry.tools.get(name)
        if t is not None:
            return t
        fn = self.registry.executors.get(name)  # callable injected without register_tool()
        if fn is not None:
            schema = self.registry._tools.get(name, {}).get("function", {})
            return Tool(
                name=name,
                description=schema.get("description", ""),
                parameters=schema.get("parameters") or {"type": "object", "properties": {}},
                handler=fn if isinstance(fn, FunctionHandler) else FunctionHandler(fn),
            )
        return None

    async def _attempts(self, tool: Tool, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        max_attempts = 1 + (self.runtime.max_retries if tool.annotations.idempotent else 0)
        result = ToolResult.error("no attempt made", TOOL_ERROR)
        for attempt in range(max_attempts):
            result = await self._invoke_once(tool, args, ctx)
            if result.ok or not (result.retryable and tool.annotations.idempotent) or attempt == max_attempts - 1:
                return result
            if ctx.remaining() is not None and ctx.remaining() < 1.0:
                return result
            delay = _BASE_BACKOFF * (2**attempt) + random.uniform(0, 0.3)
            metrics.inc("astra_tool_retries_total", tool=tool.name)
            logger.info("retrying tool %s (attempt %d/%d) in %.1fs: %s", tool.name, attempt + 2, max_attempts, delay, result.message)
            await asyncio.sleep(delay)
        return result

    async def _invoke_once(self, tool: Tool, args: Dict[str, Any], ctx: ToolContext) -> ToolResult:
        timeout = tool.timeout if tool.timeout is not None else self.runtime.default_timeout
        remaining = ctx.remaining()
        if remaining is not None:
            timeout = min(timeout, remaining) if timeout else remaining
        try:
            raw = await asyncio.wait_for(tool.handler(args, ctx), timeout) if timeout else await tool.handler(args, ctx)
            return self._normalize(raw)
        except asyncio.CancelledError:
            raise
        except _TIMEOUT_EXC:
            return ToolResult.error(f"Tool '{tool.name}' timed out after {timeout:g}s", TIMEOUT, retryable=True)
        except ToolError as exc:
            return ToolResult.error(exc.message, exc.error_type, hint=exc.hint, retryable=exc.retryable, **exc.extra)
        except (TypeError, ValueError, KeyError) as exc:
            logger.exception("Tool '%s' failed", tool.name)
            hint = None
            if isinstance(exc, TypeError):
                hint = f"Expected: {describe_parameters(tool.parameters)}"
            return ToolResult.error(f"Execution error in '{tool.name}': {exc}", TOOL_ERROR, hint=hint)
        except Exception as exc:
            logger.exception("Tool '%s' failed", tool.name)
            return ToolResult.error(f"Execution error in '{tool.name}': {exc}", TOOL_ERROR, retryable=True)

    @staticmethod
    def _normalize(raw: Any) -> ToolResult:
        if isinstance(raw, ToolResult):
            return raw
        if isinstance(raw, dict) and raw.get("error"):
            # Legacy convention: a dict with an "error" key is a failure.
            res = ToolResult(ok=False, content=raw, error_type=str(raw.get("error_type") or TOOL_ERROR), message=str(raw.get("error")))
            res.wire = json.dumps(raw, default=str)
            return res
        return ToolResult.success(raw)

    def _finalize(self, tool: Tool, result: ToolResult, ctx: ToolContext) -> ToolResult:
        wire = result.to_wire()
        limit = tool.max_output_chars or self.runtime.max_output_chars
        if limit and len(wire) > limit and not _is_special_payload(wire):
            path = self.runtime.spill(wire, tool.name, ctx.call_id)
            extra = f" Full output saved to: {path}" if path else ""
            result.wire = truncate_text(wire, limit, extra)
            result.truncated = True
            metrics.inc("astra_tool_output_truncated_total", tool=tool.name)
        else:
            result.wire = wire
        metrics.observe("astra_tool_output_chars", len(result.wire), tool=tool.name)
        return result
