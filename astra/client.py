# astra/client.py
"""The single entry point for using astra as a library.

    from astra import create_astra

    app = create_astra(provider="openai", api_key="sk-...", model="gpt-4o-mini")
    result = await app.chat("hello")
    print(result.final_text)

Everything else in the package (Conversation, Agent, Store, Callbacks,
...) is still there and still directly usable if you want lower-level
control — Astra is a thin, opinionated wrapper around them for the
common case: "configure once, then call .chat()/.resume()/.stop()
per session without re-wiring Conversation/Agent by hand every time."

Astra also owns the *shared, process-wide* pieces that make many
concurrent users safe and cheap: one ToolRuntime (concurrency caps,
sandbox runner), one ProviderPool (shared HTTP clients, circuit
breakers), a per-session run guard, quotas, and graceful shutdown.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Callable

from .agent.agent import AgentResult
from .agent.conversation import Conversation
from .agent.runner import (
    build_agent,
    recover_interrupted_session,
    resume_after_clarification,
    run_turn,
)
from .config import AgentConfig, ProviderCredentials
from .context import ContextManager, ContextPolicy
from .events import Callbacks
from .prompts import PromptBuilder
from .providers.pool import ProviderPool, get_default_pool
from .quota import Quota
from .sessions import SessionGuard, check_owner
from .signals import InMemorySignalStore, SignalStore, request_stop
from .storage import Session, SQLiteStore, Store, gen_id
from .telemetry import metrics
from .tools.core import Tool
from .tools.decorator import ToolRegistry
from .tools.loader import load_tools
from .tools.runtime import ToolRuntime

logger = logging.getLogger(__name__)

_BUILTIN_TOOLS_DIR = Path(__file__).parent / "tools"

PromptSource = "str | Callable[[], str | None] | PromptBuilder | None"


@dataclass
class AstraSettings:
    """Everything Astra needs, gathered in one place.

    Only `agent_config` is required — everything else has a working
    default. Override what your app actually needs; leave the rest.
    """

    agent_config: AgentConfig

    # Persistence / control-signal backends. Defaults are zero-setup:
    # a local SQLite file and an in-process signal store.
    store: Store | None = None
    signals: SignalStore | None = None

    # Tools. tools_dir is scanned once at startup (see astra.tools.loader).
    # Pass None to skip auto-loading and wire tools onto `registry` yourself.
    tools_dir: str | Path | None = "astra/tools"
    skills_dir: str | Path | None = None  # wires skill_tools to a markdown directory

    # System prompt: a plain string, a zero-arg callable returning a
    # string (resolved fresh on every .chat() call — use this for
    # per-request/dynamic prompts), or a PromptBuilder.
    system_prompt: Any = None

    # Default event hooks used for every session unless a call passes
    # its own `callbacks=` override.
    callbacks: Callbacks | None = None

    # Optional: name -> sub-agent spec resolver, wired into delegate_task
    # automatically if provided (see astra.tools.delegate_tools.delegate).
    delegate_resolver: Callable[[str], Any] | None = None

    # Optional: model -> pricing dict, and (text, attachments, allow_images) -> content parts.
    pricing_lookup: Any = None
    attachments_builder: Any = None

    # ── Scale, safety & capability options (all optional / additive) ────
    # Tools: extra directories (python groups and/or tool.json manifests),
    # ready-made Tool objects, and MCP servers.
    extra_tools_dirs: list = field(default_factory=list)
    tools: list = field(default_factory=list)  # list[astra.tools.core.Tool]
    mcp_servers: list = field(default_factory=list)  # list[astra.tools.mcp_source.McpServerConfig]
    tool_runtime: ToolRuntime | None = None  # shared limits/sandbox; a default is built if omitted
    workspace: str | None = None  # default working directory for process tools
    tool_policy: Callable[..., Any] | None = None  # (tool, args, ctx) -> True | "deny reason"
    max_parallel_tool_calls: int = 8
    run_timeout: float | None = None  # wall-clock cap per run (seconds)

    # LLM transport: shared clients/gates (default: process-wide pool).
    provider_pool: ProviderPool | None = None
    # Per-request provider selection: (user, tenant) -> AgentConfig | None
    # (bring-your-own-key, per-tenant model routing). None -> the default config.
    config_resolver: Callable[[str, str | None], AgentConfig | None] | None = None

    # Context management & budgets.
    context_policy: ContextPolicy | None = None
    context_summarizer: Callable[..., Any] | None = None
    quota: Quota | None = None

    # Multi-user safety.
    enforce_ownership: bool = False  # require the caller to own an existing session
    session_lock: bool = True  # one run per session at a time (SessionBusyError otherwise)
    session_lock_ttl: float = 900.0
    session_lock_wait: float = 0.0  # seconds to wait for a busy session before failing

    # Shutdown.
    shutdown_timeout: float = 30.0


class Astra:
    """Construct once (e.g. at process startup), reuse across every
    session/request. Holds the shared Store/ToolRegistry/SignalStore —
    the expensive, one-time setup — so per-call methods stay cheap.
    """

    def __init__(self, settings: AstraSettings) -> None:
        self.settings = settings
        self.config: AgentConfig = settings.agent_config
        self.store: Store = settings.store or SQLiteStore("astra_sessions.db")
        self.signals: SignalStore = settings.signals or InMemorySignalStore()
        self.default_callbacks: Callbacks = settings.callbacks or Callbacks()

        self.registry = ToolRegistry()
        if settings.tools_dir:
            load_tools(self.registry, self._resolve_tools_dir(settings.tools_dir))
        for extra in settings.extra_tools_dirs:
            load_tools(self.registry, extra)
        if settings.tools:
            self.registry.extend(settings.tools, replace=True)

        # Shared execution environment for ALL agents/sub-agents of this app.
        self.runtime: ToolRuntime = settings.tool_runtime or ToolRuntime(workspace=settings.workspace)
        if settings.workspace and not self.runtime.workspace:
            self.runtime.workspace = settings.workspace
        self.pool: ProviderPool = settings.provider_pool or get_default_pool()
        self.guard = SessionGuard(self.signals if settings.session_lock else None, ttl=settings.session_lock_ttl)
        self.context_manager: ContextManager = ContextManager(
            settings.context_policy or ContextPolicy(max_chars=self.config.max_context_chars),
            summarizer=settings.context_summarizer,
        )
        self.mcp: Any = None
        if settings.mcp_servers:
            from .tools.mcp_source import McpSource

            self.mcp = McpSource(list(settings.mcp_servers))

        self._closing = False
        self._active = 0
        self._active_lock = threading.Lock()

        if settings.skills_dir:
            from .tools.skill_tools.skills import configure_skills_dir

            configure_skills_dir(settings.skills_dir)

        if settings.delegate_resolver:
            from .tools.delegate_tools.delegate import DelegateContext, configure_delegate

            configure_delegate(
                DelegateContext(
                    resolve_agent=settings.delegate_resolver,
                    store=self.store,
                    config=self.config,
                    registry=self.registry,
                    signals=self.signals,
                    runtime=self.runtime,
                    pool=self.pool,
                    policy=settings.tool_policy,
                    quota=settings.quota,
                    context_manager_factory=lambda: self.context_manager,
                )
            )

    @staticmethod
    def _resolve_tools_dir(tools_dir: str | Path) -> Path:
        """The default "astra/tools" is relative to the CWD, which crashed
        when run from anywhere else — fall back to the tools bundled with
        the installed package."""
        p = Path(tools_dir)
        if p.is_dir():
            return p
        if str(tools_dir).replace("\\", "/").rstrip("/") == "astra/tools":
            return _BUILTIN_TOOLS_DIR
        return p  # let load_tools raise a clear error for a truly bad path

    # ── Lifecycle ─────────────────────────────────────────────────

    async def start(self) -> list[str]:
        """Connect configured MCP servers and register their tools. Call
        from your app's long-lived event loop (e.g. a FastAPI lifespan).
        Safe to skip if you configured no MCP servers."""
        if self.mcp is None:
            return []
        names = await self.mcp.start(self.registry)
        logger.info("MCP: registered %d tool(s); errors=%s", len(names), self.mcp.errors)
        return names

    async def __aenter__(self) -> "Astra":
        await self.start()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    async def aclose(self, timeout: float | None = None) -> None:
        """Graceful shutdown: refuse new runs, drain in-flight ones (up to
        ``timeout``), Stop whatever is left, then kill child processes and
        close MCP connections / HTTP clients / the store."""
        self._closing = True
        timeout = self.settings.shutdown_timeout if timeout is None else timeout
        deadline = time.monotonic() + timeout
        while self._active > 0 and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        if self._active > 0:
            logger.warning("aclose(): %d run(s) still active after %.0fs — requesting stop", self._active, timeout)
            for sid in self.guard.held_sessions():
                request_stop(self.signals, sid)
            grace = time.monotonic() + 5.0
            while self._active > 0 and time.monotonic() < grace:
                await asyncio.sleep(0.05)
        self.runtime.shutdown()
        if self.mcp is not None:
            await self.mcp.aclose()
        await self.pool.aclose()
        close = getattr(self.store, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                logger.exception("store.close() failed")

    @asynccontextmanager
    async def _run_scope(self, session_id: str | None) -> AsyncIterator[None]:
        """Reject work while shutting down, count in-flight runs, and (for an
        existing session) serialize runs on that session."""
        if self._closing:
            raise RuntimeError("Astra is shutting down; not accepting new runs.")
        with self._active_lock:
            self._active += 1
        metrics.gauge_add("astra_runs_active", 1)
        try:
            if session_id and self.settings.session_lock:
                async with self.guard.hold(session_id, wait=self.settings.session_lock_wait):
                    yield
            else:
                yield
        finally:
            with self._active_lock:
                self._active -= 1
            metrics.gauge_add("astra_runs_active", -1)

    def _config_for(self, user: str, tenant: str | None) -> AgentConfig:
        resolver = self.settings.config_resolver
        if resolver is not None:
            cfg = resolver(user, tenant)
            if cfg is not None:
                return cfg
        return self.config

    def _agent_options(self) -> dict[str, Any]:
        s = self.settings
        return dict(
            pool=self.pool,
            runtime=self.runtime,
            policy=s.tool_policy,
            context_manager=self.context_manager,
            quota=s.quota,
            run_timeout=s.run_timeout,
            max_parallel_tool_calls=s.max_parallel_tool_calls,
        )

    def stats(self) -> dict[str, Any]:
        """Operational snapshot (wire it to /metrics or your dashboard)."""
        return {
            "active_runs": self._active,
            "locked_sessions": len(self.guard.held_sessions()),
            "tools": self.runtime.stats(),
            "llm": self.pool.stats(),
            "mcp": self.mcp.status() if self.mcp else None,
            "metrics": metrics.snapshot(),
        }

    # ── System prompt resolution ─────────────────────────────────

    def _resolve_system_prompt(self, override: str | None) -> str | None:
        if override is not None:
            return override
        source = self.settings.system_prompt
        if source is None:
            return None
        if isinstance(source, str):
            return source
        if isinstance(source, PromptBuilder):
            return source.build()
        if callable(source):
            return source()
        return None

    # ── Session inspection ────────────────────────────────────────

    def new_session_id(self) -> str:
        return gen_id("sess_")

    def get_session(self, session_id: str, user: str | None = None) -> Session | None:
        """With ``enforce_ownership`` the caller must pass ``user`` and own
        the session (someone else's session raises SessionOwnershipError)."""
        sess = self.store.get(session_id)
        if sess is not None and self.settings.enforce_ownership:
            check_owner(sess.user, user, session_id)
        return sess

    def list_sessions(self, user: str | None = None, limit: int = 50, offset: int = 0) -> list[Session]:
        return self.store.list_sessions(user=user, limit=limit, offset=offset)

    # ── Core API ───────────────────────────────────────────────────

    async def chat(
        self,
        message: str,
        *,
        session_id: str | None = None,
        user: str = "anonymous",
        attachments: list[dict] | None = None,
        callbacks: Callbacks | None = None,
        system_prompt: str | None = None,
        model_override: str | None = None,
        max_turns: int | None = None,
        tenant: str | None = None,
        config: AgentConfig | None = None,
    ) -> AgentResult:
        """Send a message and run the agent loop to completion (or a
        pause/stop/error). Pass the same session_id back in to continue
        an existing conversation; omit it to start a new one — the id
        of whichever session was used is always available on the
        returned AgentResult.session_id.

        tenant: multi-tenancy scope (per-tenant limits / quotas / ownership).
        config: per-call AgentConfig override (else ``config_resolver``,
            else the app default).
        Raises SessionBusyError if the session already has a run in
        progress, SessionOwnershipError (with enforce_ownership) if
        ``user`` doesn't own it."""
        async with self._run_scope(session_id):
            conversation = Conversation(
                store=self.store,
                session_id=session_id,
                user=user,
                signals=self.signals,
                callbacks=callbacks or self.default_callbacks,
                pricing_lookup=self.settings.pricing_lookup,
                attachments_builder=self.settings.attachments_builder,
                tenant=tenant,
                enforce_owner=self.settings.enforce_ownership,
            )
            return await run_turn(
                conversation,
                self.registry,
                config or self._config_for(user, tenant),
                message,
                attachments=attachments,
                system_prompt=self._resolve_system_prompt(system_prompt),
                model_override=model_override,
                max_turns=max_turns,
                **self._agent_options(),
            )

    async def resume(
        self,
        session_id: str,
        clarification_id: str,
        answer: str,
        *,
        callbacks: Callbacks | None = None,
        system_prompt: str | None = None,
        max_turns: int | None = None,
        user: str | None = None,
        tenant: str | None = None,
    ) -> AgentResult:
        """Answer a paused request_clarification tool call and continue
        the run from where it left off."""
        async with self._run_scope(session_id):
            existing = self.store.get(session_id)
            cfg_user = user or (existing.user if existing else "anonymous")
            return await resume_after_clarification(
                self.store,
                self.registry,
                self._config_for(cfg_user, tenant or (existing.tenant if existing else None)),
                session_id,
                clarification_id,
                answer,
                signals=self.signals,
                callbacks=callbacks or self.default_callbacks,
                pricing_lookup=self.settings.pricing_lookup,
                attachments_builder=self.settings.attachments_builder,
                system_prompt=self._resolve_system_prompt(system_prompt),
                max_turns=max_turns,
                user=user,
                enforce_owner=self.settings.enforce_ownership,
                **self._agent_options(),
            )

    async def recover(
        self,
        session_id: str,
        *,
        callbacks: Callbacks | None = None,
        system_prompt: str | None = None,
        max_turns: int | None = None,
        user: str | None = None,
    ) -> AgentResult | None:
        """If session_id was left with dangling pending tool calls
        (process crash mid-run), synthesize error results for them and
        continue. Returns None if there was nothing to recover."""
        async with self._run_scope(session_id):
            existing = self.store.get(session_id)
            cfg_user = user or (existing.user if existing else "anonymous")
            return await recover_interrupted_session(
                self.store,
                self.registry,
                self._config_for(cfg_user, existing.tenant if existing else None),
                session_id,
                signals=self.signals,
                callbacks=callbacks or self.default_callbacks,
                system_prompt=self._resolve_system_prompt(system_prompt),
                max_turns=max_turns,
                user=user,
                enforce_owner=self.settings.enforce_ownership,
                **self._agent_options(),
            )

    async def regenerate(
        self,
        session_id: str,
        *,
        callbacks: Callbacks | None = None,
        system_prompt: str | None = None,
        model_override: str | None = None,
        max_turns: int | None = None,
        user: str | None = None,
    ) -> AgentResult:
        """Discard the most recent assistant turn and run again from the
        preceding user message. Raises ValueError if the session has no
        assistant turn to regenerate (empty, or mid-clarification)."""
        async with self._run_scope(session_id):
            existing = self.store.get(session_id)
            conversation = Conversation(
                store=self.store,
                session_id=session_id,
                user=user or "anonymous",
                signals=self.signals,
                callbacks=callbacks or self.default_callbacks,
                pricing_lookup=self.settings.pricing_lookup,
                attachments_builder=self.settings.attachments_builder,
                enforce_owner=self.settings.enforce_ownership,
            )
            if not conversation.truncate_last_assistant_turn():
                raise ValueError(f"Session '{session_id}' has no assistant turn to regenerate.")

            cfg_user = user or (existing.user if existing else "anonymous")
            agent = build_agent(
                conversation,
                self.registry,
                self._config_for(cfg_user, existing.tenant if existing else None),
                system_prompt=self._resolve_system_prompt(system_prompt),
                model_override=model_override,
                max_turns=max_turns,
                **self._agent_options(),
            )
            return await agent.run()

    def stop(self, session_id: str, user: str | None = None) -> None:
        """Request a Stop. An in-flight LLM call or tool is cancelled within
        ``stop_poll_interval`` (default 0.5 s) — not only at the next step
        boundary. With ``enforce_ownership`` the caller must own the session."""
        if self.settings.enforce_ownership:
            sess = self.store.get(session_id)
            if sess is not None:
                check_owner(sess.user, user, session_id)
        request_stop(self.signals, session_id)

    # ── Incremental callback registration ───────────────────────────
    #
    # Alternative to passing everything as kwargs up front — attach
    # hooks any time after construction, one at a time. Each setter
    # returns the function you passed in, so it also works as a plain
    # decorator:
    #
    #     @app.on_token
    #     def _(delta, **kw):
    #         print(delta, end="")
    #
    # All of these mutate self.default_callbacks — the callbacks used
    # by any .chat()/.resume()/.recover() call that doesn't pass its
    # own callbacks= override.

    def on_token(self, fn):
        self.default_callbacks.on_token = fn
        return fn

    def on_reasoning(self, fn):
        self.default_callbacks.on_reasoning = fn
        return fn

    def on_message(self, fn):
        self.default_callbacks.on_message = fn
        return fn

    def on_tool_start(self, fn):
        self.default_callbacks.on_tool_start = fn
        return fn

    def on_tool_result(self, fn):
        self.default_callbacks.on_tool_result = fn
        return fn

    def on_tool_output(self, fn):
        self.default_callbacks.on_tool_output = fn
        return fn

    def on_clarification_request(self, fn):
        self.default_callbacks.on_clarification_request = fn
        return fn

    def on_done(self, fn):
        self.default_callbacks.on_done = fn
        return fn

    def on_error(self, fn):
        self.default_callbacks.on_error = fn
        return fn

    def on_event(self, fn):
        self.default_callbacks.on_event = fn
        return fn


def create_astra(
    *,
    provider: str = "openai",
    api_key: str | None = None,
    model: str = "gpt-4o-mini",
    base_url_override: str | None = None,
    reasoning_effort: str | None = None,
    max_turns: int = 40,
    store: Store | None = None,
    signals: SignalStore | None = None,
    tools_dir: str | Path | None = "astra/tools",
    skills_dir: str | Path | None = None,
    system_prompt: Any = None,
    delegate_resolver: Callable[[str], Any] | None = None,
    pricing_lookup: Any = None,
    attachments_builder: Any = None,
    # Every Callbacks field, individually — pass only what you need.
    # These are merged into one Callbacks() for you; you don't have to
    # build a Callbacks object yourself unless you want to.
    on_token: Any = None,
    on_reasoning: Any = None,
    on_message: Any = None,
    on_tool_start: Any = None,
    on_tool_result: Any = None,
    on_tool_output: Any = None,
    on_clarification_request: Any = None,
    on_done: Any = None,
    on_error: Any = None,
    on_event: Any = None,
    callbacks: Callbacks | None = None,  # or hand in a fully-built Callbacks() directly instead
    **settings_options: Any,  # any other AstraSettings field (tool_runtime, quota, mcp_servers, enforce_ownership, ...)
) -> Astra:
    """One-call factory — the fast path to a working Astra instance
    without building AgentConfig/AstraSettings by hand:

        from astra import create_astra
        app = create_astra(
            provider="openai", api_key="sk-...", model="gpt-4o-mini",
            on_token=lambda delta, **kw: print(delta, end=""),
        )
        result = await app.chat("hello")

    Every keyword here maps onto AstraSettings/AgentConfig — construct
    those directly (`Astra(AstraSettings(...))`) instead if you need a
    field this shortcut doesn't expose (or pass it through as an extra
    keyword: it is forwarded to AstraSettings).
    """
    agent_config = AgentConfig(
        credentials=ProviderCredentials(provider=provider, api_key=api_key, base_url_override=base_url_override),
        model=model,
        reasoning_effort=reasoning_effort,
        max_turns=max_turns,
    )

    resolved_callbacks = callbacks or Callbacks()
    for name, hook in (
        ("on_token", on_token),
        ("on_reasoning", on_reasoning),
        ("on_message", on_message),
        ("on_tool_start", on_tool_start),
        ("on_tool_result", on_tool_result),
        ("on_tool_output", on_tool_output),
        ("on_clarification_request", on_clarification_request),
        ("on_done", on_done),
        ("on_error", on_error),
        ("on_event", on_event),
    ):
        if hook is not None:
            setattr(resolved_callbacks, name, hook)

    settings = AstraSettings(
        agent_config=agent_config,
        store=store,
        signals=signals,
        tools_dir=tools_dir,
        skills_dir=skills_dir,
        system_prompt=system_prompt,
        callbacks=resolved_callbacks,
        delegate_resolver=delegate_resolver,
        pricing_lookup=pricing_lookup,
        attachments_builder=attachments_builder,
        **settings_options,
    )
    return Astra(settings)
