# astra/tools/clarify_approval_tools/request_clarification.py
"""Pause the turn to ask the user a genuine clarifying question.

No transport code lives here — this tool only returns a pause marker.
Agent._execute_tool_calls_parallel sees the marker, calls
Conversation.pause_for_clarification (which persists the pause and
raises ClarificationPending), and Agent.run emits "on_clarification_request"
through whatever Callbacks the caller supplied (websocket, SSE, a queue,
your own notification system — astra has no opinion).
"""

import json
import uuid

from astra.agent.conversation import CLARIFICATION_PENDING_KEY
from astra.tools.decorator import tool

# CLARIFICATION_PENDING_KEY lives in conversation.py (not agent.py) so this
# tool and agent.py can both import it without a circular dependency —
# conversation.py depends on neither.


@tool(schema_name="request_clarification")
def request_clarification(args: dict, **kwargs) -> str:
    """Pause the current turn to ask the user a genuine clarifying question
    before continuing — the same pattern as Claude's own clarify tool.

    Call this only when you're actually blocked by ambiguity you can't
    resolve with a tool call. Do not call this as a substitute for using
    an available lookup tool.

    args:
        question (str, required): the actual question, in plain language.
        options (list[str], optional): 2-5 short, concrete answers the
            user can tap instead of typing.
        allow_free_text (bool, optional): whether a typed answer is also
            accepted alongside/instead of the options. Defaults to True
            when `options` is omitted, False when options are given and
            this isn't set explicitly.

    This does not block — it returns immediately with a pause marker.
    The turn resumes once the user answers (via
    Conversation.resolve_clarification / runner.resume_after_clarification),
    and their answer arrives as this tool's result on the next turn.
    """
    question = (args.get("question") or "").strip()
    if not question:
        return "Error: request_clarification requires a non-empty 'question'."

    raw_options = args.get("options") or []
    options = [str(o).strip() for o in raw_options if str(o).strip()][:5]

    allow_free_text = args.get("allow_free_text")
    if allow_free_text is None:
        allow_free_text = not options
    else:
        allow_free_text = bool(allow_free_text)

    clarification_id = f"clar_{uuid.uuid4().hex[:10]}"

    return json.dumps(
        {
            CLARIFICATION_PENDING_KEY: True,
            "clarification_id": clarification_id,
            "question": question,
            "options": options,
            "allow_free_text": allow_free_text,
        }
    )
