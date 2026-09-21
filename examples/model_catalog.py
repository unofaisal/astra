# examples/model_catalog.py
"""A short, curated starting point per provider — not exhaustive, and
provider model catalogs change independently of astra. Shared by
examples/cli.py and examples/api/main.py so there's one source of truth
instead of two drifting copies.
"""

MODEL_CHOICES: dict[str, list[str]] = {
    "openai": ["gpt-4o", "gpt-4o-mini", "gpt-4.1", "gpt-4.1-mini", "o3-mini"],
    "anthropic": ["claude-sonnet-4-5", "claude-opus-4-5", "claude-haiku-4-5"],
    "openrouter": ["openai/gpt-4o-mini", "anthropic/claude-sonnet-4.5", "google/gemini-2.5-pro"],
    "groq": ["llama-3.3-70b-versatile", "mixtral-8x7b-32768"],
    "deepseek": ["deepseek-chat", "deepseek-reasoner"],
    "google": ["gemini-2.5-pro", "gemini-2.5-flash"],
    "xai": ["grok-4", "grok-4-fast"],
}
