SEARCH_TOOLS = {
    "name": "search_tools",
    "description": (
        "Find additional tools by keyword. Some tools are not loaded up front to keep the context small; "
        "search here when you need a capability you don't see. Matching tools become available on your next step."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "What you want to do, in a few keywords (e.g. 'pdf extract table')."},
        },
        "required": ["query"],
    },
}
