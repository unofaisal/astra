"""DashScope / Alibaba Model Studio — config + reasoning strategies."""

from astra.providers.provider import make_config
from astra.providers.providers._shared import replay_omit
from astra.providers.providers._shared.effort import reasoning_openai_style

DashScopeProviderConfig = make_config(
	name="dashscope",
	base_url="https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
	reasoning_strategy=reasoning_openai_style,
	reasoning_replay=replay_omit,
)
