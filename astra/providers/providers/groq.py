"""Groq provider — config + reasoning strategies."""

from astra.providers.provider import make_config
from astra.providers.providers._shared import replay_omit
from astra.providers.providers._shared.effort import reasoning_openai_style

GroqProviderConfig = make_config(
	name="groq",
	base_url="https://api.groq.com/openai/v1",
	reasoning_strategy=reasoning_openai_style,
	reasoning_replay=replay_omit,
)
