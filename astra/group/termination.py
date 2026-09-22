"""When a group turn stops producing replies.

MaxRoundsTermination is ALWAYS applied, no matter what selector or other
termination condition you configure — it cannot be turned off, only
raised. This mirrors every production implementation surveyed (AutoGen's
max_round, Semantic Kernel's TerminationStrategy.maximum_iterations): a
hard cap is cheap insurance against exactly the runaway-loop failure mode
documented in real deployments (one case ran 9+ days / 60,000+ tokens
before a human noticed).

The NoImmediateRepeat guard is the second always-on default (opt-out, not
opt-in): the same participant cannot speak twice in a row as a result of
autonomous continuation. This does not block a user from @mentioning the
same bot twice — it only blocks a bot's own reply from re-triggering
itself, which is the mechanism behind the ping-pong and escalation-spiral
failure modes.
"""

from __future__ import annotations

from typing import Callable, List, Protocol

from .models import GroupSession


class TerminationCondition(Protocol):
    def should_stop(self, session: GroupSession) -> "str | None":
        """Return a reason string to stop, or None to continue."""
        ...


class MaxRoundsTermination:
    def __init__(self, max_rounds: int = 20) -> None:
        self.max_rounds = max_rounds

    def should_stop(self, session: GroupSession) -> "str | None":
        return "max_rounds" if session.round >= self.max_rounds else None


class KeywordTermination:
    """Stop when the most recent message contains any of ``keywords``
    (case-insensitive) — e.g. an "APPROVED"/"TERMINATE" convention agreed
    with the participants' system prompts."""

    def __init__(self, keywords: List[str]) -> None:
        self.keywords = [k.lower() for k in keywords]

    def should_stop(self, session: GroupSession) -> "str | None":
        if not session.messages:
            return None
        text = (session.messages[-1].content or "").lower()
        return "keyword" if any(k in text for k in self.keywords) else None


class FunctionTermination:
    def __init__(self, fn: Callable[[GroupSession], "str | None"]) -> None:
        self.fn = fn

    def should_stop(self, session: GroupSession) -> "str | None":
        return self.fn(session)


class CompositeTermination:
    """Stops as soon as ANY sub-condition fires."""

    def __init__(self, conditions: List[TerminationCondition]) -> None:
        self.conditions = conditions

    def should_stop(self, session: GroupSession) -> "str | None":
        for c in self.conditions:
            reason = c.should_stop(session)
            if reason:
                return reason
        return None


def with_hard_cap(condition: "TerminationCondition | None", max_rounds: int) -> TerminationCondition:
    """Combine a user-supplied condition with the always-on MaxRounds cap.
    Used internally by GroupChat — not something you need to call unless
    you're composing your own runner."""
    conditions: List[TerminationCondition] = [MaxRoundsTermination(max_rounds)]
    if condition is not None:
        conditions.append(condition)
    return CompositeTermination(conditions)
