CREATE_SKILL = {
    "name": "create_skill",
    "description": (
        "Create a new skill as a markdown file: a reusable chunk of "
        "instructions/reference material the agent (or a delegated "
        "sub-agent) can load on demand."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Short slug for the skill, used as the filename (letters/numbers/hyphens/underscores).",
            },
            "description": {
                "type": "string",
                "description": "A short, one-line summary of what this skill does.",
            },
            "content": {
                "type": "string",
                "description": "Markdown body — the instructions/guidance the agent sees when the skill is loaded.",
            },
        },
        "required": ["name", "description", "content"],
    },
}

VIEW_SKILL = {
    "name": "view_skill",
    "description": "Fetch the full content of a skill by name.",
    "parameters": {
        "type": "object",
        "properties": {
            "skill_name": {"type": "string", "description": "The name of the skill to fetch."},
        },
        "required": ["skill_name"],
    },
}

LIST_SKILLS = {
    "name": "list_skills",
    "description": "List available skills with their one-line description.",
    "parameters": {
        "type": "object",
        "properties": {
            "limit": {"type": "integer", "description": "Maximum number of skills to return. Defaults to 20."},
        },
        "required": [],
    },
}
