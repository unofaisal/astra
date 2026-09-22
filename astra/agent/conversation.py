# astra/agent/conversation.py
"""Conversation — the persistence + event spine of a single agent session.

This is the generalized replacement for the original Frappe-native
Conversation class. What changed:

  - Frappe doc + child tables            -> astra.storage.Session (plain
                                             dataclass) persisted through
                                             a pluggable Store.
  - Redis stop-flag / clarify-resume ctx -> astra.signals.SignalStore
                                             (in-memory by default).
  - frappe.publish_realtime              -> astra.events.Callbacks
                                             (plain callables you supply).
  - Model Pricing doctype lookup         -> optional `pricing_lookup`
                                             callable you supply.
  - Attachment inlining                  -> optional `attachments_builder`
                                             callable you supply.

Everything else — history reconstruction for the LLM, tool-call
pending/running/success/error/cancelled lifecycle, loop detection,
reasoning buffering/replay, clarification pause/resume, crash recovery —
is the same behavior as the original, just decoupled from Frappe.
"""

from __future__ import annotations

import contextvars
import hashlib
import json
import logging
import time
from typing import Any, Callable, Optional

from ..events import Callbacks
from ..signals import (
    SignalStore,
    clear_stop_flag,
    is_stop_requested,
    request_stop,
)
from ..storage import MemoryStore, MessageRow, Session, Store, ToolCallRow, gen_id

logger = logging.getLogger(__name__)

# Task-scoped (not thread-scoped) session id, readable by any tool running
# within the current async call stack — including tools launched via
# asyncio.gather, which copy the context at task-creation time.
current_session_id: contextvars.ContextVar[str] = contextvars.ContextVar("current_session_id", default="")

# Same propagation mechanism, for delegate_task's depth limiting.
current_delegate_depth: contextvars.ContextVar[int] = contextvars.ContextVar("current_delegate_depth", default=0)


class StoppedByUser(Exception):
    """Raised inside Agent.run()'s loop when a user-initiated stop is
    detected via Conversation.is_stop_requested()."""


class ClarificationPending(Exception):
    """Raised inside Agent.run()'s loop when request_clarification has
    paused the turn to ask the user a question.

    An orderly pause, not an interruption to clean up after: exactly one
    tool call is left in "awaiting_clarification" status. The turn
    resumes via Conversation.resolve_clarification(), not via a new user
    message.
    """

    def __init__(self, clarification_id: str, tool_call_id: str, extra: dict[str, Any] | None = None):
        self.clarification_id = clarification_id
        self.tool_call_id = tool_call_id
        self.extra = extra or {}
        super().__init__(f"Awaiting clarification {clarification_id}")


# Sentinel key a tool's result carries to signal "pause this turn" — read
# by Agent._execute_tool_calls_parallel, written by request_clarification.
CLARIFICATION_PENDING_KEY = "__chat_clarification_pending__"


class ChainBrokenError(Exception):
    """Raised when the model stops unexpectedly mid-chain. partial_message
    contains whatever the model produced before the break."""

    def __init__(self, reason: str, partial_message: dict[str, Any]):
        self.reason = reason
        self.partial_message = partial_message
        super().__init__(f"Chain broken: {reason}")


PricingLookup = Optional[Callable[[str], Optional[dict]]]
AttachmentsBuilder = Optional[Callable[[str, list[dict], bool], Any]]


def _compute_cost(pricing_lookup: PricingLookup, model: str, input_tokens: int, output_tokens: int, cached_tokens: int = 0) -> float | None:
    if not pricing_lookup or not model:
        return None
    pricing = pricing_lookup(model)
    if not pricing:
        return None
    billable_input = max((input_tokens or 0) - (cached_tokens or 0), 0)
    cost = 0.0
    cost += billable_input * (pricing.get("input_price_per_million") or 0) / 1_000_000
    cost += (output_tokens or 0) * (pricing.get("output_price_per_million") or 0) / 1_000_000
    cost += (cached_tokens or 0) * (pricing.get("cached_price_per_million") or 0) / 1_000_000
    return round(cost, 6)


def _safe_tool_arguments(raw: str | None) -> str:
    """Guarantee the string replayed as tool_calls[].function.arguments is
    valid JSON — a malformed arguments string persisted verbatim would get
    replayed into every future request and some providers 400 the whole
    request the moment any historical tool call has unparseable arguments."""
    raw = raw or "{}"
    try:
        json.loads(raw)
        return raw
    except (TypeError, ValueError):
        return "{}"


def _is_error_result(content: Any) -> bool:
    """A tool result is an error if EITHER convention is used:
      1. A plain string starting with "Error:" (request_clarification's
         hand-written validation messages use this).
      2. A JSON object with an "error" key — the convention used almost
         everywhere else: ToolExecutor._dispatch's exception/not-found/
         bad-args paths, skill_tools, delegate_tools. This is the
         canonical convention going forward since it also carries
         structured detail (error_type, etc.) that a plain string can't.
    Recognizing only #1 was a real bug: it meant nearly every tool-level
    failure got silently marked status="success" with the error JSON
    stored as if it were a successful result.
    """
    if not isinstance(content, str):
        return False
    if content.startswith("Error:"):
        return True
    try:
        parsed = json.loads(content)
    except (TypeError, ValueError):
        return False
    return isinstance(parsed, dict) and "error" in parsed


class Conversation:
    def __init__(
        self,
        store: Store | None = None,
        session_id: str | None = None,
        user: str = "anonymous",
        signals: SignalStore | None = None,
        callbacks: Callbacks | None = None,
        pricing_lookup: PricingLookup = None,
        attachments_builder: AttachmentsBuilder = None,
    ):
        self.store = store or MemoryStore()
        self.signals = signals
        self.callbacks = callbacks or Callbacks()
        self.pricing_lookup = pricing_lookup
        self.attachments_builder = attachments_builder

        existing = self.store.get(session_id) if session_id else None
        self.session: Session = existing or Session(session_id=session_id or gen_id("sess_"), user=user)

        self.session_id = self.session.session_id
        self.system_prompt: str | None = None
        self._reasoning_buffer = ""
        self._last_message_id: str | None = None
        self._turn_index = self.session.turn_count

        # Loop detection — process-local, not persisted (matches original).
        self._recent_tool_calls: list[str] = []
        self._max_repeated_calls: int = 3

    # ── Loop detection ───────────────────────────────────

    def _detect_tool_loop(self, tool_name: str, arguments: str) -> bool:
        """True if the same (tool_name, arguments) pair has been seen
        _max_repeated_calls times consecutively at the tail of history."""
        call_signature = f"{tool_name}:{arguments}"
        self._recent_tool_calls.append(call_signature)

        window = self._max_repeated_calls * 2
        if len(self._recent_tool_calls) > window:
            self._recent_tool_calls = self._recent_tool_calls[-window:]

        consecutive = 0
        for call in reversed(self._recent_tool_calls):
            if call == call_signature:
                consecutive += 1
            else:
                break
        return consecutive >= self._max_repeated_calls

    # ── History reconstruction ───────────────────────────

    def get_messages(
        self,
        reasoning_replay=None,
        max_turns: int | None = None,
        allow_image_attachments: bool = True,
    ):
        """Rebuild the OpenAI-style message list from stored rows.

        reasoning_replay: optional callable(meta, had_tool_calls, msg) —
            see OpenAIProvider.replay_reasoning. When omitted, buffered
            reasoning is merged into the assistant content as a
            </thinking> block instead (fine for simple deployments; not
            correct for providers with strict replay requirements).
        max_turns: keep only the last N assistant turns (plus the user
            message that triggered the first kept one).
        allow_image_attachments: whether to inline image attachments as
            base64 content parts (only if an attachments_builder was
            supplied) — pass False for text-only models.
        """
        messages = []
        if self.system_prompt:
            messages.append({"role": "system", "content": self.system_prompt})

        tool_calls_by_parent: dict[str, list[ToolCallRow]] = {}
        for tc in self.session.tool_calls:
            tool_calls_by_parent.setdefault(tc.parent_message, []).append(tc)

        pending_reasoning: list[str] = []
        all_rows = list(self.session.messages)

        if max_turns is not None:
            assistant_indices = [i for i, r in enumerate(all_rows) if r.role == "assistant"]
            if len(assistant_indices) > max_turns:
                # Keep everything from just after the last DROPPED
                # assistant turn onward, so history starts cleanly at a
                # user/assistant boundary rather than mid-tool-exchange.
                prev_assistant_idx = assistant_indices[-max_turns - 1]
                all_rows = all_rows[prev_assistant_idx + 1 :]

        for row in all_rows:
            if row.role == "reasoning":
                if reasoning_replay is None and row.content:
                    pending_reasoning.append(row.content.strip())
                continue

            if row.role == "assistant":
                content = row.content or ""
                tc_rows = tool_calls_by_parent.get(row.message_id, [])
                had_tool_calls = bool(tc_rows)

                if reasoning_replay is None and pending_reasoning:
                    reasoning_text = "\n\n".join(pending_reasoning)
                    if content:
                        content = f"<thinking>\n{reasoning_text}\n</thinking>\n\n{content}"
                    else:
                        content = f"<thinking>\n{reasoning_text}\n</thinking>"
                    pending_reasoning = []

                msg: dict[str, Any] = {"role": "assistant", "content": content}

                if tc_rows:
                    msg["tool_calls"] = [
                        {
                            "id": tc.call_id,
                            "type": "function",
                            "function": {"name": tc.tool_name, "arguments": _safe_tool_arguments(tc.arguments)},
                        }
                        for tc in tc_rows
                    ]
                messages.append(msg)

                if reasoning_replay is not None:
                    reasoning_meta = row.reasoning_meta
                    reasoning_replay(reasoning_meta, had_tool_calls, msg)

                # Every tool_call_id in an assistant message must be
                # immediately followed by a matching tool-role reply, or
                # strict providers (OpenAI) 400 the request. If a call is
                # stuck pending/running (crash, interrupted turn), emit a
                # synthetic reply so history stays valid.
                for tc in tc_rows:
                    if tc.status in ("success", "error"):
                        result_content = (tc.error if tc.status == "error" else tc.result) or ""
                    elif tc.status == "cancelled":
                        result_content = tc.error or "Cancelled by user before execution."
                    else:
                        result_content = (
                            "Error: tool execution was interrupted and no result was recorded. "
                            "Please retry the operation if needed."
                        )
                    messages.append(
                        {"role": "tool", "tool_call_id": tc.call_id, "name": tc.tool_name, "content": result_content}
                    )
            elif row.role == "user":
                pending_reasoning = []
                content: Any = row.content or ""
                if row.attachments and self.attachments_builder is not None:
                    try:
                        content = self.attachments_builder(row.content or "", row.attachments, allow_image_attachments)
                    except Exception:
                        logger.exception(
                            "attachments_builder failed for message %s in session %s — sending as plain text.",
                            row.message_id, self.session_id,
                        )
                        content = row.content or ""
                messages.append({"role": "user", "content": content})
            else:
                pending_reasoning = []
                messages.append({"role": row.role, "content": row.content or ""})

        return messages

    # ── Mutators ─────────────────────────────────────────

    def set_system(self, text: str) -> None:
        self.system_prompt = text

    def _checkpoint(self) -> None:
        """Persist whatever's accumulated in memory right now. Called
        after each completed unit of work (an assistant turn, a finished
        tool call) rather than relying solely on one big save() at the
        end of the run — this is what makes a stop/kill safe: everything
        up to the last completed step is already durable."""
        self.session.last_active = time.time()
        self.session.message_count = len(self.session.messages)
        self.session.tool_call_count = len(self.session.tool_calls)
        self.store.save(self.session)

    def add_system_message(self, text: str, skill_name: str | None = None) -> None:
        """Insert a system message inline (not the top-level system_prompt
        — see set_system for that). Used for per-turn skill injections."""
        msg_id = gen_id()
        row = MessageRow(message_id=msg_id, role="system", content=text, timestamp=time.time())
        if skill_name:
            row.skill_name = skill_name
            if not self.session.skill_invoked:
                self.session.skill_invoked = skill_name
            self.session.skill_content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
        self.session.messages.append(row)
        self._last_message_id = msg_id

    async def add_user_message(self, text: str, attachments: list[dict[str, Any]] | None = None) -> None:
        """attachments: [{"file_name", "file_url"/"file_path", "mime_type"}, ...]."""
        if not self.session.title:
            self.session.title = (text or "")[:72]
        msg_id = gen_id()
        self.session.messages.append(
            MessageRow(message_id=msg_id, role="user", content=text, attachments=attachments, timestamp=time.time())
        )
        self._last_message_id = msg_id
        self._checkpoint()
        await self.callbacks.emit("on_message", role="user", content=text, session_id=self.session_id)

    async def add_assistant_message(self, message_obj: dict[str, Any], streamed: bool = False, latency_ms: int | None = None) -> None:
        """Persist an assistant turn (content, reasoning, tool calls, usage).

        Raises ChainBrokenError AFTER persisting the partial message if the
        provider signaled an unexpected termination.
        """
        buffer_had_reasoning = bool(self._reasoning_buffer)
        self._flush_reasoning()

        content = message_obj.get("content") or ""
        tool_calls = message_obj.get("tool_calls") or []
        reasoning_meta = message_obj.get("reasoning_meta")
        reasoning_text = message_obj.get("reasoning")
        chain_break = message_obj.get("chain_break")
        usage = message_obj.get("usage") or {}
        model = message_obj.get("model")
        input_tokens = usage.get("prompt_tokens") or usage.get("input_tokens") or 0
        output_tokens = usage.get("completion_tokens") or usage.get("output_tokens") or 0
        cached_tokens = (usage.get("prompt_tokens_details") or {}).get("cached_tokens")
        reasoning_tokens = (usage.get("completion_tokens_details") or {}).get("reasoning_tokens")
        turn_cost = _compute_cost(self.pricing_lookup, model, input_tokens, output_tokens, cached_tokens or 0)

        self._turn_index += 1

        # 1. Non-streamed: buffer never populated -> persist from message_obj["reasoning"].
        # 2. Streamed via emit_reasoning: buffer already flushed above -> skip (avoid dup).
        # 3. Streamed via a caller's own on_reasoning callback that bypassed emit_reasoning:
        #    buffer never populated -> persist from message_obj["reasoning"].
        if reasoning_text and not buffer_had_reasoning:
            self.session.messages.append(
                MessageRow(message_id=gen_id(), role="reasoning", content=reasoning_text, timestamp=time.time())
            )

        msg_id = gen_id()
        self.session.messages.append(
            MessageRow(
                message_id=msg_id,
                role="assistant",
                content=content,
                is_error=bool(message_obj.get("is_error")),
                reasoning_meta=reasoning_meta,
                chain_break=chain_break,
                turn_index=self._turn_index,
                model=model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cached_tokens=cached_tokens,
                reasoning_tokens=reasoning_tokens,
                latency_ms=latency_ms,
                estimated_cost=turn_cost,
                timestamp=time.time(),
            )
        )
        self._last_message_id = msg_id

        if model and not self.session.model:
            self.session.model = model
        self.session.total_input_tokens += input_tokens
        self.session.total_output_tokens += output_tokens
        self.session.turn_count = self._turn_index
        if turn_cost is not None:
            self.session.estimated_cost += turn_cost

        for tc in tool_calls:
            fn = tc.get("function", {})
            args_str = fn.get("arguments") or "{}"
            stored_args_str = _safe_tool_arguments(args_str)

            if self._detect_tool_loop(fn.get("name", ""), args_str):
                logger.warning(
                    "Tool loop detected: %s called %d times consecutively with same args in session %s",
                    fn.get("name", ""), self._max_repeated_calls, self.session_id,
                )
                self.session.tool_calls.append(
                    ToolCallRow(
                        call_id=tc.get("id") or gen_id(),
                        parent_message=msg_id,
                        tool_name=fn.get("name", ""),
                        arguments=stored_args_str,
                        status="error",
                        error=(
                            "Error: Tool loop detected — the same tool was called repeatedly with "
                            "identical arguments. Please try a different approach or provide your "
                            "final answer with the information already available."
                        ),
                        started_at=time.time(),
                        completed_at=time.time(),
                        elapsed_ms=0,
                        was_loop_strike=True,
                    )
                )
                continue

            self.session.tool_calls.append(
                ToolCallRow(
                    call_id=tc.get("id") or gen_id(),
                    parent_message=msg_id,
                    tool_name=fn.get("name", ""),
                    arguments=stored_args_str,
                    status="pending",
                    started_at=time.time(),
                )
            )

        if content and not streamed:
            await self.callbacks.emit("on_message", role="assistant", content=content, session_id=self.session_id)

        self._checkpoint()

        if chain_break:
            raise ChainBrokenError(chain_break.get("reason", "Unknown"), message_obj)

    # ── Streaming hooks ──────────────────────────────────

    async def emit_token(self, delta: str) -> None:
        if not delta:
            return
        await self.callbacks.emit("on_token", delta=delta, session_id=self.session_id)

    async def emit_reasoning(self, delta: str) -> None:
        if not delta:
            return
        self._reasoning_buffer += delta
        await self.callbacks.emit("on_reasoning", delta=delta, session_id=self.session_id)

    # ── Tool lifecycle ───────────────────────────────────

    async def emit_tool_start(self, tool_call_id: str, tool_name: str, args: Any) -> None:
        self._flush_reasoning()
        for tc in self.session.tool_calls:
            if tc.call_id == tool_call_id and tc.status == "pending":
                tc.status = "running"
                tc.started_at = time.time()
                break
        await self.callbacks.emit(
            "on_tool_start", session_id=self.session_id, call_id=tool_call_id, tool_name=tool_name, args=args
        )

    async def add_tool_result(self, tool_call_id: str, name: str, content: Any, elapsed_ms: int | None = None, was_loop_strike: bool = False) -> None:
        is_error = _is_error_result(content)
        now = time.time()

        for tc in self.session.tool_calls:
            if tc.call_id != tool_call_id:
                continue
            tc.status = "error" if is_error else "success"
            tc.completed_at = now
            tc.was_loop_strike = was_loop_strike
            if is_error:
                tc.error = content
            else:
                tc.result = content
            if elapsed_ms is None and tc.started_at:
                elapsed_ms = int((now - tc.started_at) * 1000)
            tc.elapsed_ms = elapsed_ms
            break

        await self.callbacks.emit(
            "on_tool_result",
            session_id=self.session_id,
            call_id=tool_call_id,
            tool_name=name,
            result=None if is_error else content,
            error=content if is_error else None,
            elapsed_ms=elapsed_ms,
            is_error=is_error,
        )

        # If a tool's JSON result carries an "attachments" list, inject a
        # synthetic user turn so those files are visible to the model on
        # the next turn (requires an attachments_builder to actually do
        # anything with them in get_messages()).
        if not is_error and isinstance(content, str):
            try:
                parsed = json.loads(content)
                if isinstance(parsed, dict) and parsed.get("attachments"):
                    attachments = parsed.get("attachments", [])
                    if isinstance(attachments, list) and attachments:
                        inject_text = parsed.get("inject_message", f"The tool '{name}' returned the following file(s) for your review:")
                        await self.add_user_message(text=inject_text, attachments=attachments)
            except Exception:
                pass

        self._checkpoint()

    # ── Terminal events ──────────────────────────────────

    async def emit_done(self, response: str) -> None:
        await self.callbacks.emit("on_done", session_id=self.session_id, response=response)

    async def emit_error(self, error_text: str) -> None:
        await self.callbacks.emit("on_error", session_id=self.session_id, error_text=error_text)

    # ── Feedback & eval-adjacent fields ──────────────────

    def set_user_feedback(self, feedback: str, note: str | None = None) -> None:
        if feedback not in ("up", "down", "none"):
            raise ValueError(f"Invalid feedback value: {feedback}")
        self.session.user_feedback = feedback
        if note:
            self.session.user_feedback_note = note
        self.store.save(self.session)

    def set_outcome(self, outcome: str) -> None:
        valid = {"Success", "Partial", "Failure", "Unclear"}
        if outcome not in valid:
            raise ValueError(f"Invalid outcome: {outcome}. Must be one of {valid}")
        self.session.outcome = outcome
        self.store.save(self.session)

    # ── Clarification control ────────────────────────────

    def pause_for_clarification(self, tool_call_id: str, clarification_id: str, extra: dict[str, Any] | None = None) -> None:
        """Called from Agent._execute_tool_calls_parallel when a tool
        result carries request_clarification's CLARIFICATION_PENDING_KEY
        marker. Leaves the tool_call row "awaiting_clarification"."""
        self._flush_reasoning()
        for tc in self.session.tool_calls:
            if tc.call_id == tool_call_id:
                tc.status = "awaiting_clarification"
                tc.clarification_id = clarification_id
                tc.completed_at = None
                break
        self._checkpoint()
        raise ClarificationPending(clarification_id, tool_call_id, extra=extra)

    @staticmethod
    async def resolve_clarification(store: Store, session_id: str, clarification_id: str, answer: str) -> dict:
        """Finalize a paused request_clarification tool call once the user
        has answered. Returns {"found": True/False}."""
        answer = (answer or "").strip()
        if not answer:
            raise ValueError("A non-empty answer is required")

        conversation = Conversation(store=store, session_id=session_id)
        target = None
        for tc in conversation.session.tool_calls:
            if tc.clarification_id == clarification_id:
                target = tc
                break

        if target is None or target.status != "awaiting_clarification":
            return {"found": False}

        await conversation.add_tool_result(target.call_id, target.tool_name, f"User answered: {answer}")
        return {"found": True}

    def cancel_pending_tool_calls(self, reason: str = "Cancelled by user before execution.") -> int:
        """Give any still-"pending"/"running" tool_calls a terminal status
        instead of leaving them stuck forever (e.g. a stop landing between
        add_assistant_message persisting them and dispatch)."""
        count = 0
        now = time.time()
        for tc in self.session.tool_calls:
            if tc.status in ("pending", "running"):
                tc.status = "cancelled"
                tc.error = reason
                tc.completed_at = now
                if tc.started_at:
                    tc.elapsed_ms = int((now - tc.started_at) * 1000)
                count += 1
        if count:
            self._checkpoint()
        return count

    # ── Persistence ──────────────────────────────────────

    def save(self, ended_reason: str | None = None) -> None:
        self._flush_reasoning()
        if ended_reason:
            self.session.ended_reason = ended_reason
        self._checkpoint()

    # ── Recovery helpers ─────────────────────────────────

    def get_last_incomplete_state(self) -> dict[str, Any]:
        pending_calls: list[str] = []
        last_assistant_id: str | None = None
        last_assistant_had_tools = False

        for row in reversed(self.session.messages):
            if row.role == "assistant":
                last_assistant_id = row.message_id
                last_assistant_had_tools = any(
                    tc.parent_message == row.message_id and tc.status in ("pending", "running")
                    for tc in self.session.tool_calls
                )
                break

        for tc in self.session.tool_calls:
            if tc.status == "pending":
                pending_calls.append(tc.call_id)

        return {
            "has_pending_tool_calls": bool(pending_calls),
            "pending_call_ids": pending_calls,
            "last_assistant_message_id": last_assistant_id,
            "last_assistant_had_tool_calls": last_assistant_had_tools,
        }

    def can_retry_from_last_state(self) -> bool:
        state = self.get_last_incomplete_state()
        return state["has_pending_tool_calls"] or state["last_assistant_had_tool_calls"]

    async def inject_recovery_tool_results(self) -> int:
        state = self.get_last_incomplete_state()
        if not state["has_pending_tool_calls"]:
            return 0

        count = 0
        for tc in self.session.tool_calls:
            if tc.status == "pending":
                await self.add_tool_result(
                    tool_call_id=tc.call_id,
                    name=tc.tool_name,
                    content="Error: Previous execution was interrupted and no result was recorded. Please retry the operation if needed.",
                    was_loop_strike=False,
                )
                count += 1
        return count

    def truncate_last_assistant_turn(self) -> bool:
        """Remove the most recent *response* — which may span more than
        one assistant MessageRow, since a single agentic exchange can
        involve several internal LLM calls interleaved with tool calls
        (each one gets its own row — see Agent._run_one_turn calling
        add_assistant_message on every loop iteration). Removing only
        the single last row would orphan earlier rounds of the same
        response, leaving them stitched to the newly regenerated one.
        So this walks back through the whole trailing run of
        assistant/reasoning rows and removes all of it, stopping at the
        preceding user (or system) message — ready to be regenerated by
        calling Agent.run() again from there.

        Returns False if there's nothing to remove (empty conversation,
        or it already ends on a user/system message).
        """
        if not self.session.messages or self.session.messages[-1].role not in ("assistant", "reasoning"):
            return False

        cut_idx = len(self.session.messages)
        for i in range(len(self.session.messages) - 1, -1, -1):
            if self.session.messages[i].role in ("assistant", "reasoning"):
                cut_idx = i
            else:
                break

        removed_message_ids = {m.message_id for m in self.session.messages[cut_idx:]}
        self.session.messages = self.session.messages[:cut_idx]
        self.session.tool_calls = [
            tc for tc in self.session.tool_calls if tc.parent_message not in removed_message_ids
        ]
        self._last_message_id = self.session.messages[-1].message_id if self.session.messages else None
        self._checkpoint()
        return True

    # ── Private ──────────────────────────────────────────

    def _flush_reasoning(self) -> None:
        if not self._reasoning_buffer:
            return
        self.session.messages.append(
            MessageRow(message_id=gen_id(), role="reasoning", content=self._reasoning_buffer, timestamp=time.time())
        )
        self._reasoning_buffer = ""

    # ── Stop control (delegates to astra.signals) ────────

    def request_stop(self) -> None:
        if self.signals is not None:
            request_stop(self.signals, self.session_id)

    def is_stop_requested(self) -> bool:
        if self.signals is None:
            return False
        return is_stop_requested(self.signals, self.session_id)

    def clear_stop_flag(self) -> None:
        if self.signals is not None:
            clear_stop_flag(self.signals, self.session_id)