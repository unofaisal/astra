# astra/tools/decorator.py
"""Tool registration: the legacy ``@tool`` + ``schema.py`` convention, the
new function-schema decorator, and the ``ToolRegistry``.

Everything the old API offered still works unchanged
(``@tool(schema_name=...)``, ``load_schemas``, ``register_tool``,
``executors``, ``get_tool_schemas``, ``get_tool_names``, ``disable``).
New capabilities are additive: ``register(Tool)``, ``extend(tools)``,
``view(...)`` (per-agent filtered snapshot — no mutation of the shared
registry), toolsets, availability checks, deferred tools, and a
deterministic schema order (stable prompt prefix → provider prompt-cache
hits).
"""

from __future__ import annotations

import inspect
import logging
import re
import threading
import typing
from collections.abc import Callable
from typing import Any, Dict, Iterable, List, Optional, Set

from .core import Tool, ToolAnnotations, ToolContext
from .handlers import FunctionHandler

logger = logging.getLogger(__name__)


def tool(
    schema_name: str,
    *,
    idempotent: bool = False,
    read_only: bool = False,
    destructive: bool = False,
    open_world: bool = False,
    timeout: Optional[float] = None,
    toolset: Optional[str] = None,
    blocking: bool = False,
    max_output_chars: Optional[int] = None,
    max_concurrency: Optional[int] = None,
    deferred: bool = False,
    check: Optional[Callable[[], bool]] = None,
    validate: bool = True,
) -> Callable:
    """Marks a function as a tool and links it to its schema name.

    The schema MUST be defined as a variable in the group's schema.py file.
    All keyword options are optional and additive:

      idempotent   safe to retry automatically (default False → never retried)
      read_only / destructive / open_world   behavioural hints (MCP-style)
      timeout      per-call seconds (default: runtime default)
      blocking     run a *sync* function in the runtime's thread pool
      deferred     hidden from the model until found via ``search_tools``
      check        () -> bool availability gate; False hides the tool
    """

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        func.__is_tool__ = True
        func.__schema_name__ = schema_name
        func.__tool_opts__ = {
            "annotations": ToolAnnotations(read_only=read_only, destructive=destructive, idempotent=idempotent, open_world=open_world),
            "timeout": timeout,
            "toolset": toolset,
            "blocking": blocking,
            "max_output_chars": max_output_chars,
            "max_concurrency": max_concurrency,
            "deferred": deferred,
            "check": check,
            "validate": validate,
        }
        return func

    return decorator


# ── function → JSON schema (so new tools need no schema.py) ─────────

_PY_TO_JSON = {str: "string", int: "integer", float: "number", bool: "boolean", dict: "object", list: "array"}


def _annotation_schema(ann: Any) -> Dict[str, Any]:
    origin = typing.get_origin(ann)
    args = typing.get_args(ann)
    if ann in _PY_TO_JSON:
        return {"type": _PY_TO_JSON[ann]}
    if origin is typing.Literal:
        return {"enum": list(args)}
    if origin in (list, List):
        return {"type": "array", "items": _annotation_schema(args[0]) if args else {}}
    if origin in (dict, Dict):
        return {"type": "object"}
    if origin is typing.Union or (hasattr(__import__("types"), "UnionType") and origin is getattr(__import__("types"), "UnionType")):
        non_none = [a for a in args if a is not type(None)]
        if len(non_none) == 1:
            return _annotation_schema(non_none[0])
        return {"anyOf": [_annotation_schema(a) for a in non_none]}
    return {}


def _param_docs(doc: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    m = re.search(r"(?:Args|Arguments|Parameters):\s*\n((?:[ \t]+.*\n?)+)", doc or "")
    if m:
        for line in m.group(1).splitlines():
            mm = re.match(r"\s+(\w+)(?:\s*\([^)]*\))?:\s*(.+)", line)
            if mm:
                out[mm.group(1)] = mm.group(2).strip()
    return out


def schema_from_function(func: Callable[..., Any]) -> Dict[str, Any]:
    sig = inspect.signature(func)
    try:
        hints = typing.get_type_hints(func)
    except Exception:
        hints = {}
    docs = _param_docs(inspect.getdoc(func) or "")
    props: Dict[str, Any] = {}
    required: List[str] = []
    for name, p in sig.parameters.items():
        if name in ("self", "cls", "ctx") or p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD):
            continue
        if hints.get(name) is ToolContext:
            continue
        spec = _annotation_schema(hints.get(name, str))
        if name in docs:
            spec["description"] = docs[name]
        props[name] = spec
        if p.default is inspect.Parameter.empty:
            required.append(name)
    schema: Dict[str, Any] = {"type": "object", "properties": props}
    if required:
        schema["required"] = required
    return schema


def define_tool(
    name: Optional[str] = None,
    description: Optional[str] = None,
    *,
    parameters: Optional[Dict[str, Any]] = None,
    idempotent: bool = False,
    read_only: bool = False,
    destructive: bool = False,
    open_world: bool = False,
    timeout: Optional[float] = None,
    toolset: str = "default",
    blocking: bool = False,
    max_output_chars: Optional[int] = None,
    max_concurrency: Optional[int] = None,
    deferred: bool = False,
    check: Optional[Callable[[], bool]] = None,
) -> Callable[[Callable[..., Any]], Tool]:
    """Turn a typed function into a ``Tool`` (schema derived from type hints
    and the docstring's ``Args:`` section).  No ``schema.py`` needed::

        @define_tool(read_only=True, idempotent=True)
        async def get_weather(city: str, units: Literal["c", "f"] = "c") -> dict:
            '''Current weather for a city.

            Args:
                city: City name, e.g. "Nairobi".
            '''

    Returns a ``Tool``; ``load_tools`` picks up module-level ``Tool``
    instances automatically, or call ``registry.register(tool)``.
    """

    def decorator(func: Callable[..., Any]) -> Tool:
        doc = inspect.getdoc(func) or ""
        desc = description or (doc.split("\n\n")[0].strip() if doc else func.__name__)
        return Tool(
            name=name or func.__name__,
            description=desc,
            parameters=parameters or schema_from_function(func),
            handler=FunctionHandler(func, blocking=blocking),
            toolset=toolset,
            annotations=ToolAnnotations(read_only=read_only, destructive=destructive, idempotent=idempotent, open_world=open_world),
            timeout=timeout,
            max_output_chars=max_output_chars,
            max_concurrency=max_concurrency,
            deferred=deferred,
            check=check,
        )

    return decorator


# ── registry ────────────────────────────────────────────────────

class ToolRegistry:
    def __init__(self) -> None:
        # Legacy surface (kept because tests/callers read it):
        self._tools: dict[str, dict[str, Any]] = {}  # name -> OpenAI function schema
        self.executors: dict[str, Callable] = {}  # name -> callable
        # New surface:
        self.tools: dict[str, Tool] = {}  # fully wired tools
        self.load_errors: list[str] = []
        self._lock = threading.RLock()
        self._pending_toolset: Optional[str] = None

    # ── legacy schema/func wiring ────────────────────────────────
    def load_schemas(self, schemas: dict[str, dict], toolset: Optional[str] = None) -> None:
        """Injects hand-crafted JSON schemas from schema.py files."""
        with self._lock:
            for name, schema in schemas.items():
                self._tools[name] = {
                    "type": "function",
                    "function": {
                        "name": schema["name"],
                        "description": schema.get("description", ""),
                        "parameters": schema.get("parameters", {"type": "object", "properties": {}}),
                    },
                }
                if toolset:
                    self._schema_toolsets()[name] = toolset

    def _schema_toolsets(self) -> Dict[str, str]:
        if not hasattr(self, "_toolset_hints"):
            self._toolset_hints: Dict[str, str] = {}
        return self._toolset_hints

    def register_tool(self, func: Callable) -> None:
        """Wires a python function to its pre-loaded schema."""
        name = func.__schema_name__
        with self._lock:
            if name not in self._tools:
                raise ValueError(f"Schema '{name}' not found in registry. Did you define it in the group's schema.py?")
            fn_schema = self._tools[name]["function"]
            opts = getattr(func, "__tool_opts__", {}) or {}
            toolset = opts.get("toolset") or self._schema_toolsets().get(name) or "default"
            t = Tool(
                name=name,
                description=fn_schema.get("description", ""),
                parameters=fn_schema.get("parameters") or {"type": "object", "properties": {}},
                handler=FunctionHandler(func, blocking=opts.get("blocking", False)),
                toolset=toolset,
                annotations=opts.get("annotations") or ToolAnnotations(),
                timeout=opts.get("timeout"),
                max_output_chars=opts.get("max_output_chars"),
                max_concurrency=opts.get("max_concurrency"),
                check=opts.get("check"),
                deferred=opts.get("deferred", False),
                validate=opts.get("validate", True),
            )
            self.tools[name] = t
            self.executors[name] = func

    # ── new-style registration ───────────────────────────────────
    def register(self, tool_obj: Tool, *, replace: bool = False) -> None:
        with self._lock:
            if tool_obj.name in self.tools and not replace:
                raise ValueError(f"Tool '{tool_obj.name}' is already registered (pass replace=True to override).")
            self.tools[tool_obj.name] = tool_obj
            self._tools[tool_obj.name] = tool_obj.schema()
            self.executors[tool_obj.name] = getattr(tool_obj.handler, "func", None) or tool_obj.handler

    def extend(self, tools: Iterable[Tool], *, replace: bool = False) -> List[str]:
        names = []
        for t in tools:
            try:
                self.register(t, replace=replace)
                names.append(t.name)
            except ValueError as exc:
                self.load_errors.append(str(exc))
                logger.error(str(exc))
        return names

    def unregister(self, name: str) -> None:
        with self._lock:
            self._tools.pop(name, None)
            self.executors.pop(name, None)
            self.tools.pop(name, None)

    def get(self, name: str) -> Optional[Tool]:
        return self.tools.get(name)

    # ── discovery ───────────────────────────────────────────────
    def get_tool_schemas(self, loaded: Optional[Iterable[str]] = None) -> list:
        """OpenAI-format schemas, sorted by name (deterministic order keeps
        the request prefix stable for provider prompt caching).  Tools
        whose ``check`` fails are hidden; ``deferred`` tools appear only if
        named in ``loaded`` (see ``search_tools``)."""
        loaded_set: Set[str] = set(loaded or ())
        out = []
        with self._lock:
            for name in sorted(self._tools):
                t = self.tools.get(name)
                if t is None:
                    out.append(self._tools[name])  # schema-only legacy entry
                    continue
                if not t.available():
                    continue
                if t.deferred and name not in loaded_set:
                    continue
                if name == "search_tools" and not self.has_deferred():
                    continue  # nothing to discover → don't spend tokens on it
                out.append(t.schema())
        return out

    def get_tool_names(self) -> list[str]:
        return sorted(self._tools.keys())

    def has_deferred(self) -> bool:
        return any(t.deferred and t.available() for t in self.tools.values())

    def search(self, query: str, limit: int = 8) -> List[Tool]:
        """Keyword search over deferred (and all) tools — powers ``search_tools``."""
        terms = [w for w in re.split(r"\W+", (query or "").lower()) if w]
        scored = []
        for t in self.tools.values():
            if not t.available():
                continue
            hay = f"{t.name} {t.description} {t.toolset}".lower()
            score = sum(hay.count(w) + (3 if w in t.name.lower() else 0) for w in terms)
            if score:
                scored.append((score, t.name, t))
        scored.sort(key=lambda x: (-x[0], x[1]))
        return [t for _, _, t in scored[:limit]]

    # ── mutation / views ────────────────────────────────────────
    def disable(self, names: set[str]) -> None:
        """Drop tools by name from the schema list and executors. Mutates
        this registry — prefer ``view(deny=...)`` for per-agent filtering."""
        with self._lock:
            for n in names:
                self._tools.pop(n, None)
                self.executors.pop(n, None)
                self.tools.pop(n, None)

    def view(
        self,
        *,
        allow: Optional[Iterable[str]] = None,
        deny: Optional[Iterable[str]] = None,
        toolsets: Optional[Iterable[str]] = None,
    ) -> "ToolRegistry":
        """A filtered snapshot sharing the same Tool objects.  Does not
        touch the original — safe to hand to one agent/sub-agent."""
        allow_s = set(allow) if allow is not None else None
        deny_s = set(deny or ())
        sets = set(toolsets) if toolsets is not None else None
        v = ToolRegistry()
        with self._lock:
            for name, schema in self._tools.items():
                if allow_s is not None and name not in allow_s:
                    continue
                if name in deny_s:
                    continue
                t = self.tools.get(name)
                if sets is not None and (t is None or t.toolset not in sets):
                    continue
                v._tools[name] = schema
                if name in self.executors:
                    v.executors[name] = self.executors[name]
                if t is not None:
                    v.tools[name] = t
        return v
