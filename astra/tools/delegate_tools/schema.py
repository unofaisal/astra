DELEGATE_TASK = {
    "name": "delegate_task",
    "description": (
        "Delegate a self-contained sub-task to a named sub-agent and wait for it. It runs "
        "its own isolated multi-turn loop with its own tools and context — "
        "only its final answer is returned here; its intermediate tool "
        "calls and reasoning are not shown in this chat. To run several "
        "sub-agents in parallel, call delegate_task several times in the same step."
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

SPAWN_AGENT = {
    "name": "spawn_agent",
    "description": (
        "Start a sub-agent in the BACKGROUND and return immediately with a task_id, so you can "
        "keep working (or start more sub-agents) while it runs. Collect its answer later with "
        "await_agents. Use this for long, independent sub-tasks; use delegate_task when you "
        "need the answer right away."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "agent_name": {"type": "string", "description": "Name of the sub-agent to start."},
            "task": {
                "type": "string",
                "description": "A complete, self-contained instruction — the agent cannot see this conversation.",
            },
        },
        "required": ["agent_name", "task"],
    },
}

AWAIT_AGENTS = {
    "name": "await_agents",
    "description": (
        "Wait for background sub-agents started with spawn_agent and return their results. "
        "Returns whatever has finished within timeout_seconds; tasks still running are reported "
        "as 'running' — call await_agents again to keep waiting."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task_ids": {"type": "array", "items": {"type": "string"}, "description": "task_id values returned by spawn_agent."},
            "timeout_seconds": {"type": "number", "minimum": 0, "maximum": 600, "description": "How long to wait (default 60)."},
        },
        "required": ["task_ids"],
    },
}

CANCEL_AGENT = {
    "name": "cancel_agent",
    "description": "Cancel a background sub-agent started with spawn_agent.",
    "parameters": {
        "type": "object",
        "properties": {"task_id": {"type": "string", "description": "task_id returned by spawn_agent."}},
        "required": ["task_id"],
    },
}
