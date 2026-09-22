# astra/tools/discovery_tools/search.py
"""Deferred tool loading (progressive disclosure).

With dozens of tools, sending every schema on every request costs tokens
and hurts tool-selection accuracy.  Mark rarely-needed tools
``deferred=True`` (``@tool(..., deferred=True)``, ``define_tool(deferred=True)``,
``"deferred": true`` in a manifest, or ``McpServerConfig(deferred=True)``);
they are hidden until the model finds them here.  Discovered tool names are
persisted on the session (``loaded_tools``) so they stay available.

``search_tools`` itself only appears when at least one deferred tool exists.
"""

from __future__ import annotations

import json

from astra.tools.core import ToolContext
from astra.tools.decorator import tool
from astra.tools.validation import describe_parameters


@tool(schema_name="search_tools", read_only=True, idempotent=True, toolset="discovery")
async def search_tools(args: dict, ctx: ToolContext) -> dict:
    registry = ctx.registry
    query = (args.get("query") or "").strip()
    if registry is None or not query:
        return {"error": "query is required", "error_type": "invalid_args"}
    already = set(ctx.conversation.session.loaded_tools) if ctx.conversation is not None else set()
    hits = [t for t in registry.search(query, limit=10) if t.deferred and t.name not in already]
    if ctx.conversation is not None:
        for t in hits:
            ctx.conversation.session.loaded_tools.append(t.name)
    return {
        "found": [{"name": t.name, "description": t.description, "parameters": describe_parameters(t.parameters)} for t in hits],
        "note": "These tools are now available — call them on your next step." if hits else "No matching tools.",
    }
