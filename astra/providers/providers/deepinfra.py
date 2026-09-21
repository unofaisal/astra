"""DeepInfra provider — config + reasoning strategies."""

from astra.providers.provider import make_config
from astra.providers.providers._shared import replay_omit
from astra.providers.providers._shared.effort import reasoning_openai_style

DeepInfraProviderConfig = make_config(
	name="deepinfra",
	base_url="https://api.deepinfra.com/v1/openai",
	reasoning_strategy=reasoning_openai_style,
	reasoning_replay=replay_omit,
)
