"""Multi-agent group chat: several named agents sharing one visible
thread with a user, as opposed to astra.tools.delegate_tools' agent-as-
tool (call/return, isolated context) pattern.

    from astra.group import GroupChat, GroupParticipant, GroupSession

    group = GroupSession(group_id="g1", user="alice", participants=["researcher", "writer"])
    gc = GroupChat(group, {
        "researcher": GroupParticipant(name="researcher", system_prompt="...", provider=..., registry=...),
        "writer": GroupParticipant(name="writer", system_prompt="...", provider=..., registry=...),
    })
    result = await gc.send("@researcher find three sources on X")
    print(result.final_text)

See astra/group/chat.py's module docstring for the design rationale
(why this is a separate module from Conversation/Agent rather than an
extension of them) and astra/group/selection.py for why mention-based
routing is the default instead of an LLM-selected "auto" mode.
"""

from .chat import GroupBusyError, GroupChat, GroupParticipant
from .models import GroupMessage, GroupResult, GroupSession, GroupToolCall, GroupTurnResult, gen_group_id
from .projection import USER_AUTHOR, project_for_speaker, render_transcript
from .selection import (
    FunctionSelector,
    MentionSelector,
    ModeratorSelector,
    RoundRobinSelector,
    SpeakerSelector,
    StaticSelector,
    parse_mentions,
)
from .store import GroupMemoryStore, GroupSQLiteStore, GroupStore
from .termination import (
    CompositeTermination,
    FunctionTermination,
    KeywordTermination,
    MaxRoundsTermination,
    TerminationCondition,
    with_hard_cap,
)

__all__ = [
    "GroupChat",
    "GroupParticipant",
    "GroupBusyError",
    "GroupSession",
    "GroupMessage",
    "GroupToolCall",
    "GroupTurnResult",
    "GroupResult",
    "gen_group_id",
    "USER_AUTHOR",
    "project_for_speaker",
    "render_transcript",
    "SpeakerSelector",
    "MentionSelector",
    "RoundRobinSelector",
    "StaticSelector",
    "FunctionSelector",
    "ModeratorSelector",
    "parse_mentions",
    "GroupStore",
    "GroupMemoryStore",
    "GroupSQLiteStore",
    "TerminationCondition",
    "MaxRoundsTermination",
    "KeywordTermination",
    "FunctionTermination",
    "CompositeTermination",
    "with_hard_cap",
]
