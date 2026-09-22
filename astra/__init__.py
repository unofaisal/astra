"""astra — a general-purpose, storage/framework-agnostic agent harness.

Ported from a Frappe-native agent harness. Every piece that used to touch
Frappe (doctypes, Redis, realtime pub/sub, ERPNext-specific tools) has been
replaced with a small pluggable protocol so the harness can run anywhere:
a script, a FastAPI app, a Slack bot, a CLI, tests.

Core building blocks:
  - astra.config       — plain dataclass configuration (provider/model/etc.)
  - astra.providers    — LLM provider registry + OpenAI-compatible client
  - astra.storage       — pluggable session persistence (Store protocol)
  - astra.signals       — pluggable stop-flags / loop windows / pause context
  - astra.events        — callback-based event emission (no forced pub/sub)
  - astra.prompts       — pluggable system-prompt assembly (markdown by default)
  - astra.tools          — file-based tool loader/registry/executor
  - astra.agent          — Conversation (session/history) + Agent (turn loop)
"""

__version__ = "0.1.0"

from .client import Astra, AstraSettings, create_astra  # noqa: E402

__all__ = ["Astra", "AstraSettings", "create_astra"]
