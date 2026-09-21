# tests/test_api.py
"""Tests for examples/api/main.py — exercises the actual FastAPI app
through TestClient, with OpenAIProvider.generate monkeypatched so no
real network calls happen.
"""
import json
import os

import pytest

os.environ.setdefault("ASTRA_API_KEY", "fake")


@pytest.fixture
def client(tmp_path, monkeypatch):
    # Fresh DB per test, and a fresh Astra instance so tests don't share
    # session state with each other.
    monkeypatch.setenv("ASTRA_DB_PATH", str(tmp_path / "test_api.db"))

    import importlib

    import examples.api.main as api_main

    importlib.reload(api_main)  # rebuild astra_app against the fresh DB path

    from fastapi.testclient import TestClient

    return TestClient(api_main.app), api_main


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


def test_health(client):
    tc, _ = client
    r = tc.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_chat_basic(client, monkeypatch):
    tc, _ = client
    _patch_provider(monkeypatch, content="hello from api")
    r = tc.post("/chat", json={"message": "hi"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "done"
    assert body["final_text"] == "hello from api"
    assert body["session_id"]


def test_chat_continues_session(client, monkeypatch):
    tc, _ = client
    _patch_provider(monkeypatch, content="reply")
    r1 = tc.post("/chat", json={"message": "first"})
    session_id = r1.json()["session_id"]
    r2 = tc.post("/chat", json={"message": "second", "session_id": session_id})
    assert r2.json()["session_id"] == session_id

    r3 = tc.get(f"/sessions/{session_id}")
    assert r3.status_code == 200
    assert len(r3.json()["messages"]) == 4


def test_get_unknown_session_404(client):
    tc, _ = client
    r = tc.get("/sessions/does-not-exist")
    assert r.status_code == 404


def test_list_sessions(client, monkeypatch):
    tc, _ = client
    _patch_provider(monkeypatch)
    tc.post("/chat", json={"message": "one"})
    tc.post("/chat", json={"message": "two"})
    r = tc.get("/sessions")
    assert r.status_code == 200
    assert len(r.json()) == 2


def test_stop_endpoint_does_not_error(client):
    tc, _ = client
    r = tc.post("/sessions/some-session-id/stop")
    assert r.status_code == 200
    assert r.json()["status"] == "stop requested"


def test_resume_unknown_clarification_returns_404(client):
    tc, _ = client
    r = tc.post(
        "/chat/resume",
        json={"session_id": "does-not-exist", "clarification_id": "clar_x", "answer": "yes"},
    )
    assert r.status_code == 404


def test_resume_full_flow(client, monkeypatch):
    tc, api_main = client
    from astra.providers.openai_api import OpenAIProvider

    call_n = {"n": 0}

    async def scripted_generate(self, messages, tools=None, on_token=None, on_reasoning=None):
        call_n["n"] += 1
        if call_n["n"] == 1:
            args = json.dumps({"question": "Which order?", "options": ["#1", "#2"]})
            msg = {
                "role": "assistant", "content": "", "model": "fake",
                "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "request_clarification", "arguments": args}}],
            }

            class _Fn:
                name = "request_clarification"
                arguments = args

            class _TC:
                id = "c1"
                function = _Fn()

            return msg, [_TC()]
        return {"role": "assistant", "content": "Cancelled order #1.", "model": "fake"}, None

    monkeypatch.setattr(OpenAIProvider, "generate", scripted_generate)

    r1 = tc.post("/chat", json={"message": "cancel my order"})
    body1 = r1.json()
    assert body1["status"] == "clarification_pending"
    assert body1["clarification_id"]

    r2 = tc.post(
        "/chat/resume",
        json={"session_id": body1["session_id"], "clarification_id": body1["clarification_id"], "answer": "#1"},
    )
    body2 = r2.json()
    assert body2["status"] == "done"
    assert body2["final_text"] == "Cancelled order #1."


def test_stream_endpoint_emits_token_and_result_events(client, monkeypatch):
    tc, _ = client
    _patch_provider(monkeypatch, content="streamed hello")

    with tc.stream("POST", "/chat/stream", json={"message": "hi"}) as response:
        assert response.status_code == 200
        raw = "".join(response.iter_text())

    # Parse the SSE stream: pairs of "event: X\ndata: {...}\n\n"
    events = []
    for block in raw.strip().split("\n\n"):
        if not block.strip():
            continue
        lines = block.splitlines()
        event_type = lines[0].removeprefix("event: ")
        data = json.loads(lines[1].removeprefix("data: "))
        events.append((event_type, data))

    types = [e[0] for e in events]
    assert "token" in types
    assert "result" in types
    result_event = [d for t, d in events if t == "result"][0]
    assert result_event["status"] == "done"
    assert result_event["final_text"] == "streamed hello"


def test_stream_endpoint_emits_clarification_event(client, monkeypatch):
    tc, _ = client
    from astra.providers.openai_api import OpenAIProvider

    async def clarify_generate(self, messages, tools=None, on_token=None, on_reasoning=None):
        args = json.dumps({"question": "Refund how?", "options": ["card", "credit"]})
        msg = {
            "role": "assistant", "content": "", "model": "fake",
            "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "request_clarification", "arguments": args}}],
        }

        class _Fn:
            name = "request_clarification"
            arguments = args

        class _TC:
            id = "c1"
            function = _Fn()

        return msg, [_TC()]

    monkeypatch.setattr(OpenAIProvider, "generate", clarify_generate)

    with tc.stream("POST", "/chat/stream", json={"message": "refund me"}) as response:
        raw = "".join(response.iter_text())

    events = []
    for block in raw.strip().split("\n\n"):
        if not block.strip():
            continue
        lines = block.splitlines()
        event_type = lines[0].removeprefix("event: ")
        data = json.loads(lines[1].removeprefix("data: "))
        events.append((event_type, data))

    types = [e[0] for e in events]
    assert "clarification_request" in types
    clar_event = [d for t, d in events if t == "clarification_request"][0]
    assert clar_event["question"] == "Refund how?"
    result_event = [d for t, d in events if t == "result"][0]
    assert result_event["status"] == "clarification_pending"


def test_providers_endpoint(client):
    tc, _ = client
    r = tc.get("/providers")
    assert r.status_code == 200
    body = r.json()
    assert "openai" in body
    assert isinstance(body["openai"], list) and len(body["openai"]) > 0


def test_config_get(client):
    tc, _ = client
    r = tc.get("/config")
    assert r.status_code == 200
    body = r.json()
    assert body["provider"] == "openai"
    assert body["api_key_set"] is True
    assert body["api_key_suffix"] == "fake"  # os.environ ASTRA_API_KEY="fake" from module setdefault


def test_config_update_takes_effect_on_next_call(client, monkeypatch):
    tc, api_main = client

    from astra.providers.openai_api import OpenAIProvider

    seen_models = []

    async def fake_generate(self, messages, tools=None, on_token=None, on_reasoning=None):
        seen_models.append(self.model)
        return {"role": "assistant", "content": "ok", "model": self.model}, None

    monkeypatch.setattr(OpenAIProvider, "generate", fake_generate)

    r = tc.post("/config", json={"model": "gpt-4o-super"})
    assert r.status_code == 200
    assert r.json()["model"] == "gpt-4o-super"

    tc.post("/chat", json={"message": "hi"})
    assert seen_models == ["gpt-4o-super"]


def test_config_update_blank_api_key_keeps_current(client):
    tc, _ = client
    r1 = tc.get("/config")
    suffix_before = r1.json()["api_key_suffix"]

    r2 = tc.post("/config", json={"model": "some-model"})  # api_key omitted
    assert r2.json()["api_key_suffix"] == suffix_before  # unchanged


def test_chat_with_per_request_model_override(client, monkeypatch):
    tc, _ = client
    from astra.providers.openai_api import OpenAIProvider

    seen_models = []

    async def fake_generate(self, messages, tools=None, on_token=None, on_reasoning=None):
        seen_models.append(self.model)
        return {"role": "assistant", "content": "ok", "model": self.model}, None

    monkeypatch.setattr(OpenAIProvider, "generate", fake_generate)

    tc.post("/chat", json={"message": "hi", "model": "one-off-model"})
    tc.post("/chat", json={"message": "hi again"})  # no override this time

    assert seen_models[0] == "one-off-model"
    assert seen_models[1] != "one-off-model"  # falls back to the process default, unaffected by the prior override


def test_regenerate_replaces_last_assistant_turn(client, monkeypatch):
    tc, _ = client
    from astra.providers.openai_api import OpenAIProvider

    responses = iter(["first answer", "regenerated answer"])

    async def fake_generate(self, messages, tools=None, on_token=None, on_reasoning=None):
        return {"role": "assistant", "content": next(responses), "model": self.model}, None

    monkeypatch.setattr(OpenAIProvider, "generate", fake_generate)

    r1 = tc.post("/chat", json={"message": "hi"})
    session_id = r1.json()["session_id"]
    assert r1.json()["final_text"] == "first answer"

    r2 = tc.post(f"/sessions/{session_id}/regenerate", json={})
    assert r2.status_code == 200
    assert r2.json()["final_text"] == "regenerated answer"

    session = tc.get(f"/sessions/{session_id}").json()
    # still exactly 1 user + 1 assistant message — the old assistant turn was replaced, not appended
    assert len(session["messages"]) == 2
    assert session["messages"][-1]["content"] == "regenerated answer"


def test_regenerate_on_session_with_no_assistant_turn_404s(client):
    tc, _ = client
    # create a session with only... actually there's no way to create a
    # session with zero assistant turns through this API without a chat
    # call completing, so simulate against a made-up id instead.
    r = tc.post("/sessions/does-not-exist/regenerate", json={})
    assert r.status_code == 404


def test_regenerate_stream_emits_result_event(client, monkeypatch):
    tc, _ = client
    from astra.providers.openai_api import OpenAIProvider

    responses = iter(["first", "regenerated via stream"])

    async def fake_generate(self, messages, tools=None, on_token=None, on_reasoning=None):
        content = next(responses)
        if on_token:
            import asyncio

            r = on_token(content)
            if asyncio.iscoroutine(r):
                await r
        return {"role": "assistant", "content": content, "model": self.model}, None

    monkeypatch.setattr(OpenAIProvider, "generate", fake_generate)

    r1 = tc.post("/chat", json={"message": "hi"})
    session_id = r1.json()["session_id"]

    with tc.stream("POST", f"/sessions/{session_id}/regenerate/stream", json={}) as response:
        raw = "".join(response.iter_text())

    events = []
    for block in raw.strip().split("\n\n"):
        if not block.strip():
            continue
        lines = block.splitlines()
        event_type = lines[0].removeprefix("event: ")
        data = json.loads(lines[1].removeprefix("data: "))
        events.append((event_type, data))

    result_event = [d for t, d in events if t == "result"][0]
    assert result_event["status"] == "done"
    assert result_event["final_text"] == "regenerated via stream"


def test_delete_session(client, monkeypatch):
    tc, _ = client
    _patch_provider(monkeypatch)
    r1 = tc.post("/chat", json={"message": "hi"})
    session_id = r1.json()["session_id"]

    r2 = tc.delete(f"/sessions/{session_id}")
    assert r2.status_code == 200

    r3 = tc.get(f"/sessions/{session_id}")
    assert r3.status_code == 404


def test_cors_headers_present(client):
    tc, _ = client
    r = tc.get("/health", headers={"Origin": "http://localhost:5173"})
    assert r.headers.get("access-control-allow-origin") in ("*", "http://localhost:5173")


def test_resume_stream_emits_result_event(client, monkeypatch):
    tc, _ = client
    from astra.providers.openai_api import OpenAIProvider

    call_n = {"n": 0}

    async def scripted_generate(self, messages, tools=None, on_token=None, on_reasoning=None):
        call_n["n"] += 1
        if call_n["n"] == 1:
            args = json.dumps({"question": "Which order?"})
            msg = {
                "role": "assistant", "content": "", "model": "fake",
                "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "request_clarification", "arguments": args}}],
            }

            class _Fn:
                name = "request_clarification"
                arguments = args

            class _TC:
                id = "c1"
                function = _Fn()

            return msg, [_TC()]
        content = "resolved via stream"
        if on_token:
            import asyncio

            r = on_token(content)
            if asyncio.iscoroutine(r):
                await r
        return {"role": "assistant", "content": content, "model": "fake"}, None

    monkeypatch.setattr(OpenAIProvider, "generate", scripted_generate)

    r1 = tc.post("/chat", json={"message": "cancel my order"})
    body1 = r1.json()
    assert body1["status"] == "clarification_pending"

    with tc.stream(
        "POST",
        "/chat/resume/stream",
        json={"session_id": body1["session_id"], "clarification_id": body1["clarification_id"], "answer": "#1"},
    ) as response:
        raw = "".join(response.iter_text())

    events = []
    for block in raw.strip().split("\n\n"):
        if not block.strip():
            continue
        lines = block.splitlines()
        event_type = lines[0].removeprefix("event: ")
        data = json.loads(lines[1].removeprefix("data: "))
        events.append((event_type, data))

    types = [e[0] for e in events]
    assert "token" in types
    result_event = [d for t, d in events if t == "result"][0]
    assert result_event["status"] == "done"
    assert result_event["final_text"] == "resolved via stream"
