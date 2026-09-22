"""Plain configuration objects.

Replaces the Frappe "Agent Setup" singleton doctype. Nothing here reads
from a database — the caller builds an AgentConfig however they like
(env vars, a YAML file, a settings object, hardcoded) and passes it in.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import os


@dataclass
class ProviderCredentials:
    """Connection info for a single LLM provider.

    provider: key into astra.providers.registry.PROVIDERS (e.g. "openai",
        "anthropic", "openrouter", "groq", ...).
    api_key: the API key for that provider.
    base_url_override: optional override of the provider's default
        base_url (useful for self-hosted / vLLM / proxy setups).
    """

    provider: str = "openrouter"
    api_key: str | None = None
    base_url_override: str | None = None

    @classmethod
    def from_env(cls, prefix: str = "ASTRA_") -> "ProviderCredentials":
        return cls(
            provider=os.environ.get(f"{prefix}PROVIDER", "openrouter"),
            api_key=os.environ.get(f"{prefix}API_KEY"),
            base_url_override=os.environ.get(f"{prefix}BASE_URL"),
        )


@dataclass
class AgentConfig:
    """Everything Agent/OpenAIProvider used to pull from the Agent Setup
    singleton, now just a plain object the caller constructs.
    """

    credentials: ProviderCredentials = field(default_factory=ProviderCredentials)
    model: str = "gpt-4o-mini"
    reasoning_effort: str | None = None  # None | "none" | "low" | "medium" | "high" | ...
    max_turns: int = 40
    max_retries: int = 2
    max_context_chars: int = 1_000_000
    supports_vision: bool = False
    pdf_engine: str = "mistral-ocr"  # only meaningful for provider == "openrouter"

    # ── LLM transport / resilience (astra owns retries: the SDK's own are off) ──
    request_timeout: float = 300.0  # read/write timeout per request (seconds)
    connect_timeout: float = 10.0
    sdk_max_retries: int = 0  # keep 0: astra's retry loop below honours Retry-After and budgets
    max_backoff: float = 30.0  # cap for exponential backoff / Retry-After
    honor_retry_after: bool = True
    llm_max_concurrency: int | None = None  # cap concurrent requests per endpoint+key (None = unlimited)
    llm_queue_timeout: float = 30.0  # max wait for a concurrency slot
    llm_breaker_threshold: int = 8  # consecutive transient failures before the circuit opens
    llm_breaker_cooldown: float = 20.0

    @classmethod
    def from_env(cls, prefix: str = "ASTRA_") -> "AgentConfig":
        return cls(
            credentials=ProviderCredentials.from_env(prefix),
            model=os.environ.get(f"{prefix}MODEL", "gpt-4o-mini"),
            reasoning_effort=os.environ.get(f"{prefix}REASONING_EFFORT") or None,
            max_turns=int(os.environ.get(f"{prefix}MAX_TURNS", "40")),
        )
