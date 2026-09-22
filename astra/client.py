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
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .agent.agent import AgentResult
from .agent.conversation import Conversation
from .agent.runner import (
    build_agent,
    recover_interrupted_session,
    resume_after_clarification,
    run_turn,
)
from .config import AgentConfig, ProviderCredentials
from .events import Callbacks
from .prompts import PromptBuilder
from .signals import InMemorySignalStore, SignalStore, request_stop
from .storage import Session, SQLiteStore, Store, gen_id
from .tools.decorator import ToolRegistry
from .tools.loader import load_tools

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
            load_tools(self.registry, settings.tools_dir)

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
                )
            )

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

    def get_session(self, session_id: str) -> Session | None:
        return self.store.get(session_id)

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
    ) -> AgentResult:
        """Send a message and run the agent loop to completion (or a
        pause/stop/error). Pass the same session_id back in to continue
        an existing conversation; omit it to start a new one — the id
        of whichever session was used is always available on the
        returned AgentResult.session_id."""
        conversation = Conversation(
            store=self.store,
            session_id=session_id,
            user=user,
            signals=self.signals,
            callbacks=callbacks or self.default_callbacks,
            pricing_lookup=self.settings.pricing_lookup,
            attachments_builder=self.settings.attachments_builder,
        )
        result = await run_turn(
            conversation,
            self.registry,
            self.config,
            message,
            attachments=attachments,
            system_prompt=self._resolve_system_prompt(system_prompt),
            model_override=model_override,
            max_turns=max_turns,
        )
        return result

    async def resume(
        self,
        session_id: str,
        clarification_id: str,
        answer: str,
        *,
        callbacks: Callbacks | None = None,
        system_prompt: str | None = None,
        max_turns: int | None = None,
    ) -> AgentResult:
        """Answer a paused request_clarification tool call and continue
        the run from where it left off."""
        result = await resume_after_clarification(
            self.store,
            self.registry,
            self.config,
            session_id,
            clarification_id,
            answer,
            signals=self.signals,
            callbacks=callbacks or self.default_callbacks,
            pricing_lookup=self.settings.pricing_lookup,
            attachments_builder=self.settings.attachments_builder,
            system_prompt=self._resolve_system_prompt(system_prompt),
            max_turns=max_turns,
        )
        return result

    async def recover(
        self,
        session_id: str,
        *,
        callbacks: Callbacks | None = None,
        system_prompt: str | None = None,
        max_turns: int | None = None,
    ) -> AgentResult | None:
        """If session_id was left with dangling pending tool calls
        (process crash mid-run), synthesize error results for them and
        continue. Returns None if there was nothing to recover."""
        result = await recover_interrupted_session(
            self.store,
            self.registry,
            self.config,
            session_id,
            signals=self.signals,
            callbacks=callbacks or self.default_callbacks,
            system_prompt=self._resolve_system_prompt(system_prompt),
            max_turns=max_turns,
        )
        return result

    async def regenerate(
        self,
        session_id: str,
        *,
        callbacks: Callbacks | None = None,
        system_prompt: str | None = None,
        model_override: str | None = None,
        max_turns: int | None = None,
    ) -> AgentResult:
        """Discard the most recent assistant turn and run again from the
        preceding user message. Raises ValueError if the session has no
        assistant turn to regenerate (empty, or mid-clarification)."""
        conversation = Conversation(
            store=self.store,
            session_id=session_id,
            signals=self.signals,
            callbacks=callbacks or self.default_callbacks,
            pricing_lookup=self.settings.pricing_lookup,
            attachments_builder=self.settings.attachments_builder,
        )
        if not conversation.truncate_last_assistant_turn():
            raise ValueError(f"Session '{session_id}' has no assistant turn to regenerate.")

        agent = build_agent(
            conversation,
            self.registry,
            self.config,
            system_prompt=self._resolve_system_prompt(system_prompt),
            model_override=model_override,
            max_turns=max_turns,
        )
        result = await agent.run()
        return result

    def stop(self, session_id: str) -> None:
        """Cooperative stop — checked at the next loop/tool-dispatch
        boundary inside Agent.run(), not an immediate kill."""
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
    on_clarification_request: Any = None,
    on_done: Any = None,
    on_error: Any = None,
    on_event: Any = None,
    callbacks: Callbacks | None = None,  # or hand in a fully-built Callbacks() directly instead
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
    field this shortcut doesn't expose.
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
    )
    return Astra(settings)
