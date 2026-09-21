# tests/conftest.py
"""Shared fixtures for the astra test suite.

The centerpiece is FakeProvider: a drop-in replacement for
OpenAIProvider.generate that never touches the network. Tests script it
with a list of "turns" — each one either plain text or a set of tool
calls — so full multi-turn conversations (including tool calls,
clarification pauses, and delegate chains) can be tested deterministically.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any

import pytest


# ── Fake provider ────────────────────────────────────────────────────


class _FakeFunction:
    def __init__(self, name: str, arguments: str):
        self.name = name
        self.arguments = arguments


class _FakeToolCall:
    def __init__(self, id_: str, name: str, arguments: str):
        self.id = id_
        self.function = _FakeFunction(name, arguments)


@dataclass
class ScriptedTurn:
    """One entry in a FakeProvider script."""

    content: str = ""
    tool_calls: list[tuple[str, str, dict]] = field(default_factory=list)  # (call_id, tool_name, args_dict)
    raise_error: Exception | None = None
    chain_break: dict | None = None


def text_turn(content: str) -> ScriptedTurn:
    return ScriptedTurn(content=content)


def tool_turn(*calls: tuple[str, str, dict]) -> ScriptedTurn:
    """tool_turn(("call_1", "my_tool", {"x": 1}), ...)"""
    return ScriptedTurn(tool_calls=list(calls))


def clarify_turn(call_id: str, question: str, options: list[str] | None = None) -> ScriptedTurn:
    args = {"question": question}
    if options:
        args["options"] = options
    return tool_turn((call_id, "request_clarification", args))


class FakeProvider:
    """Replaces OpenAIProvider for tests. Call .script([...]) with a list
    of ScriptedTurn; each call to .generate() consumes the next one. If
    the script runs out, repeats the last turn (handy for "just keep
    calling the same tool" loop-detection tests)."""

    def __init__(self, turns: list[ScriptedTurn] | None = None, model: str = "fake-model"):
        self.model = model
        self._turns = turns or []
        self._i = 0
        self.calls: list[list[dict]] = []  # records every `messages` array passed in, for assertions

    def script(self, turns: list[ScriptedTurn]) -> None:
        self._turns = turns
        self._i = 0

    def replay_reasoning(self, meta, had_tool_calls, msg):
        return None

    async def generate(self, messages, tools=None, on_token=None, on_reasoning=None):
        self.calls.append(messages)
        if not self._turns:
            raise RuntimeError("FakeProvider.generate() called with no script — call .script([...]) first")
        turn = self._turns[min(self._i, len(self._turns) - 1)]
        self._i += 1

        if turn.raise_error is not None:
            raise turn.raise_error

        if on_token and turn.content:
            r = on_token(turn.content)
            if asyncio.iscoroutine(r):
                await r

        message_obj: dict[str, Any] = {"role": "assistant", "content": turn.content, "model": self.model}
        if turn.chain_break:
            message_obj["chain_break"] = turn.chain_break

        sdk_tool_calls = None
        if turn.tool_calls:
            message_obj["tool_calls"] = [
                {"id": cid, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}
                for cid, name, args in turn.tool_calls
            ]
            sdk_tool_calls = [_FakeToolCall(cid, name, json.dumps(args)) for cid, name, args in turn.tool_calls]

        return message_obj, sdk_tool_calls


@pytest.fixture
def fake_provider():
    return FakeProvider()
