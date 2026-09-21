#!/usr/bin/env python3
"""A ready-to-run terminal chat client, built entirely on the public
Astra SDK (astra.create_astra) — no internal astra modules touched
directly. This is the reference for "how a real app wires astra up."

Usage:
    export ASTRA_PROVIDER=openai        # or anthropic / openrouter / groq / ...
    export ASTRA_API_KEY=sk-...
    export ASTRA_MODEL=gpt-4o-mini
    python examples/cli.py

Type a message and press enter. Ctrl+C or "exit" to quit.
If the agent pauses to ask a clarifying question, you'll be prompted
for an answer right in the terminal and the run continues automatically.
"""

from __future__ import annotations

import asyncio
import os
import sys

from astra import create_astra
from astra.events import Callbacks

SESSIONS_DB = "astra_cli_sessions.db"

from model_catalog import MODEL_CHOICES  # noqa: E402 — shared with examples/api/main.py


def pick_provider() -> str:
    default = os.environ.get("ASTRA_PROVIDER", "openai")
    choices = sorted(MODEL_CHOICES.keys())
    print("Providers:")
    for i, name in enumerate(choices, 1):
        marker = " (default)" if name == default else ""
        print(f"  {i}. {name}{marker}")
    raw = input(f"pick a provider [{default}] > ").strip()
    if not raw:
        return default
    if raw.isdigit() and 1 <= int(raw) <= len(choices):
        return choices[int(raw) - 1]
    return raw  # allow typing a provider name not in the curated list


def pick_model(provider: str) -> str:
    default = os.environ.get("ASTRA_MODEL") or (MODEL_CHOICES.get(provider, [None])[0])
    choices = MODEL_CHOICES.get(provider, [])
    if choices:
        print(f"Models for {provider}:")
        for i, name in enumerate(choices, 1):
            marker = " (default)" if name == default else ""
            print(f"  {i}. {name}{marker}")
        print("  (or type any other model id this provider serves)")
    raw = input(f"pick a model [{default}] > ").strip()
    if not raw:
        if not default:
            print("No default available — a model id is required.")
            return pick_model(provider)
        return default
    if raw.isdigit() and choices and 1 <= int(raw) <= len(choices):
        return choices[int(raw) - 1]
    return raw  # custom model id


def build_callbacks() -> Callbacks:
    """Stream tokens to stdout as they arrive, and print tool activity
    so the terminal shows what the agent is doing, not just its final
    answer."""

    async def on_token(delta: str, **_):
        print(delta, end="", flush=True)

    async def on_tool_start(tool_name: str, args, **_):
        print(f"\n  \033[2m→ {tool_name}({args})\033[0m", flush=True)

    async def on_tool_result(tool_name: str, is_error: bool, **_):
        marker = "✗" if is_error else "✓"
        print(f"  \033[2m{marker} {tool_name} done\033[0m", flush=True)

    async def on_error(error_text: str, **_):
        print(f"\n\033[31m[error] {error_text}\033[0m")

    return Callbacks(
        on_token=on_token,
        on_tool_start=on_tool_start,
        on_tool_result=on_tool_result,
        on_error=on_error,
    )


async def handle_clarification(app, result) -> "AgentResult":
    """If the agent paused to ask a question, surface it and loop until
    it's resolved (the model might pause again on the very next turn —
    handle that too, rather than assuming one round-trip is enough)."""
    while result.status == "clarification_pending":
        print()  # the question itself was already printed by on_event/on_clarification_request
        answer = input("your answer > ").strip()
        if not answer:
            print("(an answer is required to continue)")
            continue
        result = await app.resume(result.session_id, result.clarification_id, answer)
    return result


async def main() -> None:
    api_key = os.environ.get("ASTRA_API_KEY")
    if not api_key:
        print("Set ASTRA_API_KEY before running (see this file's module docstring).", file=sys.stderr)
        sys.exit(1)

    provider = pick_provider()
    model = pick_model(provider)

    from astra.storage import SQLiteStore

    callbacks = build_callbacks()

    async def on_clarification_request(question, options, **_):
        print(f"\n\033[33m[clarification needed]\033[0m {question}")
        if options:
            for i, opt in enumerate(options, 1):
                print(f"  {i}. {opt}")

    callbacks.on_clarification_request = on_clarification_request

    app = create_astra(
        provider=provider,
        api_key=api_key,
        model=model,
        store=SQLiteStore(SESSIONS_DB),
        tools_dir="astra/tools",
        system_prompt="You are a helpful, concise assistant running in a terminal.",
        callbacks=callbacks,
    )

    print(f"\nastra CLI — provider={provider} model={model} store={SESSIONS_DB}")
    print("Type a message, '/model <name>' to switch models, or 'exit' to quit.\n")

    session_id: str | None = None

    while True:
        try:
            user_input = input("you > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nbye!")
            break

        if not user_input:
            continue
        if user_input.lower() in ("exit", "quit"):
            print("bye!")
            break

        if user_input.startswith("/model"):
            parts = user_input.split(maxsplit=1)
            if len(parts) == 2:
                app.config.model = parts[1].strip()
                print(f"(switched to model: {app.config.model})")
            else:
                print(f"(current model: {app.config.model}) — usage: /model <model-id>")
            continue

        print("astra > ", end="", flush=True)
        result = await app.chat(user_input, session_id=session_id)
        result = await handle_clarification(app, result)
        session_id = result.session_id  # remember it so the next turn continues this conversation
        print()  # newline after the streamed response

        if result.status == "error":
            print(f"\033[31m(run ended with an error: {result.error})\033[0m")
        elif result.status == "stopped":
            print("\033[33m(run was stopped)\033[0m")
        elif result.status == "max_turns":
            print("\033[33m(hit the max-turns limit)\033[0m")


if __name__ == "__main__":
    asyncio.run(main())
