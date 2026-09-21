# astra

A general-purpose agent harness, ported from a Frappe-native original.
Everything that used to require Frappe (doctypes, Redis, socket.io
realtime, ERPNext-specific tools) has been replaced with small pluggable
protocols so it runs anywhere — a script, FastAPI, a Slack bot, a CLI, tests.

## What's the same as the original

- Turn loop: build messages → call the model → run any tool calls in
  parallel → repeat.
- Full session persistence (messages, tool calls, tokens, cost) with
  checkpointing after every completed step, so a crash mid-run never
  loses more than the in-flight step.
- Tool-call lifecycle: `pending → running → success/error/cancelled/
  awaiting_clarification`.
- Loop detection (same tool + same args N times in a row → hard stop).
- `request_clarification`: a tool can pause the turn to ask the user a
  question; the run resumes later from exactly where it left off.
- `delegate_task`: spin up an isolated sub-agent, depth-limited via a
  ContextVar so it's correct even under concurrent tool calls.
- Reasoning-token buffering/replay, streamed or non-streamed, provider
  by provider (OpenAI/Anthropic/OpenRouter/... reasoning strategies —
  the entire `providers/` tree ported essentially unchanged, since it
  was already Frappe-free).
- Crash recovery: dangling `pending` tool calls get synthesized error
  results so history stays valid for the next call.

## What changed, and why

| Original (Frappe-native) | astra |
|---|---|
| "Agent session" doctype + child tables | `astra.storage.Session` (plain dataclass) behind a `Store` protocol — 4 methods: `get/save/delete/list_sessions`. Default: `SQLiteStore` (one file, zero setup). `MemoryStore` also included. **Bring your own** by implementing the protocol against Postgres, Dynamo, whatever. |
| Redis stop-flag / clarify-resume context | `astra.signals.SignalStore` protocol (`set/get/delete`, TTL). Default: `InMemorySignalStore`. Swap for Redis yourself if you need it shared across worker processes. |
| `frappe.publish_realtime` | `astra.events.Callbacks` — a dataclass of plain callables (`on_token`, `on_reasoning`, `on_tool_start`, `on_tool_result`, `on_clarification_request`, `on_done`, `on_error`, plus a catch-all `on_event`). Wire them to websockets, SSE, a queue, stdout — your call. Sync or async callables both work. |
| Hardcoded ERPNext identity/tool-guidance prompt in `setup.py` | `astra.prompts.PromptBuilder` — join any number of `Callable[[], str]` "parts". `from_markdown_file()` reads a `.md` file fresh each call (matches your "markdown files for system prompt and skills" preference); `from_text()` wraps a static string; `load_skills_index()` auto-lists a skills directory. Build your prompt however you want — it's just text. |
| "Agent Setup" singleton doctype | `astra.config.AgentConfig` / `ProviderCredentials` — plain dataclasses, `from_env()` helper. |
| "Skill" doctype | Markdown files under a directory (`astra/tools/skill_tools/`), via `configure_skills_dir()`. `list_skills`/`view_skill`/`create_skill` tools operate on `*.md` files directly. |
| `frappe_tools/` (ERPNext document/report/workflow tools) | **Dropped entirely**, per your instruction — this is a general harness now. |
| `anomaly/` | **Dropped**, per your instruction. |
| Frappe-specific notification channels (`notify.py`, `human_approval`) | **Dropped for now** — these belonged to the workflow engine, which you asked to defer. |
| `workflow/`, `trigger.py`, `verify.py` | **Deferred**, per your instruction — you'll want multiple entry points later (manual chat, webhook, n8n-style workflow steps). The pieces that make that easy are already in place: `Callbacks` for transport-agnostic events, `Provenance` for trigger bookkeeping, the `ClarificationPending`/pause-marker pattern the workflow engine's `WorkflowPaused` will mirror. |
| `pdf_tools/` (BBS-extractor etc.) | **Dropped** — too domain-specific to the original business to generalize meaningfully. |

## Layout

```
astra/
  config.py           AgentConfig / ProviderCredentials (plain dataclasses)
  events.py           Callbacks — your event hooks
  signals.py           SignalStore — stop flags, clarification resume-context
  prompts.py           PromptBuilder — markdown-file or callable-based prompt assembly
  storage/
    base.py             Session/MessageRow/ToolCallRow + Store protocol
    sqlite_store.py      default Store (zero setup)
    memory_store.py       in-memory Store (tests/ephemeral)
  providers/            ported ~as-is (already Frappe-free): provider configs,
                         registry, OpenAI-compatible client w/ streaming + retries
  tools/
    decorator.py, loader.py, executor.py   ported ~as-is (already Frappe-free)
    clarify_approval_tools/   request_clarification
    delegate_tools/            delegate_task (needs configure_delegate() at startup)
    skill_tools/                list/view/create skill (markdown files)
  agent/
    conversation.py     session state, history reconstruction, tool lifecycle, events
    agent.py             the turn loop
    runner.py             convenience entry points (run_turn, resume_after_clarification, ...)
  agent_types/messages.py   Message/MessageList aliases
examples/
  quickstart.py         end-to-end wiring example
```

## Quickstart

```bash
pip install -e .
export ASTRA_PROVIDER=openai ASTRA_API_KEY=sk-... ASTRA_MODEL=gpt-4o-mini
python examples/cli.py
```

### The SDK — one import, configure once, reuse everywhere

`astra.create_astra(...)` is the single entry point. It builds and holds
the shared `Store`/`ToolRegistry`/`SignalStore` once; you call `.chat()`
per message from anywhere in your app afterward.

```python
from astra import create_astra

app = create_astra(
    provider="openai",
    api_key="sk-...",
    model="gpt-4o-mini",
    tools_dir="astra/tools",         # auto-loads every tool group
    system_prompt="You are helpful.",
)

result = await app.chat("Hello!")
print(result.final_text, result.session_id)

# continue the same conversation later, anywhere, just by passing the id back:
result = await app.chat("And then?", session_id=result.session_id)
```

Handling a clarification pause:

```python
result = await app.chat("Cancel my order")
if result.status == "clarification_pending":
    answer = input("...")  # or however your UI collects it
    result = await app.resume(result.session_id, result.clarification_id, answer)
```

Stopping a run cooperatively (checked at the next loop/tool boundary):

```python
app.stop(session_id)
```

For full control over every knob (pricing lookup, attachments builder,
delegate resolver, a custom `Store`), build `AstraSettings` directly:

```python
from astra import Astra, AstraSettings
from astra.config import AgentConfig, ProviderCredentials

settings = AstraSettings(
    agent_config=AgentConfig(credentials=ProviderCredentials(provider="anthropic", api_key="...")),
    store=my_custom_store,          # any object matching the Store protocol
    skills_dir="skills",
    delegate_resolver=my_agent_lookup_fn,
)
app = Astra(settings)
```

A full terminal chat client built entirely on this SDK (streaming
tokens, tool activity, clarification prompts) is in `examples/cli.py`.

### REST API

A ready-to-run FastAPI app, also built entirely on the SDK, is in
`examples/api/main.py`.

```bash
pip install -e ".[api]"
export ASTRA_PROVIDER=openai ASTRA_API_KEY=sk-... ASTRA_MODEL=gpt-4o-mini
uvicorn examples.api.main:app --reload
```

| Endpoint | Purpose |
|---|---|
| `POST /chat` | Send a message, wait for the full result |
| `POST /chat/stream` | Same, but Server-Sent Events (`token`, `reasoning`, `tool_start`, `tool_result`, `clarification_request`, `result`) |
| `POST /chat/resume` | Answer a paused clarification |
| `POST /sessions/{id}/stop` | Cooperative stop |
| `GET /sessions/{id}` | Fetch a session's full state |
| `GET /sessions` | List sessions (optionally `?user=...`) |
| `GET /health` | Health check |

**Deliberately out of scope for this pass** (see the module docstring
in `examples/api/main.py` for detail):
- **No auth.** Every endpoint is open — add a real auth dependency
  before exposing this publicly; nothing currently stops one caller
  from reading/resuming/stopping another's session by guessing its id.
- **Single worker only.** The default `SignalStore` (stop-flags) is
  in-process — `stop()` only reaches a run on the *same* worker. A
  Redis-backed `SignalStore` (same 3-method contract) is the fix for
  multi-worker/multi-replica deployments — not built this pass.
- **`SQLiteStore` by default** — fine for light/moderate load; swap in
  `examples/postgres_store.py`'s `PostgresStore` for real concurrent
  traffic.

### Testing

```bash
pip install -e ".[dev]"
pytest tests/ -q
```

59 tests covering: basic/multi-turn chat, parallel tool calls, loop
detection (both "model recovers" and "model never listens → max_turns"
cases), provider errors, model-stops-mid-thought handling, cooperative
stop, every callback hook (sync and async), a hand-written duck-typed
`Store` with zero astra imports, `SQLiteStore` durability across
separate instances, clarification pause/resume (including resolving one
after many unrelated sessions ran to completion in between), crash
recovery, `delegate_task` (basic/unknown-agent/disabled/unconfigured/
depth-limit-enforced), Provenance-tagged cron/webhook-style invocations,
a regression test for a real module-identity bug in the tool loader,
and the full REST API (including SSE streaming and clarification-over-
SSE). No network calls — a scriptable `FakeProvider` in
`tests/conftest.py` stands in for the LLM everywhere.

### Lower-level access, if you need it

Everything `Astra` wraps is still directly usable — `Conversation`,
`Agent`, `Store`, `Callbacks`, `ToolRegistry`, `run_turn` etc. — for
cases the SDK doesn't cover (e.g. holding onto an `Agent` instance
mid-run, or building your own orchestration around `Conversation`
directly instead of per-call session construction):

```python
from astra.storage import SQLiteStore
from astra.tools.decorator import ToolRegistry
from astra.tools.loader import load_tools
from astra.agent.conversation import Conversation
from astra.agent.runner import run_turn
from astra.config import AgentConfig

store = SQLiteStore("sessions.db")
registry = ToolRegistry()
load_tools(registry, "astra/tools")

conversation = Conversation(store=store, user="alice")
config = AgentConfig.from_env()

result = await run_turn(conversation, registry, config, "Hello!", system_prompt="You are helpful.")
print(result.final_text)
```

## Using `delegate_task`

Via the SDK, just pass a resolver at construction:

```python
app = create_astra(..., delegate_resolver=my_agent_lookup_fn)
# my_agent_lookup_fn(name) -> object with .content/.is_agent/.is_enabled, or None
```

Or configure it manually for lower-level usage:

```python
from astra.tools.delegate_tools.delegate import DelegateContext, configure_delegate

configure_delegate(DelegateContext(
    resolve_agent=my_agent_lookup_fn,
    store=store,
    config=config,
    registry=registry,
))
```

## Using clarification pause/resume

```python
result = await run_turn(conversation, registry, config, "Cancel my order")
if result.status == "clarification_pending":
    # ... surface result.clarification_id + the question (from your
    # on_clarification_request callback) to the user, get their answer ...
    result = await resume_after_clarification(
        store, registry, config, conversation.session_id,
        result.clarification_id, answer="#123",
    )
```

## Not yet ported (by design — deferred per your instructions)

- **Workflow engine** (n8n-style step graph: tool/branch/loop/trigger
  steps, pause/resume). Reference: the original `workflow/engine.py`.
- **Trigger entry point** (webhook / scheduled / event-driven agent
  firing). Reference: the original `trigger.py`.
- **Manual-chat HTTP entry point** (SSE streaming, stop/feedback
  endpoints). Reference: the original `verify.py`. You said you'd wire
  this into FastAPI or similar yourself — `Callbacks` + `AgentResult` +
  `Conversation.request_stop()`/`is_stop_requested()` are the surface to
  build routes on top of.

When you're ready for those, the shapes they'll plug into are already
here: `Provenance` on `Session` for trigger bookkeeping, `Callbacks` for
transport-agnostic streaming, and the `ClarificationPending` pause
pattern for the workflow engine's step-pause equivalent.
