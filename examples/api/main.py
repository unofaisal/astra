# examples/api/main.py
"""A ready-to-run REST API for astra, built entirely on the public SDK
(astra.create_astra) — no internal astra modules touched directly. This
is the reference for wiring astra into a real HTTP service, and the
backend for examples/frontend/.

Run it:
    export ASTRA_PROVIDER=openai ASTRA_API_KEY=sk-... ASTRA_MODEL=gpt-4o-mini
    uvicorn examples.api.main:app --reload

Scope, deliberately:
  - No auth. Every endpoint is open, including /config (which can change
    the API key and provider for the ENTIRE process). Add a real auth
    dependency (see the `current_user` stub below) before exposing this
    publicly. Session ownership IS enforced (enforce_ownership=True): once
    `current_user` returns real identities, one user cannot read, continue,
    stop or delete another user's session.
  - Multi-worker: Stop flags and the per-session run lock are shared across
    worker processes if you pass a Redis-backed SignalStore
    (astra.signals_redis.RedisSignalStore) — set ASTRA_REDIS_URL below.
    Without it, the default in-process store only covers ONE worker.
  - Two concurrent runs on the same session are rejected with HTTP 409
    (SessionBusyError) instead of silently corrupting the history.
  - SQLiteStore (WAL mode) by default. Fine for light/moderate volume;
    swap in examples/postgres_store.py's PostgresStore (or your own Store)
    for real multi-process load.
  - GET /metrics exposes astra_app.stats() (tool/LLM/limiter state,
    counters, latency histograms). Graceful shutdown drains in-flight runs.
  - /config mutates process-wide settings (provider/model/api key)
    in-place on astra_app.config — this affects EVERY session on this
    process, not just the caller's. There is no per-user configuration
    yet (there are no users yet, absent auth). The per-request `model`
    field on /chat is a separate, narrower thing: a one-off override for
    a single call, via Agent's existing model_override plumbing, and
    does not touch global config or affect other concurrent requests.
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any, AsyncIterator, Optional

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from astra import create_astra
from astra.events import Callbacks
from astra.sessions import SessionBusyError, SessionOwnershipError
from astra.storage import SQLiteStore

from ..model_catalog import MODEL_CHOICES

# ── App-wide Astra instance — built once at startup, reused per request ──

DEFAULT_USER = "local-user"  # fixed, hardcoded — no auth yet, see module docstring

def _build_signals():
    """Shared Stop flags / session locks across workers when Redis is configured."""
    url = os.environ.get("ASTRA_REDIS_URL")
    if not url:
        return None  # default in-process store (single worker)
    from astra.signals_redis import RedisSignalStore

    return RedisSignalStore(url=url, prefix="astra:")


astra_app = create_astra(
    provider=os.environ.get("ASTRA_PROVIDER", "openai"),
    api_key=os.environ.get("ASTRA_API_KEY"),
    model=os.environ.get("ASTRA_MODEL", "gpt-4o-mini"),
    store=SQLiteStore(os.environ.get("ASTRA_DB_PATH", "astra_api_sessions.db")),
    signals=_build_signals(),
    tools_dir="astra/tools",
    enforce_ownership=True,
)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    await astra_app.start()  # connects MCP servers, if any were configured
    try:
        yield
    finally:
        await astra_app.aclose()  # drain in-flight runs, kill child processes, close clients


app = FastAPI(title="astra REST API", version="0.3.0", lifespan=lifespan)


@app.exception_handler(SessionBusyError)
async def _busy_handler(_request, exc: SessionBusyError):
    from fastapi.responses import JSONResponse

    return JSONResponse(status_code=409, content={"detail": str(exc)})


@app.exception_handler(SessionOwnershipError)
async def _forbidden_handler(_request, exc: SessionOwnershipError):
    from fastapi.responses import JSONResponse

    # 404 rather than 403: don't confirm that someone else's session exists.
    return JSONResponse(status_code=404, content={"detail": "session not found"})

app.add_middleware(
    CORSMiddleware,
    allow_origins=os.environ.get("ASTRA_CORS_ORIGINS", "*").split(","),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Auth stub ──────────────────────────────────────────────────────
# No auth this pass. Replace with a real dependency (API key header /
# JWT / session cookie) and use `user` to scope list_sessions/get_session
# so callers only see their own sessions — nothing enforces that yet.


async def current_user() -> str:
    return DEFAULT_USER


# ── Request / response models ─────────────────────────────────────


class ChatRequest(BaseModel):
    message: str
    session_id: Optional[str] = None
    attachments: Optional[list[dict]] = None
    model: Optional[str] = None  # one-off override for this call only — see module docstring


class ChatResponse(BaseModel):
    status: str
    final_text: str
    session_id: Optional[str]
    clarification_id: Optional[str] = None
    error: Optional[str] = None
    turns_used: int


class ResumeRequest(BaseModel):
    session_id: str
    clarification_id: str
    answer: str


class RegenerateRequest(BaseModel):
    model: Optional[str] = None


class ConfigResponse(BaseModel):
    provider: str
    model: str
    reasoning_effort: Optional[str]
    base_url_override: Optional[str]
    api_key_set: bool
    api_key_suffix: Optional[str]


class ConfigUpdateRequest(BaseModel):
    provider: Optional[str] = None
    model: Optional[str] = None
    api_key: Optional[str] = None  # only changed if provided (non-empty) — blank means "keep current"
    reasoning_effort: Optional[str] = None
    base_url_override: Optional[str] = None


def _to_response(result) -> ChatResponse:
    return ChatResponse(
        status=result.status,
        final_text=result.final_text,
        session_id=result.session_id,
        clarification_id=result.clarification_id,
        error=result.error,
        turns_used=result.turns_used,
    )


# ── Non-streaming endpoints ────────────────────────────────────────


@app.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest, user: str = Depends(current_user)) -> ChatResponse:
    """Send a message and wait for the full result. Omit session_id to
    start a new conversation; pass it back in to continue one."""
    result = await astra_app.chat(
        req.message,
        session_id=req.session_id,
        user=user,
        attachments=req.attachments,
        model_override=req.model,
    )
    return _to_response(result)


@app.post("/chat/resume", response_model=ChatResponse)
async def resume(req: ResumeRequest, user: str = Depends(current_user)) -> ChatResponse:
    """Answer a paused clarification and continue the run."""
    try:
        result = await astra_app.resume(req.session_id, req.clarification_id, req.answer, user=user)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return _to_response(result)


@app.post("/sessions/{session_id}/regenerate", response_model=ChatResponse)
async def regenerate(session_id: str, req: RegenerateRequest, user: str = Depends(current_user)) -> ChatResponse:
    """Discard the most recent assistant turn and run again."""
    try:
        result = await astra_app.regenerate(session_id, model_override=req.model, user=user)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return _to_response(result)


@app.post("/sessions/{session_id}/stop")
async def stop(session_id: str, user: str = Depends(current_user)) -> dict:
    """Request a stop. An in-flight LLM call or tool is cancelled within
    ~0.5 s (not only at the next step boundary). Reaches runs on other
    worker processes if a shared (Redis) SignalStore is configured."""
    astra_app.stop(session_id, user=user)
    return {"status": "stop requested", "session_id": session_id}


@app.get("/sessions/{session_id}")
async def get_session(session_id: str, user: str = Depends(current_user)) -> dict:
    session = astra_app.get_session(session_id, user=user)
    if session is None:
        raise HTTPException(status_code=404, detail=f"session '{session_id}' not found")
    return session.to_dict()


@app.delete("/sessions/{session_id}")
async def delete_session(session_id: str, user: str = Depends(current_user)) -> dict:
    astra_app.get_session(session_id, user=user)  # ownership check (raises -> 404)
    astra_app.store.delete(session_id)
    return {"status": "deleted", "session_id": session_id}


@app.get("/sessions")
async def list_sessions(limit: int = 50, offset: int = 0, user: str = Depends(current_user)) -> list[dict]:
    # Always scoped to the caller — never list other users' sessions.
    sessions = astra_app.list_sessions(user=user, limit=limit, offset=offset)
    return [s.to_dict() for s in sessions]


@app.get("/providers")
async def list_providers() -> dict:
    """The curated provider -> model catalog, so the frontend's model
    pickers (both the settings page and the inline per-chat one) have a
    single source of truth instead of a hardcoded duplicate list."""
    return MODEL_CHOICES


@app.get("/config", response_model=ConfigResponse)
async def get_config() -> ConfigResponse:
    cfg = astra_app.config
    api_key = cfg.credentials.api_key or ""
    return ConfigResponse(
        provider=cfg.credentials.provider,
        model=cfg.model,
        reasoning_effort=cfg.reasoning_effort,
        base_url_override=cfg.credentials.base_url_override,
        api_key_set=bool(api_key),
        api_key_suffix=api_key[-4:] if len(api_key) >= 4 else None,
    )


@app.post("/config", response_model=ConfigResponse)
async def update_config(req: ConfigUpdateRequest) -> ConfigResponse:
    """Mutates the process-wide AgentConfig in place. Since every
    chat()/chat_stream() call reads astra_app.config fresh, this takes
    effect on the very next request — no restart needed. Affects ALL
    sessions on this process (see module docstring)."""
    cfg = astra_app.config
    if req.provider is not None:
        cfg.credentials.provider = req.provider
    if req.model is not None:
        cfg.model = req.model
    if req.api_key:  # blank/omitted means "keep current key"
        cfg.credentials.api_key = req.api_key
    if req.reasoning_effort is not None:
        cfg.reasoning_effort = req.reasoning_effort or None
    if req.base_url_override is not None:
        cfg.credentials.base_url_override = req.base_url_override or None
    return await get_config()


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.get("/metrics")
async def metrics_endpoint() -> dict:
    """Operational snapshot: active runs, tool/LLM limiter state, circuit
    breakers, MCP status, counters and latency histograms."""
    return astra_app.stats()


# ── Streaming endpoint (SSE) ───────────────────────────────────────

_DONE = object()


def _sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


def _stream_callbacks(queue: asyncio.Queue) -> Callbacks:
    async def on_token(delta, **kw):
        await queue.put(_sse("token", {"delta": delta}))

    async def on_reasoning(delta, **kw):
        await queue.put(_sse("reasoning", {"delta": delta}))

    async def on_tool_start(call_id, tool_name, args, **kw):
        await queue.put(_sse("tool_start", {"call_id": call_id, "tool_name": tool_name, "args": args}))

    async def on_tool_result(call_id, tool_name, result, error, is_error, elapsed_ms, **kw):
        await queue.put(
            _sse(
                "tool_result",
                {
                    "call_id": call_id,
                    "tool_name": tool_name,
                    "is_error": is_error,
                    "elapsed_ms": elapsed_ms,
                    "result": result,
                    "error": error,
                },
            )
        )

    async def on_clarification_request(clarification_id, question, options, allow_free_text, **kw):
        await queue.put(
            _sse(
                "clarification_request",
                {
                    "clarification_id": clarification_id,
                    "question": question,
                    "options": options,
                    "allow_free_text": allow_free_text,
                },
            )
        )

    return Callbacks(
        on_token=on_token,
        on_reasoning=on_reasoning,
        on_tool_start=on_tool_start,
        on_tool_result=on_tool_result,
        on_clarification_request=on_clarification_request,
    )


async def _run_streamed(coro_factory, queue: asyncio.Queue, fallback_session_id: Optional[str]) -> None:
    try:
        result = await coro_factory()
        await queue.put(_sse("result", _to_response(result).model_dump()))
    except SessionBusyError as exc:
        await queue.put(_sse("result", {"status": "busy", "error": str(exc), "session_id": fallback_session_id}))
    except Exception as exc:
        await queue.put(_sse("result", {"status": "error", "error": str(exc), "session_id": fallback_session_id}))
    finally:
        await queue.put(_DONE)


async def _drain(queue: asyncio.Queue, task: "asyncio.Task") -> AsyncIterator[str]:
    try:
        while True:
            item = await queue.get()
            if item is _DONE:
                break
            yield item
    finally:
        if not task.done():
            task.cancel()


@app.post("/chat/stream")
async def chat_stream(req: ChatRequest, user: str = Depends(current_user)) -> StreamingResponse:
    """Same as /chat, but streams token/reasoning/tool/clarification
    events as Server-Sent Events while the run is in progress, ending
    with a final `result` event carrying the same payload /chat returns.

    Event types: token, reasoning, tool_start, tool_result,
    clarification_request, result.
    """
    queue: asyncio.Queue = asyncio.Queue()
    callbacks = _stream_callbacks(queue)

    async def run():
        return await astra_app.chat(
            req.message,
            session_id=req.session_id,
            user=user,
            attachments=req.attachments,
            model_override=req.model,
            callbacks=callbacks,
        )

    task = asyncio.create_task(_run_streamed(run, queue, req.session_id))
    return StreamingResponse(_drain(queue, task), media_type="text/event-stream")


@app.post("/sessions/{session_id}/regenerate/stream")
async def regenerate_stream(session_id: str, req: RegenerateRequest, user: str = Depends(current_user)) -> StreamingResponse:
    queue: asyncio.Queue = asyncio.Queue()
    callbacks = _stream_callbacks(queue)

    async def run():
        return await astra_app.regenerate(session_id, model_override=req.model, callbacks=callbacks, user=user)

    task = asyncio.create_task(_run_streamed(run, queue, session_id))
    return StreamingResponse(_drain(queue, task), media_type="text/event-stream")


@app.post("/chat/resume/stream")
async def resume_stream(req: ResumeRequest, user: str = Depends(current_user)) -> StreamingResponse:
    """Streaming variant of /chat/resume — same event shape as /chat/stream."""
    queue: asyncio.Queue = asyncio.Queue()
    callbacks = _stream_callbacks(queue)

    async def run():
        return await astra_app.resume(req.session_id, req.clarification_id, req.answer, callbacks=callbacks, user=user)

    task = asyncio.create_task(_run_streamed(run, queue, req.session_id))
    return StreamingResponse(_drain(queue, task), media_type="text/event-stream")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
