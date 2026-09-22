"""GroupChat: the orchestrator.

Deliberately NOT built on Agent/Conversation — those are the single
continuous-assistant-identity machinery (see models.py's docstring for
why reusing them would mean bending code that many other, unrelated
features depend on staying simple). What IS reused, unchanged: the
provider layer (ProviderPool, OpenAIProvider.generate), the tool layer
(ToolRegistry, ToolExecutor, ToolRuntime, ToolContext — same concurrency
caps, sandboxing, circuit breakers, telemetry as single-agent tools get),
SessionGuard (one group turn at a time, same exclusivity primitive that
fixed the single-agent lost-update bug), ContextManager (bounds each
per-speaker projection so token cost doesn't grow unboundedly — the exact
gap a real AutoGen GitHub issue reported: the auto-selector prompt "will
continuously grow in token count as the conversation continues" because
nothing trimmed it), and Quota (checked before every speaker's LLM call,
same as Agent).

Default mode is SINGLE PASS: the selector is consulted once per user
message, every name it returns speaks once, in order, and the turn ends
— no bot automatically triggers another bot's reply. This is deliberately
more conservative than raw AutoGen (which lets a manager keep selecting
speakers indefinitely) and matches what shipped chat products actually
default to. Set ``allow_agent_triggered_rounds=True`` to opt into
AutoGen-style continuation (the selector is re-consulted after each
speaker using that speaker's own reply as the trigger) — still bounded by
max_rounds and the always-on no-immediate-repeat guard either way.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from ..concurrency import LimiterBusy
from ..context import ContextManager, ContextPolicy
from ..events import Callbacks
from ..quota import Quota, QuotaExceeded
from ..sessions import SessionBusyError, SessionGuard, SessionOwnershipError, check_owner
from ..telemetry import metrics, span
from ..tools.core import ToolContext
from ..tools.decorator import ToolRegistry
from ..tools.executor import ToolExecutor
from ..tools.runtime import ToolRuntime, get_default_runtime
from .models import GroupMessage, GroupResult, GroupSession, GroupToolCall, GroupTurnResult, gen_group_id
from .projection import USER_AUTHOR, project_for_speaker
from .selection import MentionSelector, SpeakerSelector
from .store import GroupMemoryStore, GroupStore
from .termination import TerminationCondition, with_hard_cap

logger = logging.getLogger(__name__)


class GroupBusyError(SessionBusyError):
    pass


@dataclass
class GroupParticipant:
    """A resolved participant, ready to run — what GroupChat actually
    needs, independent of however you store/name your agent directory
    (reuse astra.tools.delegate_tools.delegate.SubAgentSpec's shape if
    you already have one)."""

    name: str
    system_prompt: str
    provider: Any  # anything with an async .generate(messages, tools=...) -> (message_dict, tool_calls)
    registry: ToolRegistry
    description: str = ""
    max_tool_turns: int = 4  # bound on the tool-call loop WITHIN one speaker's turn
    timeout_seconds: Optional[float] = None


class GroupChat:
    """One running (or resumed) group conversation.

        gc = GroupChat(
            group=GroupSession(group_id="...", user="alice", participants=["researcher", "writer"]),
            participants={"researcher": GroupParticipant(...), "writer": GroupParticipant(...)},
            store=store, runtime=runtime, selector=MentionSelector([...]),
        )
        result = await gc.send("@researcher find three sources on X")

    Safe for many concurrent GroupChats sharing one ToolRuntime/store —
    same pattern as many concurrent Agent/Conversation sessions.
    """

    def __init__(
        self,
        group: GroupSession,
        participants: Dict[str, GroupParticipant],
        *,
        store: Optional[GroupStore] = None,
        runtime: Optional[ToolRuntime] = None,
        selector: Optional[SpeakerSelector] = None,
        termination: Optional[TerminationCondition] = None,
        max_rounds: int = 20,
        allow_agent_triggered_rounds: bool = False,
        guard: Optional[SessionGuard] = None,
        context_manager: Optional[ContextManager] = None,
        quota: Optional[Quota] = None,
        callbacks: Optional[Callbacks] = None,
        session_lock_wait: float = 0.0,
        enforce_owner: bool = False,
    ) -> None:
        missing = set(group.participants) - set(participants)
        if missing:
            raise ValueError(f"GroupSession lists participants with no GroupParticipant given: {sorted(missing)}")
        self.group = group
        self.participants = participants
        self.store = store or GroupMemoryStore()
        self.runtime = runtime or get_default_runtime()
        self.selector = selector or MentionSelector(list(group.participants))
        self.termination = with_hard_cap(termination, max_rounds)
        self.allow_agent_triggered_rounds = allow_agent_triggered_rounds
        self.guard = guard or SessionGuard()
        self.context_manager = context_manager or ContextManager(ContextPolicy())
        self.quota = quota
        self.callbacks = callbacks or Callbacks()
        self.session_lock_wait = session_lock_wait
        self.enforce_owner = enforce_owner

    # ── persistence ──────────────────────────────────────────────
    def save(self) -> None:
        self.group.last_active = time.time()
        self.group.version += 1
        try:
            self.store.save(self.group)
        except BaseException:
            self.group.version -= 1
            raise

    @classmethod
    def load(
        cls,
        group_id: str,
        participants: Dict[str, GroupParticipant],
        *,
        store: GroupStore,
        user: Optional[str] = None,
        enforce_owner: bool = False,
        **kwargs: Any,
    ) -> "GroupChat":
        group = store.get(group_id)
        if group is None:
            raise KeyError(f"No group session '{group_id}'")
        if enforce_owner:
            check_owner(group.user, user, group_id)
        return cls(group, participants, store=store, enforce_owner=enforce_owner, **kwargs)

    # ── public API ───────────────────────────────────────────────
    async def send(self, text: str, *, user: Optional[str] = None) -> GroupResult:
        """Add a user message and run one group turn (one or more
        participants replying, per the selector — see class docstring for
        single-pass vs. agent-triggered-rounds)."""
        if self.enforce_owner:
            check_owner(self.group.user, user, self.group.group_id)
        try:
            async with self.guard.hold(self.group.group_id, wait=self.session_lock_wait):
                return await self._run_turn(text)
        except SessionBusyError as exc:
            metrics.inc("astra_group_busy_total")
            return GroupResult(group_id=self.group.group_id, status="busy", error=str(exc))

    # ── turn loop ────────────────────────────────────────────────
    async def _run_turn(self, user_text: str) -> GroupResult:
        g = self.group
        with span("group_turn", **{"gen_ai.conversation.id": g.group_id}):
            self._append(GroupMessage(message_id=_mid(), author=USER_AUTHOR, content=user_text, round=g.round))
            self.save()

            turns: List[GroupTurnResult] = []
            trigger_author = USER_AUTHOR
            stop_reason = self.termination.should_stop(g)

            while stop_reason is None:
                try:
                    names = await self.selector.select(g, trigger_author=trigger_author)
                except Exception:
                    logger.exception("speaker selection failed for group %s", g.group_id)
                    names = []
                names = [n for n in names if n in self.participants]
                if not names:
                    break

                ran_any = False
                for name in names:
                    # The no-immediate-repeat guard only applies to AGENT-
                    # triggered continuation (trigger_author is a
                    # participant's own prior reply) — it exists to stop a
                    # bot's reply from re-triggering itself, not to block a
                    # single/default participant from answering two
                    # separate, fresh user messages in a row.
                    if trigger_author != USER_AUTHOR and name == g.last_speaker and name not in self._explicit_mentions(g.messages[-1].content or ""):
                        continue  # always-on no-immediate-repeat guard (see termination.py docstring)
                    ran_any = True
                    turn = await self._run_speaker_turn(name)
                    turns.append(turn)
                    g.round += 1
                    self.save()
                    if turn.stopped:
                        stop_reason = turn.message.content and "error" or "stopped"
                        break
                    stop_reason = self.termination.should_stop(g)
                    if stop_reason:
                        break

                if not ran_any:
                    # Every candidate this round was blocked by the no-repeat
                    # guard (e.g. a selector that keeps nominating whoever
                    # just spoke) — nothing left to do. Stop instead of
                    # spinning: re-selecting forever here would be exactly
                    # the runaway-loop failure mode this guard exists to
                    # prevent, just moved up one level.
                    stop_reason = stop_reason or "stalled"
                    break
                if not self.allow_agent_triggered_rounds:
                    break  # single-pass default: selector consulted exactly once per user message
                if stop_reason:
                    break
                trigger_author = g.last_speaker or trigger_author

            if stop_reason is None:
                stop_reason = "done"
            g.ended_reason = None if stop_reason == "done" else stop_reason
            self.save()
            metrics.inc("astra_group_turns_total", status=stop_reason)
            return GroupResult(group_id=g.group_id, status=stop_reason, turns=turns)

    async def _run_speaker_turn(self, name: str) -> GroupTurnResult:
        g = self.group
        p = self.participants[name]
        ctx_deadline = time.monotonic() + p.timeout_seconds if p.timeout_seconds else None

        if self.quota is not None:
            try:
                self.quota.check(g.user, g.tenant)
            except QuotaExceeded as exc:
                msg = GroupMessage(message_id=_mid(), author=name, content=f"(quota exceeded: {exc})", round=g.round)
                self._append(msg)
                g.last_speaker = name
                return GroupTurnResult(speaker=name, message=msg, stopped=True)

        await self.callbacks.emit("on_event", type="group_turn_start", group_id=g.group_id, speaker=name)

        messages = project_for_speaker(g, name, include_intro=True, system_prompt=p.system_prompt)
        messages = self.context_manager.prepare(messages)
        tools = p.registry.get_tool_schemas() or None
        executor = ToolExecutor(p.registry, runtime=self.runtime)

        tool_calls_made: List[GroupToolCall] = []
        final_text = ""
        usage = {"input_tokens": 0, "output_tokens": 0, "cost": 0.0}

        for _ in range(max(1, p.max_tool_turns)):
            try:
                message_obj, calls = await p.provider.generate(messages, tools=tools)
            except Exception as exc:
                logger.exception("group %s: speaker %s failed", g.group_id, name)
                final_text = f"(error: {exc})"
                break

            usage["input_tokens"] += int(message_obj.get("input_tokens") or 0)
            usage["output_tokens"] += int(message_obj.get("output_tokens") or 0)
            usage["cost"] += float(message_obj.get("estimated_cost") or 0.0)
            content = message_obj.get("content") or ""
            if content:
                final_text = content

            if not calls:
                break

            messages.append({"role": "assistant", "content": content, "tool_calls": [_as_wire(c) for c in calls]})
            tool_results_text: List[str] = []
            for call in calls:
                fn = call.function
                ctx = ToolContext(
                    tool_name=fn.name,
                    call_id=call.id,
                    session_id=g.group_id,
                    user=g.user,
                    tenant=g.tenant,
                    deadline=ctx_deadline,
                    runtime=self.runtime,
                    registry=p.registry,
                )
                result = await executor.execute(fn.name, fn.arguments, ctx=ctx)
                tool_calls_made.append(GroupToolCall(tool_name=fn.name, arguments=fn.arguments, summary=result.to_wire()[:2000], ok=result.ok))
                tool_results_text.append(f"{fn.name} -> {result.to_wire()[:2000]}")
                messages.append({"role": "tool", "tool_call_id": call.id, "content": result.to_wire()})
            # Loop again so the speaker can react to its own tool results
            # within this one turn (bounded by max_tool_turns above).

        msg = GroupMessage(
            message_id=_mid(),
            author=name,
            content=final_text,
            tool_calls=tool_calls_made,
            round=g.round,
            input_tokens=usage["input_tokens"],
            output_tokens=usage["output_tokens"],
            estimated_cost=usage["cost"],
            mentions=self._explicit_mentions(final_text),
        )
        self._append(msg)
        g.last_speaker = name
        await self.callbacks.emit("on_event", type="group_turn_end", group_id=g.group_id, speaker=name, content=final_text)
        return GroupTurnResult(speaker=name, message=msg, stopped=False)

    def _explicit_mentions(self, text: str) -> List[str]:
        from .selection import parse_mentions

        return parse_mentions(text, list(self.participants))

    def _append(self, msg: GroupMessage) -> None:
        self.group.messages.append(msg)


def _mid() -> str:
    from ..storage.base import gen_id

    return gen_id("gmsg_")


def _as_wire(call: Any) -> dict:
    return {"id": call.id, "type": "function", "function": {"name": call.function.name, "arguments": call.function.arguments}}
