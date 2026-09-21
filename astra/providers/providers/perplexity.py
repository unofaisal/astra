"""Perplexity Sonar provider — config + reasoning strategies."""

from astra.providers.provider import make_config
from astra.providers.providers._shared import replay_omit
from astra.providers.providers._shared.effort import reasoning_openai_style

PerplexityProviderConfig = make_config(
	name="perplexity",
	base_url="https://api.perplexity.ai",
	reasoning_strategy=reasoning_openai_style,
	reasoning_replay=replay_omit,
)
