"""MCP (Model Context Protocol) as a tool *source*.

Any MCP server — in any language — becomes astra tools with zero glue
code: ``tools/list`` is turned into ``Tool`` objects and each call is
forwarded with ``tools/call``.  Built on the official ``mcp`` Python SDK
(optional extra: ``pip install astra[mcp]``); works with both the 1.x
(camelCase) and 2.x (snake_case) SDK generations.

Design points
  * One long-lived background task per server owns the transport (anyio
    cancel scopes must be entered/exited in the same task).  Calls from
    any task are multiplexed over the one session.
  * Tools are namespaced ``<server>__<tool>`` (collision-free, valid
    provider tool names).
  * **Schema pinning / rug-pull protection**: a tool whose description or
    schema changes after first connect is quarantined (removed) unless the
    server config says ``on_schema_change="warn"``; ``pin`` lets you pin
    expected schema hashes up front.  Tool descriptions are prompt-injected
    into the model, so a silent change is an attack surface.
  * Server-declared annotations (readOnlyHint…) are hints from a possibly
    untrusted party: ignored unless ``trust_annotations=True``.
  * stdio servers get a minimal environment (SDK defaults + ``env`` +
    named ``env_from_host`` variables) — never the whole host environment.
  * Per-call timeout, circuit breaker, per-server concurrency cap.
  * ``list_tools`` cache hints (``ttl_ms``) drive periodic refresh.

MCP connections are bound to the event loop that started them, so call
``await source.start(registry)`` from your app's long-lived loop (e.g. a
FastAPI lifespan) and ``await source.aclose()`` on shutdown.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .core import (
    TIMEOUT,
    TOOL_ERROR,
    UNAVAILABLE,
    Tool,
    ToolAnnotations,
    ToolContext,
    ToolError,
    ToolResult,
)

logger = logging.getLogger(__name__)


def _g(obj: Any, *names: str, default: Any = None) -> Any:
    """Attribute access tolerant of SDK camelCase vs snake_case."""
    for n in names:
        if isinstance(obj, dict):
            if n in obj:
                return obj[n]
        elif hasattr(obj, n):
            return getattr(obj, n)
    return default


@dataclass
class McpServerConfig:
    name: str
    # stdio transport
    command: Optional[str] = None
    args: List[str] = field(default_factory=list)
    env: Optional[Dict[str, str]] = None
    env_from_host: List[str] = field(default_factory=list)
    cwd: Optional[str] = None
    # streamable-HTTP transport
    url: Optional[str] = None
    headers: Optional[Dict[str, str]] = None
    # behaviour
    prefix: Optional[str] = None
    toolset: Optional[str] = None
    timeout: float = 60.0
    connect_timeout: float = 30.0
    allow: Optional[List[str]] = None
    deny: List[str] = field(default_factory=list)
    pin: Optional[Dict[str, str]] = None  # remote tool name -> expected schema hash
    on_schema_change: str = "block"  # block | warn
    trust_annotations: bool = False
    deferred: bool = False
    max_concurrency: Optional[int] = None
    refresh_seconds: Optional[float] = None  # None → follow server ttl_ms (min 60s), 0 → never


def _sanitize(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_\-]", "_", name)


def namespaced(prefix: str, tool: str) -> str:
    full = f"{_sanitize(prefix)}__{_sanitize(tool)}"
    if len(full) > 64:
        digest = hashlib.sha1(full.encode()).hexdigest()[:8]
        full = full[:55] + "_" + digest
    return full


def schema_digest(name: str, description: str, schema: Any) -> str:
    import json

    blob = json.dumps({"n": name, "d": description or "", "s": schema}, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


class _Connection:
    """One MCP server: a background task owns the transport + session."""

    def __init__(self, cfg: McpServerConfig) -> None:
        self.cfg = cfg
        self.session: Any = None
        self._task: Optional[asyncio.Task] = None
        self._ready: Optional[asyncio.Future] = None
        self._stop: Optional[asyncio.Event] = None
        self.last_error: Optional[str] = None

    @property
    def connected(self) -> bool:
        return self.session is not None and self._task is not None and not self._task.done()

    async def connect(self) -> None:
        loop = asyncio.get_running_loop()
        self._ready = loop.create_future()
        self._stop = asyncio.Event()
        self._task = loop.create_task(self._run(), name=f"astra-mcp-{self.cfg.name}")
        try:
            await asyncio.wait_for(asyncio.shield(self._ready), self.cfg.connect_timeout)
        except BaseException:
            await self.close()
            raise

    async def _run(self) -> None:
        try:
            try:
                from mcp import ClientSession
            except ImportError as exc:  # pragma: no cover
                raise RuntimeError("MCP support requires the 'mcp' package (pip install astra[mcp])") from exc
            async with self._transport() as streams:
                read, write = streams[0], streams[1]
                async with ClientSession(read, write) as session:
                    init = getattr(session, "initialize", None) or getattr(session, "discover")
                    await init()
                    self.session = session
                    if self._ready and not self._ready.done():
                        self._ready.set_result(True)
                    await self._stop.wait()  # type: ignore[union-attr]
        except BaseException as exc:  # noqa: BLE001 - surface anything to connect()
            self.last_error = f"{type(exc).__name__}: {exc}"
            if self._ready and not self._ready.done():
                self._ready.set_exception(exc if isinstance(exc, Exception) else RuntimeError(str(exc)))
            if not isinstance(exc, (asyncio.CancelledError, Exception)):
                raise
        finally:
            self.session = None

    def _transport(self):
        cfg = self.cfg
        if cfg.command:
            from mcp import StdioServerParameters
            from mcp.client.stdio import stdio_client

            env = dict(cfg.env or {})
            for name in cfg.env_from_host:
                if name in os.environ:
                    env[name] = os.environ[name]
            params = StdioServerParameters(command=cfg.command, args=list(cfg.args), env=env or None, cwd=cfg.cwd)
            return stdio_client(params)
        if cfg.url:
            try:  # SDK 2.x
                from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

                client = create_mcp_http_client(headers=cfg.headers) if cfg.headers else None
                return streamable_http_client(cfg.url, http_client=client) if client else streamable_http_client(cfg.url)
            except ImportError:  # SDK 1.x
                from mcp.client.streamable_http import streamablehttp_client  # type: ignore

                return streamablehttp_client(cfg.url, headers=cfg.headers)
        raise ValueError(f"MCP server '{cfg.name}' needs either 'command' (stdio) or 'url' (HTTP)")

    async def close(self) -> None:
        if self._stop is not None:
            self._stop.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(self._task), 5.0)
            except BaseException:
                self._task.cancel()
                try:
                    await self._task
                except BaseException:
                    pass
        self.session = None

    async def list_tools(self) -> tuple[list, Optional[int]]:
        tools: list = []
        cursor = None
        ttl = None
        while True:
            params = None
            if cursor:
                try:
                    from mcp import types as mt

                    params = mt.PaginatedRequestParams(cursor=cursor)
                except Exception:
                    params = None
            res = await self.session.list_tools(params=params) if params is not None else await self.session.list_tools()
            tools += list(_g(res, "tools", default=[]))
            ttl = _g(res, "ttl_ms", "ttlMs", default=ttl)
            cursor = _g(res, "next_cursor", "nextCursor")
            if not cursor:
                return tools, ttl

    async def call(self, name: str, args: Dict[str, Any], timeout: Optional[float]) -> Any:
        if not self.connected:
            raise ConnectionError("MCP server not connected")
        kwargs: Dict[str, Any] = {}
        if timeout is not None:
            kwargs["read_timeout_seconds"] = timeout
            try:
                from datetime import timedelta  # 1.x expects a timedelta

                import inspect as _i

                ann = str(_i.signature(self.session.call_tool).parameters["read_timeout_seconds"].annotation)
                if "timedelta" in ann:
                    kwargs["read_timeout_seconds"] = timedelta(seconds=timeout)
            except Exception:
                pass
        return await self.session.call_tool(name, args, **kwargs)


def _result_to_content(res: Any) -> Any:
    structured = _g(res, "structured_content", "structuredContent")
    blocks = _g(res, "content", default=[]) or []
    texts: List[str] = []
    for b in blocks:
        btype = _g(b, "type")
        if btype == "text":
            texts.append(str(_g(b, "text", default="")))
        elif btype == "image":
            texts.append(f"[image: {_g(b, 'mime_type', 'mimeType', default='image')}, not shown]")
        elif btype == "resource":
            res_ = _g(b, "resource")
            txt = _g(res_, "text")
            texts.append(str(txt) if txt else f"[resource: {_g(res_, 'uri', default='?')}]")
        elif btype == "resource_link":
            texts.append(f"[resource link: {_g(b, 'uri', default='?')}]")
        else:
            texts.append(f"[{btype or 'content'}]")
    text = "\n".join(t for t in texts if t)
    if structured is not None and (not text or text.strip().startswith(("{", "["))):
        return structured
    return text


class McpHandler:
    def __init__(self, conn: _Connection, remote_name: str, source: "McpSource") -> None:
        self.conn = conn
        self.remote_name = remote_name
        self.source = source

    async def __call__(self, args: Dict[str, Any], ctx: ToolContext) -> Any:
        timeout = self.conn.cfg.timeout
        remaining = ctx.remaining()
        if remaining is not None:
            timeout = min(timeout, max(0.1, remaining))
        try:
            res = await self.conn.call(self.remote_name, args, timeout)
        except asyncio.CancelledError:
            raise
        except ConnectionError as exc:
            await self.source._maybe_reconnect(self.conn)
            raise ToolError(f"MCP server '{self.conn.cfg.name}' is not connected: {exc}", error_type=UNAVAILABLE, retryable=True)
        except Exception as exc:  # noqa: BLE001
            msg = str(exc)
            low = msg.lower()
            if "timed out" in low or isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
                raise ToolError(f"MCP call '{self.remote_name}' timed out after {timeout:.0f}s", error_type=TIMEOUT, retryable=True)
            if "closed" in low or "broken" in low or type(exc).__name__ in ("ClosedResourceError", "BrokenResourceError", "EndOfStream"):
                await self.source._maybe_reconnect(self.conn)
                raise ToolError(f"MCP server '{self.conn.cfg.name}' connection lost: {msg}", error_type=UNAVAILABLE, retryable=True)
            raise ToolError(f"MCP error: {msg}", error_type=TOOL_ERROR)
        if _g(res, "is_error", "isError", default=False):
            raise ToolError(str(_result_to_content(res)) or "MCP tool reported an error", error_type=TOOL_ERROR)
        if _g(res, "result_type") == "input_required":  # pragma: no cover - SDK 2.x MRTR
            raise ToolError("The MCP server asked for interactive input, which astra does not support.", error_type=TOOL_ERROR)
        return ToolResult.success(_result_to_content(res))


class McpSource:
    def __init__(self, servers: List[McpServerConfig]) -> None:
        self.configs = {c.name: c for c in servers}
        self.conns: Dict[str, _Connection] = {}
        self.registry: Any = None
        self.baseline: Dict[str, str] = {}  # namespaced name -> digest
        self.quarantined: Dict[str, str] = {}  # namespaced name -> reason
        self.errors: Dict[str, str] = {}
        self._refresh_tasks: Dict[str, asyncio.Task] = {}
        self._reconnecting: set = set()

    # ── lifecycle ───────────────────────────────────────────────
    async def start(self, registry: Any) -> List[str]:
        """Connect to every configured server and register its tools.
        A failing server is recorded in ``self.errors`` and skipped — it
        never prevents the others (or the app) from starting."""
        self.registry = registry
        names: List[str] = []
        results = await asyncio.gather(*(self._start_one(c) for c in self.configs.values()), return_exceptions=True)
        for cfg, res in zip(self.configs.values(), results):
            if isinstance(res, Exception):
                self.errors[cfg.name] = f"{type(res).__name__}: {res}"
                logger.error("MCP server '%s' failed to start: %s", cfg.name, res)
            else:
                names += res
        return names

    async def _start_one(self, cfg: McpServerConfig) -> List[str]:
        conn = _Connection(cfg)
        await conn.connect()
        self.conns[cfg.name] = conn
        names = await self._sync_tools(cfg, conn, first=True)
        if cfg.refresh_seconds != 0:
            self._refresh_tasks[cfg.name] = asyncio.get_running_loop().create_task(self._refresh_loop(cfg, conn))
        return names

    async def aclose(self) -> None:
        for t in self._refresh_tasks.values():
            t.cancel()
        for t in list(self._refresh_tasks.values()):
            try:
                await t
            except BaseException:
                pass
        self._refresh_tasks.clear()
        await asyncio.gather(*(c.close() for c in self.conns.values()), return_exceptions=True)
        self.conns.clear()

    # ── tool sync ───────────────────────────────────────────────
    async def _sync_tools(self, cfg: McpServerConfig, conn: _Connection, *, first: bool = False) -> List[str]:
        remote, ttl = await conn.list_tools()
        prefix = cfg.prefix or cfg.name
        seen: set = set()
        registered: List[str] = []
        for rt in remote:
            rname = _g(rt, "name")
            if cfg.allow is not None and rname not in cfg.allow:
                continue
            if rname in cfg.deny:
                continue
            desc = _g(rt, "description", default="") or ""
            schema = _g(rt, "input_schema", "inputSchema", default={}) or {"type": "object", "properties": {}}
            full = namespaced(prefix, rname)
            seen.add(full)
            digest = schema_digest(rname, desc, schema)

            if cfg.pin is not None and cfg.pin.get(rname) not in (None, digest):
                self._quarantine(full, f"schema hash does not match the pinned value for '{rname}'")
                continue
            prev = self.baseline.get(full)
            if prev is not None and prev != digest:
                if cfg.on_schema_change == "block":
                    self._quarantine(full, "tool description/schema changed since first connect (possible rug-pull)")
                    continue
                logger.warning("MCP tool %s changed its schema/description", full)
            self.baseline[full] = digest
            self.quarantined.pop(full, None)

            ann = _g(rt, "annotations")
            annotations = ToolAnnotations(open_world=True)
            if cfg.trust_annotations and ann is not None:
                annotations = ToolAnnotations(
                    read_only=bool(_g(ann, "read_only_hint", "readOnlyHint", default=False)),
                    destructive=bool(_g(ann, "destructive_hint", "destructiveHint", default=False)),
                    idempotent=bool(_g(ann, "idempotent_hint", "idempotentHint", default=False)),
                    open_world=bool(_g(ann, "open_world_hint", "openWorldHint", default=True)),
                )
            tool = Tool(
                name=full,
                description=(desc or f"{rname} (via MCP server '{cfg.name}')").strip(),
                parameters=schema,
                handler=McpHandler(conn, rname, self),
                toolset=cfg.toolset or cfg.name,
                annotations=annotations,
                timeout=cfg.timeout + 5,
                max_concurrency=cfg.max_concurrency,
                deferred=cfg.deferred,
                breaker=True,
                source="mcp",
                meta={"server": cfg.name, "remote_name": rname, "schema_hash": digest},
            )
            self.registry.register(tool, replace=True)
            registered.append(full)

        # tools that disappeared from the server
        for name in [n for n, t in list(self.registry.tools.items()) if t.source == "mcp" and t.meta.get("server") == cfg.name and n not in seen]:
            self.registry.unregister(name)
        conn.ttl_ms = ttl  # type: ignore[attr-defined]
        return registered

    def _quarantine(self, full: str, reason: str) -> None:
        self.quarantined[full] = reason
        self.registry.unregister(full)
        logger.error("MCP tool %s quarantined: %s", full, reason)

    async def refresh(self, server: Optional[str] = None) -> None:
        for name, conn in self.conns.items():
            if server and name != server:
                continue
            await self._sync_tools(self.configs[name], conn)

    async def _refresh_loop(self, cfg: McpServerConfig, conn: _Connection) -> None:
        while True:
            ttl = getattr(conn, "ttl_ms", None)
            delay = cfg.refresh_seconds if cfg.refresh_seconds else max(60.0, (ttl or 300_000) / 1000.0)
            await asyncio.sleep(delay)
            try:
                if conn.connected:
                    await self._sync_tools(cfg, conn)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning("MCP refresh for '%s' failed: %s", cfg.name, exc)

    async def _maybe_reconnect(self, conn: _Connection) -> None:
        name = conn.cfg.name
        if conn.connected or name in self._reconnecting:
            return
        self._reconnecting.add(name)
        try:
            await conn.close()
            await conn.connect()
            await self._sync_tools(conn.cfg, conn)
            logger.info("MCP server '%s' reconnected", name)
        except Exception as exc:  # noqa: BLE001
            logger.warning("MCP reconnect for '%s' failed: %s", name, exc)
        finally:
            self._reconnecting.discard(name)

    def status(self) -> Dict[str, Any]:
        return {
            "servers": {n: {"connected": c.connected, "last_error": c.last_error} for n, c in self.conns.items()},
            "errors": dict(self.errors),
            "quarantined": dict(self.quarantined),
        }
