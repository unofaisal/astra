"""Context management — keeps long sessions cheap and inside the window.

Before this module astra replayed the ENTIRE history (every tool result in
full) on every turn, so cost and latency grew with session length, and the
``max_context_chars`` setting was defined but never used.

Strategy (all opt-out, all conservative — nothing happens until the
request is actually large):

  1. **Replay-time trimming** (pure, never mutates the stored session):
     when the estimated size passes ``trigger_ratio × budget``, cap
     oversized tool results, then replace the *oldest* tool results with a
     short placeholder (the newest ``keep_last_tool_results`` stay intact).
     Message/tool-call pairing is preserved, so provider validation holds.
  2. **Optional summarization** (``summarize=True``): older turns are
     folded into a rolling summary persisted on the session
     (``context_summary`` / ``summary_upto``) and replayed as one system
     message.  The cut always lands on a user-turn boundary.

Token estimates use the provider-reported prompt size of the last turn
when available, else ``len(json)/chars_per_token``.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

SUMMARY_PROMPT = (
    "You compress an agent conversation so it can continue with less context. "
    "Write a faithful, compact summary of the transcript: user goals, decisions made, "
    "key facts and identifiers (IDs, names, numbers, file paths), tool results that still matter, "
    "and what remains to be done. Do not invent anything. Plain text, no preamble."
)


@dataclass
class ContextPolicy:
    max_tokens: Optional[int] = None  # None → derive from max_chars
    max_chars: Optional[int] = 1_000_000
    chars_per_token: float = 4.0
    trigger_ratio: float = 0.8
    keep_last_tool_results: int = 8
    max_tool_result_chars: int = 20_000
    clear_placeholder: str = "[Earlier tool result removed to save context. Call the tool again if you still need it.]"
    summarize: bool = False
    keep_recent_turns: int = 4
    summary_input_chars: int = 60_000
    summary_max_tokens: int = 1_000


Summarizer = Callable[[str, Optional[str]], Awaitable[str]]


class ContextManager:
    def __init__(self, policy: Optional[ContextPolicy] = None, summarizer: Optional[Summarizer] = None) -> None:
        self.policy = policy or ContextPolicy()
        self.summarizer = summarizer

    # ── sizing ───────────────────────────────────────────────────
    def budget_tokens(self) -> int:
        p = self.policy
        if p.max_tokens:
            return p.max_tokens
        return int((p.max_chars or 1_000_000) / p.chars_per_token)

    def estimate_tokens(self, messages: List[Dict[str, Any]]) -> int:
        try:
            n = len(json.dumps(messages, default=str))
        except Exception:
            n = sum(len(str(m)) for m in messages)
        return int(n / self.policy.chars_per_token)

    def over_budget(self, messages: List[Dict[str, Any]], last_prompt_tokens: Optional[int] = None) -> bool:
        limit = self.budget_tokens() * self.policy.trigger_ratio
        est = self.estimate_tokens(messages)
        if last_prompt_tokens:
            est = max(est, last_prompt_tokens)
        return est > limit

    # ── replay-time trimming (pure) ──────────────────────────────
    def prepare(self, messages: List[Dict[str, Any]], last_prompt_tokens: Optional[int] = None) -> List[Dict[str, Any]]:
        if not self.over_budget(messages, last_prompt_tokens):
            return messages
        from .tools.executor import truncate_text

        p = self.policy
        out = [dict(m) for m in messages]
        tool_idx = [i for i, m in enumerate(out) if m.get("role") == "tool"]

        # Stage 1: cap giant tool results.
        for i in tool_idx:
            c = out[i].get("content")
            if isinstance(c, str) and len(c) > p.max_tool_result_chars:
                out[i]["content"] = truncate_text(c, p.max_tool_result_chars)

        # Stage 2: clear the oldest tool results until we fit.
        limit = self.budget_tokens() * p.trigger_ratio
        clearable = tool_idx[: max(0, len(tool_idx) - p.keep_last_tool_results)]
        for i in clearable:
            if self.estimate_tokens(out) <= limit:
                break
            out[i]["content"] = p.clear_placeholder
        return out

    # ── summarization ────────────────────────────────────────────
    async def maybe_compact(self, conv: Any, provider: Any) -> bool:
        """Fold old turns into ``session.context_summary``.  Returns True if
        the session changed.  No-op unless ``policy.summarize``."""
        p = self.policy
        if not p.summarize:
            return False
        sess = conv.session
        last_in = 0
        for m in reversed(sess.messages):
            if m.role == "assistant" and m.input_tokens:
                last_in = m.input_tokens
                break
        est_msgs = conv.get_messages()
        if not self.over_budget(est_msgs, last_in):
            return False

        rows = sess.messages
        start = 0
        if sess.summary_upto:
            for i, r in enumerate(rows):
                if r.message_id == sess.summary_upto:
                    start = i + 1
                    break
        user_positions = [i for i in range(start, len(rows)) if rows[i].role == "user"]
        if len(user_positions) <= p.keep_recent_turns:
            return False
        cut = user_positions[-p.keep_recent_turns]  # first row of the kept region
        to_fold = rows[start:cut]
        if not to_fold:
            return False

        transcript = self._render(conv, to_fold)
        try:
            if self.summarizer is not None:
                summary = await self.summarizer(transcript, sess.context_summary)
            else:
                summary = await self._summarize_with_provider(provider, transcript, sess.context_summary, conv)
        except Exception:
            logger.exception("context summarization failed; continuing without compaction")
            return False
        if not summary or not summary.strip():
            return False
        sess.context_summary = summary.strip()
        sess.summary_upto = rows[cut - 1].message_id
        conv.save()
        logger.info("session %s: folded %d messages into a summary (%d chars)", sess.session_id, len(to_fold), len(sess.context_summary))
        return True

    def _render(self, conv: Any, rows: List[Any]) -> str:
        by_parent: Dict[str, List[Any]] = {}
        for tc in conv.session.tool_calls:
            by_parent.setdefault(tc.parent_message, []).append(tc)
        parts: List[str] = []
        for r in rows:
            if r.role == "user":
                parts.append(f"USER: {r.content or ''}")
            elif r.role == "assistant":
                if r.content:
                    parts.append(f"ASSISTANT: {r.content}")
                for tc in by_parent.get(r.message_id, []):
                    res = (tc.result or tc.error or "")[:600]
                    parts.append(f"TOOL {tc.tool_name}({tc.arguments[:300]}) -> {res}")
        text = "\n".join(parts)
        cap = self.policy.summary_input_chars
        return text if len(text) <= cap else text[:cap // 2] + "\n…[middle omitted]…\n" + text[-cap // 2:]

    async def _summarize_with_provider(self, provider: Any, transcript: str, previous: Optional[str], conv: Any = None) -> str:
        user = (f"Existing summary of even earlier conversation:\n{previous}\n\n" if previous else "") + f"Transcript to fold in:\n{transcript}"
        msg, _ = await provider.generate(
            [{"role": "system", "content": SUMMARY_PROMPT}, {"role": "user", "content": user}], tools=None
        )
        if conv is not None:
            conv.add_side_usage(msg or {})
        return (msg or {}).get("content") or ""
