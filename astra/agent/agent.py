# astra/agent/agent.py
"""Agent — drives one conversation forward: build messages -> call the
provider -> execute any tool calls (in parallel) -> repeat, until the
model stops calling tools, a turn limit is hit, the user hits stop, or a
tool pauses the run for clarification.

This is the generalized replacement for the original Frappe-native Agent
loop. Behaviorally identical; only the plumbing (persistence, events,
stop-flags) now goes through the pluggable Conversation instead of
Frappe/Redis directly.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from ..context import ContextManager
from ..providers.openai_api import OpenAIProvider
from ..quota import Quota, QuotaExceeded
from ..telemetry import metrics, span
from ..tools.core import ToolContext
from ..tools.decorator import ToolRegistry
from ..tools.executor import ToolExecutor
from ..tools.runtime import ToolRuntime
from .conversation import (
    CLARIFICATION_PENDING_KEY,
    ChainBrokenError,
    ClarificationPending,
    Conversation,
    StoppedByUser,
    current_delegate_depth,
    current_session_id,
    current_tenant,
    current_user,
)

logger = logging.getLogger(__name__)

DEFAULT_MAX_TURNS = 40
MAX_DELEGATE_DEPTH = 3  # see tools/delegate_tools/delegate.py


@dataclass
class AgentResult:
    """What Agent.run() returns once the loop ends."""

    status: str  # "done" | "stopped" | "clarification_pending" | "max_turns" | "error"
    final_text: str = ""
    turns_used: int = 0
    clarification_id: str | None = None
    error: str | None = None
    session_id: str | None = None  # populated by astra.client.Astra — not set by Agent.run() itself


class RunTimeout(Exception):
    """The run's wall-clock budget (``run_timeout``) was exhausted."""


class Agent:
    def __init__(
        self,
        conversation: Conversation,
        provider: OpenAIProvider,
        registry: ToolRegistry,
        max_turns: int = DEFAULT_MAX_TURNS,
        max_context_turns: int | None = None,
        allow_image_attachments: bool = True,
        *,
        runtime: ToolRuntime | None = None,
        policy: Any = None,
        context_manager: ContextManager | None = None,
        quota: Quota | None = None,
        run_timeout: float | None = None,
        max_parallel_tool_calls: int = 8,
        stop_poll_interval: float = 0.5,
    ) -> None:
        """All keyword-only arguments are optional and additive.

        runtime: shared ToolRuntime (concurrency caps, sandbox runner, …).
            Pass ONE runtime to every Agent in the process (astra.Astra does).
        policy: ``(tool, args, ctx) -> True | str(deny reason)`` allow/deny hook.
        context_manager: replay-time trimming / summarization (astra.context).
        quota: per-user/tenant budget checked before every LLM request.
        run_timeout: wall-clock cap for the whole run (seconds).
        max_parallel_tool_calls: cap on tool calls executing at once *within
            one turn* (the runtime's caps then apply across all turns/users).
        stop_poll_interval: how often an in-flight LLM call / tool is checked
            for a user Stop request (only when the conversation has a
            SignalStore).
        """
        self.conversation = conversation
        self.provider = provider
        self.registry = registry
        self.runtime = runtime
        self.executor = ToolExecutor(registry, runtime=runtime, policy=policy)
        self.max_turns = max_turns
        self.max_context_turns = max_context_turns
        self.allow_image_attachments = allow_image_attachments
        self.context = context_manager
        self.quota = quota
        self.run_timeout = run_timeout
        self.max_parallel_tool_calls = max(1, max_parallel_tool_calls)
        self.stop_poll_interval = stop_poll_interval
        self._deadline: float | None = None

    async def run(self) -> AgentResult:
        """Advance the conversation until it settles. Assumes the
        triggering user message has already been added via
        conversation.add_user_message() (or this is a resume after
        clarification/recovery — either way, the next step is "call the
        model")."""
        conv = self.conversation
        session_token = current_session_id.set(conv.session_id)
        user_token = current_user.set(conv.session.user or "")
        tenant_token = current_tenant.set(conv.session.tenant or "")
        self._deadline = time.monotonic() + self.run_timeout if self.run_timeout else None
        t_run = time.perf_counter()
        metrics.gauge_add("astra_agent_runs_in_flight", 1)
        try:
            with span(
                "invoke_agent",
                **{
                    "gen_ai.operation.name": "invoke_agent",
                    "gen_ai.conversation.id": conv.session_id,
                    "gen_ai.agent.name": conv.session.agent_name,
                },
            ):
                try:
                    result = await self._run_loop(conv)
                except asyncio.CancelledError:
                    # Cancelled from outside (client disconnect, parent Stop,
                    # shutdown): leave a clean, resumable terminal state.
                    try:
                        conv.cancel_pending_tool_calls("Run cancelled.")
                        conv.save(ended_reason="cancelled")
                    except Exception:
                        logger.exception("could not checkpoint cancelled session %s", conv.session_id)
                    metrics.inc("astra_agent_runs_total", status="cancelled")
                    self.executor.runtime.run_ended(conv.session_id, "cancelled")
                    raise
            metrics.inc("astra_agent_runs_total", status=result.status)
            self.executor.runtime.run_ended(conv.session_id, result.status)
            metrics.observe("astra_agent_run_seconds", time.perf_counter() - t_run, status=result.status)
            return result
        finally:
            metrics.gauge_add("astra_agent_runs_in_flight", -1)
            current_tenant.reset(tenant_token)
            current_user.reset(user_token)
            current_session_id.reset(session_token)

    async def _run_loop(self, conv: Conversation) -> AgentResult:
        for turn in range(self.max_turns):
            try:
                result = await self._run_one_turn(conv, turn)
            except ChainBrokenError as exc:
                logger.warning("Chain broken in session %s: %s", conv.session_id, exc.reason)
                conv.save(ended_reason=f"chain_broken:{exc.reason}")
                await conv.emit_error(f"The model stopped unexpectedly ({exc.reason}).")
                return AgentResult(status="error", turns_used=turn, error=exc.reason, session_id=conv.session_id)
            except ClarificationPending as pending:
                conv.save(ended_reason="awaiting_clarification")
                await conv.callbacks.emit(
                    "on_clarification_request",
                    session_id=conv.session_id,
                    clarification_id=pending.clarification_id,
                    tool_call_id=pending.tool_call_id,
                    question=pending.extra.get("question"),
                    options=pending.extra.get("options"),
                    allow_free_text=pending.extra.get("allow_free_text"),
                )
                return AgentResult(
                    status="clarification_pending",
                    turns_used=turn + 1,
                    clarification_id=pending.clarification_id,
                    session_id=conv.session_id,
                )
            except StoppedByUser:
                conv.cancel_pending_tool_calls("Stopped by user.")
                conv.save(ended_reason="stopped_by_user")
                conv.clear_stop_flag()
                return AgentResult(status="stopped", turns_used=turn + 1, session_id=conv.session_id)
            except RunTimeout:
                conv.cancel_pending_tool_calls("Run time limit reached.")
                conv.save(ended_reason="run_timeout")
                await conv.emit_error("The run exceeded its time limit.")
                return AgentResult(status="error", turns_used=turn, error="run_timeout", session_id=conv.session_id)
            except Exception as exc:
                # Safety net: anything unexpected (a bug in a tool's
                # signature introspection, a Store hiccup, a provider
                # SDK edge case we didn't anticipate) must still
                # resolve to a clean AgentResult, not propagate raw
                # out of the harness. This matters most for entry
                # points with no caller-side try/except of their own
                # — a cron job or webhook handler shouldn't crash the
                # whole worker over one bad run.
                logger.exception("Unexpected error in Agent.run() for session %s", conv.session_id)
                try:
                    conv.save(ended_reason=f"unexpected_error:{type(exc).__name__}")
                    await conv.emit_error(f"Unexpected error: {exc}")
                except Exception:
                    logger.exception("Failed to checkpoint/emit after an unexpected error in session %s", conv.session_id)
                return AgentResult(status="error", turns_used=turn, error=str(exc), session_id=conv.session_id)

            if result is not None:
                return result

        conv.save(ended_reason="max_turns_reached")
        await conv.emit_error("Reached the maximum number of turns for this run.")
        return AgentResult(status="max_turns", turns_used=self.max_turns, session_id=conv.session_id)

    async def _run_one_turn(self, conv: Conversation, turn: int) -> "AgentResult | None":
        """One iteration of the loop body. Returns an AgentResult if the
        run should end here, or None to continue looping. Raises
        ChainBrokenError / ClarificationPending / StoppedByUser for
        run() to translate into the matching terminal AgentResult —
        kept as exceptions (not return values) since they can surface
        from deep inside _execute_tool_calls_parallel, not just here.
        """
        if conv.is_stop_requested():
            conv.cancel_pending_tool_calls("Stopped by user.")
            conv.save(ended_reason="stopped_by_user")
            conv.clear_stop_flag()
            return AgentResult(status="stopped", turns_used=turn, session_id=conv.session_id)

        if self._deadline is not None and time.monotonic() >= self._deadline:
            raise RunTimeout()

        if self.context is not None:
            await self.context.maybe_compact(conv, self.provider)

        messages = conv.get_messages(
            reasoning_replay=self.provider.replay_reasoning,
            max_turns=self.max_context_turns,
            allow_image_attachments=self.allow_image_attachments,
        )
        if self.context is not None:
            messages = self.context.prepare(messages, conv.last_prompt_tokens())
        tools = self.registry.get_tool_schemas(loaded=conv.session.loaded_tools) or None

        if self.quota is not None:
            try:
                self.quota.check(conv.session.user, conv.session.tenant)
            except QuotaExceeded as exc:
                await conv.emit_error(str(exc))
                conv.save(ended_reason="quota_exceeded")
                return AgentResult(status="error", turns_used=turn, error="quota_exceeded", session_id=conv.session_id)

        try:
            with span(
                f"chat {getattr(self.provider, 'model', '')}".strip(),
                **{
                    "gen_ai.operation.name": "chat",
                    "gen_ai.request.model": getattr(self.provider, "model", None),
                    "gen_ai.conversation.id": conv.session_id,
                },
            ):
                message_obj, tool_calls = await self._cancellable(
                    self.provider.generate(
                        messages,
                        tools=tools,
                        on_token=conv.emit_token,
                        on_reasoning=conv.emit_reasoning,
                    ),
                    enforce_deadline=True,
                )
        except (StoppedByUser, RunTimeout):
            raise
        except Exception as exc:
            logger.exception("Provider call failed in session %s", conv.session_id)
            await conv.emit_error(str(exc))
            conv.save(ended_reason="provider_error")
            return AgentResult(status="error", turns_used=turn, error=str(exc), session_id=conv.session_id)

        # ChainBrokenError propagates up to run() on purpose.
        await conv.add_assistant_message(message_obj, streamed=True)
        self._record_usage(conv)

        if not tool_calls:
            final_text = message_obj.get("content") or ""
            conv.save(ended_reason="completed")
            await conv.emit_done(final_text)
            return AgentResult(status="done", final_text=final_text, turns_used=turn + 1, session_id=conv.session_id)

        # ClarificationPending / StoppedByUser propagate up to run() on purpose.
        await self._execute_tool_calls_parallel(tool_calls)
        return None

    # ── cancellation-aware awaiting ───────────────────────────────

    async def _cancellable(self, awaitable: Any, *, enforce_deadline: bool = False) -> Any:
        """Await ``awaitable`` but abort it promptly when the user hits Stop
        (or the run deadline passes).  Previously Stop was only honoured
        *between* steps, so a slow LLM stream or a long-running tool kept
        running (and spending) until it finished on its own.  Cancelling the
        task propagates into the tool/provider — process tools kill their
        whole process group on cancellation.

        Polling only happens when the conversation has a SignalStore (no
        signals ⇒ no way to request a stop ⇒ zero overhead)."""
        conv = self.conversation
        needs_poll = conv.signals is not None
        needs_deadline = enforce_deadline and self._deadline is not None
        if not needs_poll and not needs_deadline:
            return await awaitable
        task = asyncio.ensure_future(awaitable)
        try:
            while True:
                timeout = self.stop_poll_interval if needs_poll else None
                if needs_deadline:
                    left = max(0.0, self._deadline - time.monotonic())  # type: ignore[operator]
                    timeout = left if timeout is None else min(timeout, left)
                done, _ = await asyncio.wait({task}, timeout=timeout)
                if done:
                    return task.result()
                if needs_poll and conv.is_stop_requested():
                    await self._cancel_task(task)
                    raise StoppedByUser()
                if needs_deadline and time.monotonic() >= self._deadline:  # type: ignore[operator]
                    await self._cancel_task(task)
                    raise RunTimeout()
        except asyncio.CancelledError:
            task.cancel()
            raise

    @staticmethod
    async def _cancel_task(task: "asyncio.Future[Any]") -> None:
        task.cancel()
        try:
            await task
        except BaseException:  # noqa: BLE001 - CancelledError / whatever the tool raised while dying
            pass

    def _record_usage(self, conv: Conversation) -> None:
        if self.quota is None:
            return
        for m in reversed(conv.session.messages):
            if m.role == "assistant":
                try:
                    self.quota.record(conv.session.user, conv.session.tenant, m.input_tokens, m.output_tokens, m.estimated_cost)
                except Exception:
                    logger.exception("quota.record failed")
                return

    def _tool_context(self, call_id: str, name: str) -> ToolContext:
        conv = self.conversation
        cb = conv.callbacks
        emit = None
        if cb.on_tool_output is not None or cb.on_event is not None:

            async def emit(stream: str, data: bytes) -> None:  # type: ignore[misc]
                await cb.emit(
                    "on_tool_output",
                    session_id=conv.session_id,
                    call_id=call_id,
                    tool_name=name,
                    stream=stream,
                    data=data.decode("utf-8", errors="replace"),
                )

        return ToolContext(
            tool_name=name,
            call_id=call_id,
            session_id=conv.session_id,
            user=conv.session.user,
            tenant=conv.session.tenant,
            depth=current_delegate_depth.get(),
            deadline=self._deadline,
            conversation=conv,
            registry=self.registry,
            stop_check=conv.is_stop_requested if conv.signals is not None else None,
            emit=emit,
        )

    async def _execute_tool_calls_parallel(self, tool_calls: list[Any]) -> None:
        conv = self.conversation
        sem = asyncio.Semaphore(self.max_parallel_tool_calls)

        async def _run_one(tc: Any) -> None:
            call_id = tc.id
            name = tc.function.name
            args = tc.function.arguments

            if conv.is_stop_requested():
                raise StoppedByUser()

            try:
                parsed_args: Any = json.loads(args)
            except Exception:
                parsed_args = args

            async with sem:
                if conv.is_stop_requested():
                    raise StoppedByUser()
                await conv.emit_tool_start(call_id, name, parsed_args)

                started = asyncio.get_event_loop().time()
                ctx = self._tool_context(call_id, name)
                result = await self._cancellable(self.executor._dispatch(name, args, ctx=ctx), enforce_deadline=True)
                elapsed_ms = int((asyncio.get_event_loop().time() - started) * 1000)

            was_loop_strike = False
            for row in conv.session.tool_calls:
                if row.call_id == call_id:
                    was_loop_strike = row.was_loop_strike
                    break

            # request_clarification's marker — pause this turn instead of
            # treating it as a normal tool result.
            try:
                parsed_result = json.loads(result)
            except Exception:
                parsed_result = None
            if isinstance(parsed_result, dict) and parsed_result.get(CLARIFICATION_PENDING_KEY):
                extra = {k: v for k, v in parsed_result.items() if k not in (CLARIFICATION_PENDING_KEY, "clarification_id")}
                conv.pause_for_clarification(call_id, parsed_result["clarification_id"], extra=extra)
                return  # unreachable — pause_for_clarification always raises

            await conv.add_tool_result(call_id, name, result, elapsed_ms=elapsed_ms, was_loop_strike=was_loop_strike)

        # Any tool call already flagged as a loop-strike by
        # add_assistant_message was persisted with status="error" and
        # never dispatched — skip it here, everything else runs in parallel.
        dispatchable = [tc for tc in tool_calls if self._not_already_resolved(tc.id)]

        # current_delegate_depth needs no explicit handling here:
        # asyncio.gather copies the current Context into each child Task
        # automatically, and any .set() a nested delegate_task performs
        # stays scoped to that child Task's own context — siblings and
        # the parent are unaffected. See tools/delegate_tools/delegate.py.
        results = await asyncio.gather(*(_run_one(tc) for tc in dispatchable), return_exceptions=True)

        for r in results:
            if isinstance(r, (ClarificationPending, StoppedByUser, RunTimeout)):
                raise r
        for r in results:
            if isinstance(r, Exception):
                # _dispatch already swallows tool exceptions into an
                # error string; anything raised here is a bug in the
                # harness itself — surface it rather than hiding it.
                raise r

    def _not_already_resolved(self, call_id: str) -> bool:
        for row in self.conversation.session.tool_calls:
            if row.call_id == call_id:
                return row.status == "pending"
        return True
