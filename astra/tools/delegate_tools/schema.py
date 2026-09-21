DELEGATE_TASK = {
    "name": "delegate_task",
    "description": (
        "Delegate a self-contained sub-task to a named sub-agent. It runs "
        "its own isolated multi-turn loop with its own tools and context — "
        "only its final answer is returned here; its intermediate tool "
        "calls and reasoning are not shown in this chat."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "agent_name": {
                "type": "string",
                "description": "Name of the sub-agent to delegate to.",
            },
            "task": {
                "type": "string",
                "description": "A complete, self-contained instruction for the agent — it has no access to this conversation's history, so include everything it needs.",
            },
        },
        "required": ["agent_name", "task"],
    },
}
