# Changes in this pass

This document summarizes what was added/changed on top of the original
astra codebase, why, and what stayed the same. All 70 original tests pass
unmodified; 84 new tests were added (154 total).

## Why

Three questions drove this work, in order:
1. How should tool registration work so CLI tools, Python tools, and
   tools in other languages don't each need their own subprocess code?
2. What in the current code limits performance or capability at scale
   (many concurrent users)?
3. Is `delegate_task` the right way to do multi-agent orchestration?

## 1. Tool layer (astra/tools/)

**New files:** `core.py` (was `types.py` — renamed to dodge a stdlib
`types` circular import), `validation.py`, `process.py`, `sandbox.py`,
`handlers.py`, `runtime.py`, `manifest.py`, `mcp_source.py`,
`exec_tools/`, `discovery_tools/`.

**Changed:** `decorator.py`, `executor.py`, `loader.py` — all backward
compatible. Every old entry point (`@tool(schema_name=...)`,
`registry.load_schemas`, `registry.register_tool`, `registry.executors`,
`executor._dispatch`, `configure_delegate`, `configure_skills_dir`) still
works exactly as before.

**What's new:**
- A `Tool` object separates *what* a tool is from *how* it runs (a
  `Handler`): `FunctionHandler` (Python), `ProcessHandler` (any CLI/any
  language via one shared `run_process`), `McpHandler` (any MCP server).
  This directly answers question 1 — a CLI tool is now a `tool.json`
  manifest with zero Python code, not a hand-written subprocess wrapper.
- `run_process` is the ONE place subprocess code lives: no shell ever,
  minimal environment by default, byte-capped streaming output, timeout
  with SIGTERM→grace→SIGKILL on the whole process *group* (kills
  grandchildren too — verified: a background `sleep` spawned by a test
  process is killed, not orphaned as a zombie), and cancellation-safe.
- `ToolExecutor` gained: JSON-Schema argument validation (with a
  dependency-free fallback), timeouts, output truncation (with an
  artifact spill), an allow/deny policy hook, a public `execute()` →
  `ToolResult`, and — this fixes a real bug — retries that are **opt-in**
  (`idempotent=True`) instead of retrying every failing tool 3x
  regardless of side effects.
- `ToolRuntime` is the shared, process-wide execution environment:
  concurrency caps (global/per-tenant/per-tool) with a bounded wait queue
  (overload → a typed `busy` result, not an unbounded pile-up), a
  separate cap on concurrent child *processes*, per-tool circuit
  breakers, the sandbox runner, and shutdown (kills tracked processes).
- `McpSource` registers any MCP server's tools with zero glue code,
  built on the official SDK. Includes schema-pinning / quarantine
  (a tool whose description changes after first connect is removed, not
  silently updated — protects against a compromised/updated server
  changing what it tells the model to do), per-server env allowlisting,
  and reconnect-on-drop.
- `exec_tools/exec.py`: an opt-in generic command runner (the
  Claude-Code/Codex-CLI pattern — a shell tool + skills) for cases where
  wrapping every CLI individually isn't worth it. Disabled by default;
  needs an explicit allow-list.
- `discovery_tools/search.py` + `deferred=True`: tools can be hidden from
  the model's context until searched for, for accounts with many tools.

## 2. Scale / safety fixes

Each of these was *measured* before being changed (see the session
history / your saved research) — not guessed.

| Problem (measured) | Fix |
|---|---|
| `SQLiteStore.save()` blocked the event loop; 64 sessions/s at 500 concurrent, p99 loop-lag 1.3s | WAL mode + `synchronous=NORMAL` by default → 4x throughput. Still synchronous by design (an async store would break the Frappe/Django adapters, which rely on Frappe's per-thread context / `async_to_sync`) |
| Two concurrent runs on one session corrupted the history (lost update) | `SessionGuard`: in-process lock (Redis-backed lease across processes, if you configure a `SignalStore` with `set_if_absent`). A second run gets `SessionBusyError` fast, not silent corruption |
| No ownership check — loading someone else's `session_id` worked | `Conversation(..., enforce_owner=True)` / `Astra(..., enforce_ownership=True)`. Off by default (backward compatible) |
| A new LLM HTTP client was built on every `build_agent()` call (~20ms blocking + a new connection pool each time) | `ProviderPool`: clients cached per (endpoint, key-hash, loop) |
| SDK's own retries (2) stacked under astra's (3) → up to 9 tries; no `Retry-After` handling; no way to cap concurrent LLM calls | `sdk_max_retries=0` by default, astra owns the single retry loop, honors `Retry-After`, optional per-endpoint concurrency cap + circuit breaker |
| `max_context_chars` was defined but never used; full history replayed every turn | `ContextManager`: replay-time trimming (oldest tool results cleared first) + optional rolling summarization |
| Stop was only checked between loop/tool boundaries — a slow LLM stream or long tool kept running/spending after Stop | `Agent._cancellable()`: an in-flight call is cancelled within `stop_poll_interval` (default 0.5s); cancellation propagates into process tools (kills the process group) |
| Tool/directory iteration order wasn't sorted — schema order (and therefore the provider prompt prefix / cache hit rate) could differ across hosts/restarts | `loader.py` and `registry.get_tool_schemas()` both sort deterministically |
| No isolation for subprocess tools | `sandbox.py`: bubblewrap-based `LocalRunner` when available (verified working on this host), `RemoteRunner` as an interface for a microVM pool, `require_sandbox` to fail closed |

**Not changed:** the `Store` protocol stays synchronous (by design, see
above); Postgres/Redis were **not** made hard requirements — they're
optional (`RedisSignalStore` in `signals_redis.py`; Postgres is still
just an example adapter, `examples/postgres_store.py`, unchanged).

## 3. Multi-agent / delegate_task

Kept `delegate_task` as the core primitive (it's the same "agent as a
tool" pattern used by Claude Code, Cursor, and Anthropic's own
orchestrator-worker research system) and fixed what made it incomplete:

- **Per-agent scoping**: a `SubAgentSpec` can now declare `tools`
  (allow-list) / `disallowed_tools` (deny-list), `model`,
  `reasoning_effort`, `max_turns`, `timeout_seconds` — least privilege
  and cheap models for cheap sub-tasks, both verified with tests.
- **Bounded fan-out**: per-parent and global concurrency caps
  (`max_parallel_per_parent`, `max_parallel_global`) — beyond the cap,
  `delegate_task` returns a `busy` result instead of piling up unbounded
  concurrent LLM loops. Verified with a test that fires 2 parallel
  delegate calls against a cap of 1.
- **Stop propagates to children**: a `ToolRuntime.run_ended` hook
  cancels un-awaited background sub-agents when the parent run ends.
- **Usage roll-up**: child token/cost usage is added to the parent
  session's `delegated_input_tokens` / `delegated_output_tokens` /
  `delegated_cost`.
- **Background mode**: `spawn_agent` / `await_agents` / `cancel_agent` —
  start a sub-agent, keep working, collect the result later. This is the
  documented gap in Anthropic's own system ("the lead agent cannot steer
  subagents mid-process... can block on a single subagent").

`delegate_task` (blocking) is unchanged in its basic contract — several
calls in one model step still run in parallel via the existing
`asyncio.gather` in `_execute_tool_calls_parallel`.

## Everything else: `astra.client.Astra`

`Astra` (`create_astra()`) is the thing that ties all of the above
together for an application: one shared `ToolRuntime`, one
`ProviderPool`, a `SessionGuard`, a `ContextManager`, optional `Quota`,
optional MCP servers, graceful `aclose()` (drains in-flight runs, kills
child processes, closes HTTP clients). `examples/api/main.py` was updated
to use all of it (session ownership per-request, 409 on a busy session,
`/metrics`, Redis signal store via `ASTRA_REDIS_URL`, lifespan-managed
startup/shutdown).

## 4. Group chat (astra.group) — multiple named agents in one shared thread

Researched AutoGen `GroupChat`, Semantic Kernel `AgentGroupChat`, OpenAI
Agents SDK handoffs, and Grok Bot's group-chat UX before building this.
Key finding that shaped the design: chat-completion APIs require exactly
one continuous "assistant" identity per request — there's no third-party
role — so every framework that does this re-projects the shared history
per speaker right before it talks (that speaker's own past turns become
`assistant`, everyone else's become `user`). That re-projection is a
different function from `Conversation.get_messages()`'s single-perspective
replay, and bending the existing one to do both was judged not worth the
risk to a file (`conversation.py`) that clarification/recovery/regenerate/
summarization all depend on staying simple. So this is a **new, separate
module** (`astra/group/`) with its own small data model
(`GroupSession`/`GroupMessage`, not `Session`/`MessageRow`), not a patch to
`Conversation`. Nothing about the existing single-agent path changed.

**What's reused, unchanged:** `ToolExecutor`/`ToolRuntime` (same
concurrency caps, sandboxing, circuit breakers, telemetry every tool call
already gets), `ProviderPool`/`OpenAIProvider.generate`, `SessionGuard`
(one group turn at a time — the same exclusivity primitive that fixed the
single-agent lost-update bug), `ContextManager` (bounds each per-speaker
projection — a real AutoGen GitHub issue reports the equivalent prompt
"will continuously grow in token count as the conversation continues"
because nothing trimmed it), and `Quota`.

**Design decisions made deliberately more conservative than the research
baseline**, based on a documented AutoGen incident (agents replying to
each other unsupervised for 9+ days, 60,000+ tokens) and a survey of
production multi-agent failure modes (broadcast storms, escalation
spirals, ping-pong):
- **Default speaker selection is deterministic `@mention` routing**, not
  an LLM-selected "auto" mode — AutoGen's own docs recommend this over
  their own default "when simple rules are sufficient and more reliable."
  An LLM `ModeratorSelector` is available, opt-in.
- **Default mode is single-pass**: the selector runs once per user
  message; a participant's reply does not itself trigger another
  selection round. Multi-round autonomous discussion
  (`allow_agent_triggered_rounds=True`) is opt-in and still bounded.
- **`MaxRoundsTermination` is always applied**, composed with whatever
  termination condition you pass — cannot be disabled, only raised.
- **A same-speaker-cannot-immediately-retrigger-itself guard is always
  on** for agent-triggered continuation (never blocks a participant from
  answering two separate user messages).
- Found and fixed during testing: an early version of the round loop
  could spin forever if every candidate for a round was blocked by the
  no-repeat guard (nothing advanced, nothing detected it) — now any round
  where nobody actually spoke stops the turn instead of re-selecting
  indefinitely. Covered by `test_broadcast_storm_shape_is_capped_not_exponential`
  and `test_no_immediate_repeat_guard_blocks_self_retrigger`.
- Tool activity is replayed as a flattened text summary, not structured
  `tool_calls`/`tool`-role blocks, for every speaker's turn including
  their own — matches AutoGen's `GroupChat.append()`, which does the same
  for the same reason (a provider's tool-call/tool-result role pairing
  requirement doesn't survive another speaker's turn being interleaved in
  between across rounds).

**Files:** `astra/group/models.py` (`GroupSession`/`GroupMessage`/
`GroupToolCall`), `store.py` (`GroupMemoryStore`, `GroupSQLiteStore` — WAL
+ optimistic concurrency, same discipline as `SQLiteStore`),
`projection.py` (the per-speaker re-projection), `selection.py`
(`MentionSelector` default, `RoundRobinSelector`, `StaticSelector`,
`FunctionSelector`, `ModeratorSelector`), `termination.py`
(`MaxRoundsTermination`, `KeywordTermination`, `FunctionTermination`,
composable), `chat.py` (`GroupChat`, `GroupParticipant` — the
orchestrator). `Astra.make_group()`/`load_group()` on the existing client
are additive-only convenience methods that share the app's `ToolRuntime`/
`ProviderPool`/`quota` — `.chat()`/`.resume()`/`.recover()`/`.regenerate()`
are untouched.

29 new tests in `tests/test_group_chat.py` (183 total, all passing).
