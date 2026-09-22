"""The crux of group chat: turning ONE shared transcript into the
per-speaker view a chat-completion API requires.

Every provider's API needs exactly one continuous "assistant" lineage per
request — there's no "third-party" role. So right before a participant
speaks, its own past turns are projected as ``assistant`` and EVERYONE
else's (the user's, and every other participant's) are projected as
``user``, name-prefixed so the model can tell speakers apart
(``"[Alice]: ..."``). This is the same technique AutoGen's ConversableAgent
uses internally; see astra/group/types.py's docstring for why this can't
just be a flag on Conversation.get_messages().

Tool activity is replayed as a short text summary rather than structured
tool_call/tool role blocks, for every speaker, including the speaker's own
past turns. Two reasons: (1) most providers require an assistant
tool_calls message to be IMMEDIATELY followed by matching tool-role
messages, which breaks the moment another speaker's turn is interleaved
in between across rounds; (2) AutoGen's own GroupChat.append() does the
same thing for the same reason — it casts content to plain text "so that
it can be managed by text-based models". A participant that needs fresh
data just calls the tool again; group turns are short (see chat.py's
per-turn tool-call cap), so this costs little.
"""

from __future__ import annotations

from typing import Any

from .models import GroupMessage, GroupSession

USER_AUTHOR = "__user__"


def _render_tool_activity(msg: GroupMessage) -> str:
    if not msg.tool_calls:
        return ""
    lines = []
    for tc in msg.tool_calls:
        tag = "ok" if tc.ok else "error"
        summary = tc.summary[:500] if tc.summary else ""
        lines.append(f"[used tool {tc.tool_name} ({tag})] {summary}".rstrip())
    return "\n".join(lines)


def project_for_speaker(
    session: GroupSession,
    speaker: str,
    *,
    include_intro: bool = False,
    system_prompt: str | None = None,
) -> list[dict[str, Any]]:
    """Build the chat-completion message list ``speaker`` should see,
    covering ``session.messages`` in order. Pure — never mutates
    ``session``."""
    out: list[dict[str, Any]] = []
    if system_prompt:
        out.append({"role": "system", "content": system_prompt})
    if include_intro and len(session.participants) > 1:
        others = [p for p in session.participants if p != speaker]
        if others:
            out.append(
                {
                    "role": "system",
                    "content": "You are in a group conversation with the user and these other participants: " + ", ".join(others) + ". "
                    "Messages from them appear prefixed with their name, e.g. \"[Name]: ...\".",
                }
            )

    for m in session.messages:
        text = m.content or ""
        activity = _render_tool_activity(m)
        body = text if not activity else (f"{text}\n{activity}" if text else activity)
        if not body:
            continue

        if m.author == speaker:
            out.append({"role": "assistant", "content": body})
        elif m.author == USER_AUTHOR:
            out.append({"role": "user", "content": body})
        else:
            out.append({"role": "user", "content": f"[{m.author}]: {body}"})
    return out


def render_transcript(session: GroupSession, *, max_chars: int | None = None) -> str:
    """Plain-text transcript — used by moderator/selector prompts and by
    astra.context-style summarization, never sent as the "history" a
    participant replies into (that's project_for_speaker's job)."""
    lines = []
    for m in session.messages:
        who = "user" if m.author == USER_AUTHOR else m.author
        text = m.content or ""
        activity = _render_tool_activity(m)
        body = text if not activity else (f"{text} | {activity}" if text else activity)
        if body:
            lines.append(f"{who}: {body}")
    text = "\n".join(lines)
    if max_chars and len(text) > max_chars:
        head = max_chars // 2
        text = text[:head] + "\n…[middle omitted]…\n" + text[-(max_chars - head) :]
    return text
