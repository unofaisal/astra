# tests/test_sdk.py
import pytest

from astra import Astra, AstraSettings, create_astra
from astra.config import AgentConfig, ProviderCredentials
from astra.storage import MemoryStore

from .conftest import text_turn

pytestmark = pytest.mark.asyncio


def _patch_provider(monkeypatch, content="ok"):
    from astra.providers.openai_api import OpenAIProvider

    async def fake_generate(self, messages, tools=None, on_token=None, on_reasoning=None):
        if on_token:
            r = on_token(content)
            import asyncio

            if asyncio.iscoroutine(r):
                await r
        return {"role": "assistant", "content": content, "model": self.model}, None

    monkeypatch.setattr(OpenAIProvider, "generate", fake_generate)


async def test_create_astra_basic_chat(monkeypatch):
    _patch_provider(monkeypatch, content="hello from sdk")
    app = create_astra(provider="openai", api_key="fake", model="fake-model", store=MemoryStore(), tools_dir=None)
    result = await app.chat("hi")
    assert result.status == "done"
    assert result.final_text == "hello from sdk"
    assert result.session_id is not None


async def test_chat_continues_same_session(monkeypatch):
    _patch_provider(monkeypatch, content="reply")
    store = MemoryStore()
    app = create_astra(provider="openai", api_key="fake", store=store, tools_dir=None)
    r1 = await app.chat("first")
    r2 = await app.chat("second", session_id=r1.session_id)
    assert r2.session_id == r1.session_id
    session = app.get_session(r1.session_id)
    assert len(session.messages) == 4


async def test_callback_kwargs_all_fire(monkeypatch):
    _patch_provider(monkeypatch, content="ok")
    events = []
    app = create_astra(
        provider="openai", api_key="fake", store=MemoryStore(), tools_dir=None,
        on_token=lambda delta, **kw: events.append(("token", delta)),
        on_message=lambda role, content, **kw: events.append(("message", role)),
        on_done=lambda response, **kw: events.append(("done", response)),
    )
    await app.chat("hi")
    kinds = [e[0] for e in events]
    assert "token" in kinds
    assert "message" in kinds
    assert "done" in kinds


async def test_decorator_style_registration(monkeypatch):
    _patch_provider(monkeypatch, content="ok")
    app = create_astra(provider="openai", api_key="fake", store=MemoryStore(), tools_dir=None)
    seen = []

    @app.on_done
    def _(response, **kw):
        seen.append(response)

    await app.chat("hi")
    assert seen == ["ok"]


async def test_model_switch_takes_effect_next_call(monkeypatch):
    seen_models = []
    from astra.providers.openai_api import OpenAIProvider

    async def fake_generate(self, messages, tools=None, on_token=None, on_reasoning=None):
        seen_models.append(self.model)
        return {"role": "assistant", "content": "ok", "model": self.model}, None

    monkeypatch.setattr(OpenAIProvider, "generate", fake_generate)

    app = create_astra(provider="openai", api_key="fake", model="model-a", store=MemoryStore(), tools_dir=None)
    r1 = await app.chat("hi")
    app.config.model = "model-b"
    await app.chat("hi again", session_id=r1.session_id)

    assert seen_models == ["model-a", "model-b"]


async def test_astra_settings_explicit_construction(monkeypatch):
    _patch_provider(monkeypatch, content="explicit works")
    settings = AstraSettings(
        agent_config=AgentConfig(credentials=ProviderCredentials(provider="openai", api_key="fake")),
        store=MemoryStore(),
        tools_dir=None,
    )
    app = Astra(settings)
    result = await app.chat("hi")
    assert result.final_text == "explicit works"


async def test_default_store_is_sqlite_when_unspecified(monkeypatch, tmp_path):
    _patch_provider(monkeypatch)
    import os

    os.chdir(tmp_path)
    app = create_astra(provider="openai", api_key="fake", tools_dir=None)
    from astra.storage import SQLiteStore

    assert isinstance(app.store, SQLiteStore)


async def test_system_prompt_callable_resolved_fresh(monkeypatch):
    _patch_provider(monkeypatch)
    state = {"n": 0}

    def dynamic_prompt():
        state["n"] += 1
        return f"prompt version {state['n']}"

    app = create_astra(provider="openai", api_key="fake", store=MemoryStore(), tools_dir=None, system_prompt=dynamic_prompt)
    await app.chat("hi")
    await app.chat("hi again")
    assert state["n"] == 2  # resolved fresh on every call, not cached
