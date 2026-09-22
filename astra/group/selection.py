"""Who speaks next.

Deterministic mention-based selection is the DEFAULT, not an LLM-based
"auto" mode. AutoGen's own docs recommend deterministic rules "when
simple rules are sufficient and more reliable" over its LLM-selected
default — and a documented AutoGen incident (agents replying to each
other unsupervised) ran for 9+ days and burned 60,000+ tokens before
anyone noticed. An LLM moderator is still available (ModeratorSelector)
for when you actually want "let them figure out who should answer" —
just not as the thing that runs by default.

All selectors implement the same tiny interface, so a custom one (a state
machine, "editor always follows writer", whatever your app needs) is just
a function — mirroring AutoGen's own escape hatch for exactly this reason.
"""

from __future__ import annotations

import re
from typing import Any, Awaitable, Callable, List, Optional, Protocol

from .projection import USER_AUTHOR, render_transcript
from .models import GroupSession

_MENTION_RE = re.compile(r"@([A-Za-z][A-Za-z0-9_\-]{0,63})")


def parse_mentions(text: str, participants: List[str]) -> List[str]:
    """@name tokens in ``text`` that match a real participant, in the
    order they appear, de-duplicated. Case-insensitive; "@everyone" (or
    "@all") expands to every participant."""
    lower_map = {p.lower(): p for p in participants}
    found: List[str] = []
    for tok in _MENTION_RE.findall(text or ""):
        low = tok.lower()
        if low in ("everyone", "all"):
            for p in participants:
                if p not in found:
                    found.append(p)
            continue
        real = lower_map.get(low)
        if real and real not in found:
            found.append(real)
    return found


class SpeakerSelector(Protocol):
    """Returns the participant name(s) to speak this round, in the order
    they should go (sequentially — see chat.py). Empty list ends the
    group turn with no further replies."""

    async def select(self, session: GroupSession, *, trigger_author: str) -> List[str]: ...


class MentionSelector:
    """DEFAULT. Looks at @mentions in the triggering message. No mention →
    a single default participant (or the last speaker, if configured).
    Never chains — this selector is only consulted once per user message,
    by design (see chat.py's default single_pass mode)."""

    def __init__(self, participants: List[str], *, default: Optional[str] = None, fallback_to_last_speaker: bool = False) -> None:
        self.participants = participants
        self.default = default or (participants[0] if participants else None)
        self.fallback_to_last_speaker = fallback_to_last_speaker

    async def select(self, session: GroupSession, *, trigger_author: str) -> List[str]:
        trigger = next((m for m in reversed(session.messages) if m.message_id == trigger_author or True), None)
        text = session.messages[-1].content if session.messages else ""
        mentioned = parse_mentions(text or "", self.participants)
        if mentioned:
            return mentioned
        if self.fallback_to_last_speaker and session.last_speaker:
            return [session.last_speaker]
        return [self.default] if self.default else []


class RoundRobinSelector:
    """Sequential, in participant order — AutoGen's other "reliable rule"
    option. Useful for a fixed pipeline (researcher -> writer -> reviewer)."""

    def __init__(self, participants: List[str]) -> None:
        self.participants = participants

    async def select(self, session: GroupSession, *, trigger_author: str) -> List[str]:
        if not self.participants:
            return []
        if session.last_speaker in self.participants:
            i = self.participants.index(session.last_speaker)
            return [self.participants[(i + 1) % len(self.participants)]]
        return [self.participants[0]]


class StaticSelector:
    """Always the same participant(s) — the simplest possible selector,
    useful for a single-bot "group" used only for its shared-thread
    bookkeeping, or as a building block inside a CompositeSelector."""

    def __init__(self, names: List[str]) -> None:
        self.names = list(names)

    async def select(self, session: GroupSession, *, trigger_author: str) -> List[str]:
        return list(self.names)


CustomSelectorFn = Callable[[GroupSession, str], "List[str] | Awaitable[List[str]]"]


class FunctionSelector:
    """Wrap a plain function/coroutine — for a hand-written state machine,
    same escape hatch AutoGen recommends over its own LLM selector for
    production use."""

    def __init__(self, fn: CustomSelectorFn) -> None:
        self.fn = fn

    async def select(self, session: GroupSession, *, trigger_author: str) -> List[str]:
        import inspect

        result = self.fn(session, trigger_author)
        if inspect.isawaitable(result):
            result = await result
        return list(result or [])


class ModeratorSelector:
    """OPT-IN. One LLM call reads the transcript + participant
    descriptions and names who should respond. This is AutoGen's "auto"
    mode — real, useful, but priced and risked accordingly: one extra LLM
    call every round, and a wrong/hallucinated name has to be handled.
    Prefer MentionSelector/RoundRobinSelector/FunctionSelector unless you
    specifically want "let them decide"."""

    def __init__(self, participants: List[str], descriptions: dict[str, str], provider: Any, *, max_history_chars: int = 8000) -> None:
        self.participants = participants
        self.descriptions = descriptions
        self.provider = provider
        self.max_history_chars = max_history_chars

    async def select(self, session: GroupSession, *, trigger_author: str) -> List[str]:
        roles = "\n".join(f"- {p}: {self.descriptions.get(p, '(no description)')}" for p in self.participants)
        transcript = render_transcript(session, max_chars=self.max_history_chars)
        prompt = (
            "Below is a group conversation and a list of participants.\n\n"
            f"Participants:\n{roles}\n\nConversation so far:\n{transcript}\n\n"
            "Who should respond next? Reply with ONLY one name from the participant list above, exactly as written, "
            "and nothing else."
        )
        try:
            msg, _ = await self.provider.generate([{"role": "system", "content": prompt}], tools=None)
        except Exception:
            return []
        name = (msg.get("content") or "").strip().strip(".:")
        for p in self.participants:
            if p.lower() == name.lower() or p.lower() in name.lower():
                return [p]
        return []
