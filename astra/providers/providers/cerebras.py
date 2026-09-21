"""Cerebras provider — config + reasoning strategies."""

from astra.providers.provider import make_config
from astra.providers.providers._shared import replay_omit
from astra.providers.providers._shared.effort import reasoning_openai_style

CerebrasProviderConfig = make_config(
	name="cerebras",
	base_url="https://api.cerebras.ai/v1",
	reasoning_strategy=reasoning_openai_style,
	reasoning_replay=replay_omit,
)
