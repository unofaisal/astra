"""ToolRuntime — the shared, process-wide execution environment for tools.

Owned by ``Astra`` (or a process-wide default) and injected into every
``ToolExecutor``.  One runtime is shared by ALL agents (including delegate
sub-agents), which is what makes limits real: concurrency caps live here,
not on the per-agent executor.

Responsibilities
  * admission control — global / per-tenant / per-tool concurrency caps
    with a bounded wait queue (overload → a typed ``busy`` result, not an
    unbounded pile-up or an OOM);
  * a separate cap on concurrently running child *processes*;
  * per-tool circuit breakers (subprocess/MCP tools that keep failing get
    short-circuited instead of hammered);
  * the runner (local / sandboxed / remote) used by process tools;
  * secrets resolution for process tools (explicit names only);
  * output policy (default cap, optional spill-to-file artifacts);
  * shutdown: kill every tracked child process.

All limits are loop-agnostic (see ``astra.concurrency``), so this works
from FastAPI, ``asyncio.run()`` per request, or multiple threads.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Callable, Dict, Mapping, Optional

from ..concurrency import CircuitBreaker, KeyedLimiters, Limiter, LimiterBusy
from ..telemetry import metrics
from .process import Limits, ProcessTracker
from .sandbox import LocalRunner, Runner, Sandbox, default_sandbox

logger = logging.getLogger(__name__)


class ToolRuntime:
    def __init__(
        self,
        *,
        max_concurrent_tools: int = 512,
        max_concurrent_processes: int = 32,
        per_tenant_limit: Optional[int] = None,
        default_tool_concurrency: Optional[int] = None,
        queue_timeout: float = 15.0,
        max_queue: int = 2000,
        default_timeout: float = 120.0,
        max_output_chars: int = 60_000,
        max_retries: int = 2,
        artifact_dir: Optional[str] = None,
        workspace: Optional[str] = None,
        runner: Optional[Runner] = None,
        sandbox: Optional[Sandbox] = None,
        require_sandbox: bool = False,
        services: Optional[Dict[str, Any]] = None,
        secrets: Optional[Any] = None,  # Mapping[str,str] | Callable[[str, ctx], str|None]
        sync_tools_in_thread: bool = False,
        thread_pool_size: int = 32,
        breaker_threshold: int = 5,
        breaker_cooldown: float = 30.0,
        validate_args: bool = True,
        process_limits: Optional[Limits] = None,
        policy: Optional[Callable[..., Any]] = None,
    ) -> None:
        self.tool_limiter = Limiter(max_concurrent_tools, max_queue=max_queue, name="tools")
        self.process_limiter = Limiter(max_concurrent_processes, max_queue=max_queue, name="processes")
        self.tenant_limiters = KeyedLimiters(per_tenant_limit or 1, max_queue=max_queue, name="tenant") if per_tenant_limit else None
        self.per_tenant_limit = per_tenant_limit
        self._tool_limiters = KeyedLimiters(default_tool_concurrency or 1, max_queue=max_queue, name="tool")
        self.default_tool_concurrency = default_tool_concurrency
        self.queue_timeout = queue_timeout
        self.default_timeout = default_timeout
        self.max_output_chars = max_output_chars
        self.max_retries = max_retries
        self.artifact_dir = artifact_dir
        self.workspace = workspace
        self.services: Dict[str, Any] = dict(services or {})
        self.secrets = secrets
        self.sync_tools_in_thread = sync_tools_in_thread
        self._thread_pool_size = thread_pool_size
        self._pool: Optional[ThreadPoolExecutor] = None
        self._pool_lock = threading.Lock()
        self.breaker_threshold = breaker_threshold
        self.breaker_cooldown = breaker_cooldown
        self._breakers: Dict[str, CircuitBreaker] = {}
        self._breaker_lock = threading.Lock()
        self.validate_args = validate_args
        self.process_limits = process_limits
        self.policy = policy
        self.tracker = ProcessTracker()
        self.require_sandbox = require_sandbox
        self.runner: Runner = runner or LocalRunner(sandbox if sandbox is not None else default_sandbox(), require_sandbox=require_sandbox)
        self._closed = False
        self._run_end_hooks: list = []

    # ── run lifecycle hooks (used by delegate background tasks) ──
    def add_run_end_hook(self, fn: Callable[[str, str], Any]) -> None:
        """Register ``fn(session_id, status)`` — called when an Agent run ends."""
        if fn not in self._run_end_hooks:
            self._run_end_hooks.append(fn)

    def run_ended(self, session_id: str, status: str) -> None:
        for fn in list(self._run_end_hooks):
            try:
                fn(session_id, status)
            except Exception:
                logger.exception("run-end hook raised")

    # ── admission control ────────────────────────────────────────
    @asynccontextmanager
    async def admit(self, tool: Any, ctx: Any) -> AsyncIterator[float]:
        """Acquire tenant → tool → global slots (fixed order: no deadlock).
        Yields the queue wait in seconds.  Raises LimiterBusy on overload."""
        held: list[Limiter] = []
        t0 = time.monotonic()
        try:
            if self.tenant_limiters is not None and getattr(ctx, "tenant", None):
                lim = self.tenant_limiters.get(ctx.tenant, self.per_tenant_limit)
                await lim.acquire(self.queue_timeout)
                held.append(lim)
            cap = getattr(tool, "max_concurrency", None) or self.default_tool_concurrency
            if cap:
                lim = self._tool_limiters.get(tool.name, cap)
                await lim.acquire(self.queue_timeout)
                held.append(lim)
            await self.tool_limiter.acquire(self.queue_timeout)
            held.append(self.tool_limiter)
        except BaseException:
            for lim in reversed(held):
                lim.release()
            raise
        waited = time.monotonic() - t0
        metrics.observe("astra_tool_queue_wait_seconds", waited, tool=tool.name)
        metrics.gauge_add("astra_tools_in_flight", 1)
        try:
            yield waited
        finally:
            metrics.gauge_add("astra_tools_in_flight", -1)
            for lim in reversed(held):
                lim.release()

    # ── circuit breakers ─────────────────────────────────────────
    def breaker_for(self, tool: Any) -> Optional[CircuitBreaker]:
        if not getattr(tool, "breaker", False):
            return None
        with self._breaker_lock:
            br = self._breakers.get(tool.name)
            if br is None:
                br = self._breakers[tool.name] = CircuitBreaker(self.breaker_threshold, self.breaker_cooldown)
            return br

    # ── runners / secrets / pool ─────────────────────────────────
    def runner_for(self, mode: str = "auto") -> Optional[Runner]:
        """Pick a runner for a process tool.  ``None`` → refuse to run."""
        if mode == "none":
            if self.require_sandbox:
                return None
            return LocalRunner(None)
        if mode == "required":
            return self.runner if getattr(self.runner, "isolated", False) else None
        # auto
        if self.require_sandbox and not getattr(self.runner, "isolated", False):
            return None
        return self.runner

    def get_secret(self, name: str, ctx: Any = None) -> Optional[str]:
        s = self.secrets
        if s is None:
            return os.environ.get(name)
        if callable(s):
            return s(name, ctx)
        return s.get(name) if isinstance(s, Mapping) else None

    def thread_pool(self) -> ThreadPoolExecutor:
        with self._pool_lock:
            if self._pool is None:
                self._pool = ThreadPoolExecutor(max_workers=self._thread_pool_size, thread_name_prefix="astra-tool")
            return self._pool

    # ── artifacts ────────────────────────────────────────────────
    def spill(self, text: str, tool_name: str, call_id: Optional[str]) -> Optional[str]:
        if not self.artifact_dir:
            return None
        try:
            os.makedirs(self.artifact_dir, exist_ok=True)
            safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in f"{tool_name}-{call_id or int(time.time()*1000)}")
            path = os.path.join(self.artifact_dir, f"{safe}.txt")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(text)
            return path
        except OSError:
            logger.exception("could not spill tool output")
            return None

    # ── lifecycle / diagnostics ──────────────────────────────────
    def shutdown(self) -> int:
        """Kill tracked child processes and stop the thread pool."""
        self._closed = True
        killed = self.tracker.kill_all()
        with self._pool_lock:
            if self._pool is not None:
                self._pool.shutdown(wait=False, cancel_futures=True)
                self._pool = None
        return killed

    def stats(self) -> Dict[str, Any]:
        return {
            "tools": {"active": self.tool_limiter.active, "waiting": self.tool_limiter.waiting, "limit": self.tool_limiter.limit},
            "processes": {"active": self.process_limiter.active, "waiting": self.process_limiter.waiting, "limit": self.process_limiter.limit, "tracked": len(self.tracker)},
            "tenants": self.tenant_limiters.snapshot() if self.tenant_limiters else {},
            "breakers": {k: v.state for k, v in self._breakers.items()},
            "sandbox": bool(getattr(self.runner, "isolated", False)),
        }


_default_runtime: Optional[ToolRuntime] = None
_default_lock = threading.Lock()


def get_default_runtime() -> ToolRuntime:
    global _default_runtime
    with _default_lock:
        if _default_runtime is None:
            _default_runtime = ToolRuntime()
        return _default_runtime


def set_default_runtime(runtime: Optional[ToolRuntime]) -> None:
    global _default_runtime
    with _default_lock:
        _default_runtime = runtime
