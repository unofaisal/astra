"""Fireworks provider — config + reasoning strategies."""

from astra.providers.provider import make_config
from astra.providers.providers._shared import replay_omit
from astra.providers.providers._shared.effort import reasoning_openai_style

FireworksProviderConfig = make_config(
	name="fireworks",
	base_url="https://api.fireworks.ai/inference/v1",
	reasoning_strategy=reasoning_openai_style,
	reasoning_replay=replay_omit,
)
