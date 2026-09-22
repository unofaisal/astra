# astra/providers/openai_api.py
"""Thin async wrapper around any OpenAI-compatible chat completions endpoint.

Production concerns (unchanged from the original harness):
  - Exponential backoff + jitter on transient errors (429, 5xx, network).
  - Hard stop on permanent errors (400, 401, 403, 404, 422).
  - finish_reason guard: stops if the model hits max_tokens mid-thought.
  - Parallel tool calling: models can return multiple tool calls per
    turn; returned as a list for the caller to execute concurrently.
  - Token streaming: reassembles delta chunks (content + tool_calls)
    into the same shape generate() returns, so callers don't need two
    code paths.
  - stream_options safety: only sent to providers known to support it.

The only thing that changed from the original: configuration comes from
a plain astra.config.AgentConfig instead of a Frappe "Agent Setup"
singleton, and errors are logged via the stdlib `logging` module instead
of frappe.log_error.
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Callable
from typing import Any, Optional

from openai import APIConnectionError, APIStatusError, AsyncOpenAI

from ..agent_types.messages import Message, MessageList
from ..config import AgentConfig
from .registry import Provider, get_provider

logger = logging.getLogger(__name__)

_MAX_RETRIES = 3
_BASE_BACKOFF = 1.0  # seconds
_PERMANENT_STATUS = {400, 401, 403, 404, 422}

# Keys that belong to the standard OpenAI message shape. Anything else
# is provider-specific reasoning/thinking metadata.
_STANDARD_MESSAGE_KEYS = {
    "role",
    "content",
    "tool_calls",
    "refusal",
    "function_call",
    "audio",
    "name",
}

TokenCallback = Optional[Callable[[str], Any]]


# ── Helpers ────────────────────────────────────────────────────────


def _standardize_message(raw_dict: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Split a raw message dict into (standard_message, reasoning_meta)."""
    reasoning_meta = {k: v for k, v in raw_dict.items() if k not in _STANDARD_MESSAGE_KEYS}
    message_dict = {k: v for k, v in raw_dict.items() if k in _STANDARD_MESSAGE_KEYS}
    return message_dict, reasoning_meta


def _enforce_int(value: Any) -> int | None:
    return int(value) if value is not None else None


def _has_file_part(messages: MessageList) -> bool:
    """True if any message's content-parts array includes a `file` part
    (a PDF attachment — see providers.attachments.build_content_parts)."""
    for m in messages:
        content = m.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if isinstance(part, dict) and part.get("type") == "file":
                return True
    return False


# ── Main client ────────────────────────────────────────────────────


class OpenAIProvider:
    """Holds a single provider connection and exposes generate()."""

    def __init__(
        self,
        config: AgentConfig,
        model_override: str | None = None,
        reasoning_effort_override: str | None = None,
    ) -> None:
        """
        config: the caller's AgentConfig (provider/model/api key/etc).
        model_override: model id to use instead of config.model. Provider
            is still always the one in config.credentials.provider — a
            model_override only makes sense for a model that provider
            can actually serve.
        reasoning_effort_override: tri-state override of config.reasoning_effort —
            None: use config's value. "none": explicitly disable. any
            other string: use it, provider validates the vocabulary.
        """
        provider: Provider = get_provider(config.credentials.provider)
        self.provider: Provider = provider
        self.model: str = model_override or config.model

        if reasoning_effort_override is not None:
            effort = reasoning_effort_override.strip().lower()
        else:
            effort = (config.reasoning_effort or "").strip().lower()
        self.reasoning_effort: str | None = effort or None

        self.pdf_engine: str = (config.pdf_engine or "mistral-ocr").strip().lower()

        base_url = config.credentials.base_url_override or provider.base_url
        self.client = AsyncOpenAI(api_key=config.credentials.api_key, base_url=base_url)

    # ── Public API ─────────────────────────────────────────────────

    def replay_reasoning(
        self,
        meta: dict[str, Any] | None,
        had_tool_calls: bool,
        msg: dict[str, Any],
    ) -> None:
        """Attach reasoning metadata to an assistant turn for replay.
        Called by Conversation.get_messages() when rebuilding history."""
        if self.provider.reasoning_replay is not None:
            self.provider.reasoning_replay(meta, had_tool_calls, msg, self.model)

    async def generate(
        self,
        messages: MessageList,
        tools: list[dict[str, Any]] | None = None,
        on_token: TokenCallback = None,
        on_reasoning: TokenCallback = None,
    ) -> tuple[Message, Any]:
        """Send a chat completion request.

        Returns (message_dict, tool_calls). If on_token/on_reasoning are
        provided the request is streamed; the return shape is identical
        either way — streaming is a side channel for live UI updates only.
        """
        kwargs = self._build_request_kwargs(messages, tools)

        last_exc: Exception | None = None
        for attempt in range(_MAX_RETRIES):
            try:
                is_streaming = on_token is not None or on_reasoning is not None
                if is_streaming:
                    return await self._generate_streaming(kwargs, on_token, on_reasoning)
                return await self._generate_once(kwargs)

            except APIStatusError as exc:
                if exc.status_code in _PERMANENT_STATUS:
                    logger.error("Permanent provider error %s — not retrying: %s", exc.status_code, exc.message)
                    raise
                last_exc = exc
                logger.warning(
                    "Transient provider error %s (attempt %d/%d): %s",
                    exc.status_code, attempt + 1, _MAX_RETRIES, exc.message,
                )

            except APIConnectionError as exc:
                last_exc = exc
                logger.warning("Provider connection error (attempt %d/%d): %s", attempt + 1, _MAX_RETRIES, exc)

            if attempt < _MAX_RETRIES - 1:
                delay = _BASE_BACKOFF * (2**attempt) + random.uniform(0, 0.5)
                await asyncio.sleep(delay)

        raise RuntimeError(f"Provider failed after {_MAX_RETRIES} attempts.") from last_exc

    # ── Request construction ───────────────────────────────────────

    def _build_request_kwargs(
        self,
        messages: MessageList,
        tools: list[dict[str, Any]] | None,
    ) -> dict[str, Any]:
        p = self.provider
        kwargs: dict[str, Any] = {"model": self.model, "messages": messages}

        if not p.supports_sampling_params:
            for key in ("temperature", "top_p", "presence_penalty", "frequency_penalty", "logit_bias", "logprobs"):
                kwargs.pop(key, None)

        if "max_tokens" in kwargs:
            kwargs["max_tokens"] = _enforce_int(kwargs["max_tokens"])
            if p.max_tokens_param != "max_tokens":
                kwargs[p.max_tokens_param] = kwargs.pop("max_tokens")

        if self.reasoning_effort and p.reasoning_strategy is not None:
            p.reasoning_strategy(self.reasoning_effort, kwargs, self.model)
        elif self.reasoning_effort:
            logger.warning("No reasoning strategy for provider %s — reasoning_effort=%r will not be sent.", p.name, self.reasoning_effort)

        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"

            should_disable_parallel = not p.supports_parallel_tool_calls or (
                p.disables_parallel_for_minimal_reasoning and kwargs.get("reasoning_effort") == "minimal"
            )
            if should_disable_parallel:
                kwargs["parallel_tool_calls"] = False

        if p.name == "openrouter" and _has_file_part(messages):
            kwargs["extra_body"] = {
                **kwargs.get("extra_body", {}),
                "plugins": [{"id": "file-parser", "pdf": {"engine": self.pdf_engine}}],
            }

        return kwargs

    # ── Non-streaming path ─────────────────────────────────────────

    async def _generate_once(self, kwargs: dict[str, Any]) -> tuple[Message, Any]:
        response = await self.client.chat.completions.create(**kwargs)
        choice = response.choices[0]

        if choice.finish_reason == "length":
            raise RuntimeError(
                "Provider returned finish_reason='length' — the model hit max_tokens "
                "before completing. Increase max_tokens or shorten the context."
            )

        raw_dict = choice.message.model_dump(exclude_none=True)
        message_dict, reasoning_meta = _standardize_message(raw_dict)

        reasoning_text = raw_dict.get("reasoning") or raw_dict.get("reasoning_content")
        if reasoning_text:
            message_dict["reasoning"] = reasoning_text
        if reasoning_meta:
            message_dict["reasoning_meta"] = reasoning_meta

        message_dict["model"] = getattr(response, "model", None) or self.model
        usage = getattr(response, "usage", None)
        if usage is not None:
            message_dict["usage"] = usage.model_dump(exclude_none=True)

        return message_dict, getattr(choice.message, "tool_calls", None)

    # ── Streaming path ─────────────────────────────────────────────

    async def _generate_streaming(
        self,
        kwargs: dict[str, Any],
        on_token: Callable[[str], Any] | None,
        on_reasoning: Callable[[str], Any] | None = None,
    ) -> tuple[Message, Any]:
        stream_kwargs: dict[str, Any] = {**kwargs, "stream": True}
        if self.provider.supports_stream_options:
            stream_kwargs["stream_options"] = {"include_usage": True}

        stream = await self.client.chat.completions.create(**stream_kwargs)

        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        usage_dict: dict[str, Any] | None = None
        resolved_model: str | None = None
        reasoning_meta_parts: dict[str, Any] = {}
        tool_call_frags: dict[int, dict[str, Any]] = {}
        finish_reason: str | None = None
        role = "assistant"

        async for chunk in stream:
            if getattr(chunk, "model", None):
                resolved_model = chunk.model

            chunk_usage = getattr(chunk, "usage", None)
            if chunk_usage is not None:
                usage_dict = chunk_usage.model_dump(exclude_none=True)

            if not chunk.choices:
                continue

            choice = chunk.choices[0]
            delta = choice.delta

            if choice.finish_reason:
                finish_reason = choice.finish_reason
            if delta is None:
                continue
            if getattr(delta, "role", None):
                role = delta.role

            reasoning_delta = getattr(delta, "reasoning", None) or getattr(delta, "reasoning_content", None)
            if reasoning_delta:
                reasoning_parts.append(reasoning_delta)
                if on_reasoning is not None:
                    result = on_reasoning(reasoning_delta)
                    if asyncio.iscoroutine(result):
                        await result

            details = getattr(delta, "reasoning_details", None)
            if details:
                reasoning_meta_parts["reasoning_details"] = details
            signature = getattr(delta, "signature", None) or getattr(delta, "thought_signature", None)
            if signature:
                reasoning_meta_parts["signature"] = signature

            if delta.content:
                content_parts.append(delta.content)
                if on_token is not None:
                    result = on_token(delta.content)
                    if asyncio.iscoroutine(result):
                        await result

            if getattr(delta, "tool_calls", None):
                for tc_delta in delta.tool_calls:
                    idx = tc_delta.index
                    frag = tool_call_frags.setdefault(
                        idx, {"id": None, "type": "function", "function": {"name": "", "arguments": ""}}
                    )
                    if tc_delta.id:
                        frag["id"] = tc_delta.id
                    if tc_delta.function:
                        if tc_delta.function.name:
                            frag["function"]["name"] += tc_delta.function.name
                        if tc_delta.function.arguments:
                            frag["function"]["arguments"] += tc_delta.function.arguments

        if not finish_reason and (content_parts or reasoning_parts):
            raise RuntimeError(
                "Stream ended without finish_reason — the model likely hit max_tokens "
                "before completing. Increase max_tokens or shorten the context."
            )

        if finish_reason == "length":
            raise RuntimeError(
                "Provider returned finish_reason='length' — the model hit max_tokens "
                "before completing. Increase max_tokens or shorten the context."
            )

        content = "".join(content_parts)
        reasoning = "".join(reasoning_parts)

        tool_calls = None
        if tool_call_frags:
            ordered = [tool_call_frags[i] for i in sorted(tool_call_frags)]
            tool_calls = [_DeltaToolCall(t) for t in ordered]

        message_dict: dict[str, Any] = {"role": role, "content": content or None}

        if reasoning:
            message_dict["reasoning"] = reasoning

        reasoning_meta = dict(reasoning_meta_parts)
        if reasoning and "reasoning_content" not in reasoning_meta:
            reasoning_meta["reasoning_content"] = reasoning
        if reasoning_meta:
            message_dict["reasoning_meta"] = reasoning_meta

        if tool_call_frags:
            message_dict["tool_calls"] = [tool_call_frags[i].copy() for i in sorted(tool_call_frags)]

        message_dict["model"] = resolved_model or self.model
        if usage_dict is not None:
            message_dict["usage"] = usage_dict

        message_dict = {k: v for k, v in message_dict.items() if v is not None}
        return message_dict, tool_calls


# ── Streamed tool-call shim ────────────────────────────────────────


class _DeltaToolCall:
    """Exposes the same .id / .function.name / .function.arguments
    attribute access as the OpenAI SDK's tool call objects, without
    pulling in the full pydantic model."""

    class _Function:
        def __init__(self, d: dict[str, Any]) -> None:
            self.name: str = d.get("name", "")
            self.arguments: str = d.get("arguments", "")

    def __init__(self, d: dict[str, Any]) -> None:
        self.id: str | None = d.get("id")
        self.type: str = d.get("type", "function")
        self.function = self._Function(d.get("function", {}))
