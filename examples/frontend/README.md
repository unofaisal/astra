# astra chat frontend

A React + Vite + Tailwind chat UI for the `astra` REST API
(`examples/api/main.py`). Built to match the component/interaction
patterns of frontier chat UIs (Claude.ai, ChatGPT) — see the design
notes at the bottom.

## Run it

**1. Backend** (from the `astra/` project root):

```bash
pip install -e ".[api]"
export ASTRA_PROVIDER=openai ASTRA_API_KEY=sk-... ASTRA_MODEL=gpt-4o-mini
uvicorn examples.api.main:app --reload
```

**2. Frontend:**

```bash
cd examples/frontend
npm install
npm run dev
```

Open `http://localhost:5173`. If the backend isn't on
`http://localhost:8000`, set `VITE_ASTRA_API_URL` in a `.env` file in
this directory.

## Retheming

**One file controls every color in the app**: `src/styles/tokens.css`.
Every Tailwind utility class used in `components/*.jsx` (`bg-surface-1`,
`text-accent`, `border-attention`, …) resolves to a CSS custom property
defined there — no component has a hardcoded hex value. To reskin:
edit the values in `tokens.css`; nothing else needs to change.

To add a second theme (e.g. a light mode) rather than just editing the
one palette: duplicate the `:root { ... }` block under a
`:root[data-theme="light"] { ... }` selector with new values, then
toggle `document.documentElement.dataset.theme` at runtime — no
component changes needed either way, since they only ever reference the
named Tailwind utilities.

## Design notes

Researched current patterns from Claude.ai, ChatGPT, and agent-coding
tools (Claude Code, Cursor) before building: the 3-pane layout (sidebar
+ capped-width stream + right panel that opens only when there's real
content) has converged as the standard; reasoning traces and tool calls
are collapsed-by-default with an honest label; status is conveyed via
icon + text + color together, never color alone.

Deliberately avoided the generic AI-app look (warm cream, or
near-black-plus-neon, or interchangeable rounded SaaS cards). The
concept here is **"instrument panel"** — closer to flight-deck telemetry
than a friendly assistant, since astra is agent-framework
infrastructure, not a consumer companion. A cold near-black surface
system; one interactive accent (indigo-violet) used only for
actionable elements, never decoration; a separate amber reserved
specifically for clarification/attention states so it never competes
with the accent's meaning; a monospace/humanist type pairing where
monospace consistently means "the machine produced this exactly"
(tool names, arguments, session IDs, code) and humanist sans means
"this is conversation." No literal star/sparkle iconography — that's
the most overused AI-generic signal, and the opposite of what
"instrument panel" is going for.

## What's real vs. stubbed, and why

| Feature | Status | Notes |
|---|---|---|
| Streaming chat (tokens, reasoning, tool trace) | **Real** | `POST /chat/stream`, parsed by hand in `lib/api.js` (SSE-over-POST isn't supported by the browser's native `EventSource`, which is GET-only). |
| Clarification pause/resume | **Real** | Inline card wired to `POST /chat/resume/stream`. |
| Regenerate last turn | **Real** | Required a small backend addition: `Conversation.truncate_last_assistant_turn()` (core), `Astra.regenerate()` (SDK), `POST /sessions/{id}/regenerate[/stream]`. |
| Stop button | **Real** | Aborts the client-side stream *and* calls `POST /sessions/{id}/stop` (cooperative — checked at the next loop/tool boundary, not an instant kill). Single-worker only — see backend README. |
| Inline per-chat model picker | **Real** | Sends `model` on `/chat`; uses `Agent`'s existing `model_override` plumbing — a one-off per-call override, doesn't touch global config. |
| Settings page (provider / API key / default model / reasoning effort) | **Real** | `GET`/`POST /config`, mutates the backend's process-wide `AgentConfig` in place — takes effect on the very next request, no restart. **Affects every session on the process** (there's no per-user config without auth) — this is a deliberate, documented tradeoff, not a bug. |
| Session list, search, grouping by recency, delete | **Real** | `GET /sessions` (client-side title search — see "known tradeoffs" below), `DELETE /sessions/{id}`. |
| Export conversation | **Real**, client-side only | Downloads the current turns as Markdown — no backend endpoint needed, just serializes data already in memory. |
| Run Inspector (right panel) | **Real** | Full tool-call trace, token/cost usage, raw session JSON — all from data the backend already returns via `GET /sessions/{id}`. Repurposes the "artifact panel" slot with something real today, per the agreed direction. |
| Copy message / copy code block | **Real** | Client-side only, `navigator.clipboard`. |
| Attachments | **Stub** | Composer and top bar show a disabled paperclip with a tooltip explaining why. No file-upload endpoint or `attachments_builder` exists server-side yet — `Conversation.add_user_message` accepts an `attachments` list in principle, but there's nowhere to actually upload a file to today. **To unstub**: add a file-upload endpoint (returns a `file_url`/`file_path`), and configure an `attachments_builder` on the `Astra` instance so `get_messages()` actually inlines them for the model. |
| Share (public link) | **Stub** | Disabled button with a tooltip. **To unstub**: needs a backend concept of a shareable/read-only session view — doesn't exist. |
| Artifacts tab (right panel) | **Stub**, explicitly labeled "coming soon" in the UI itself | No backend event type for generated documents/code exists yet. **To unstub**: define an artifact event/content type, emit it via a new `Callbacks` hook (e.g. `on_artifact`), stream it the same way tool events are streamed now. |

### Known tradeoffs worth knowing about

- **Search is client-side, title-only.** `GET /sessions` returns full
  session data already, but there's no full-text-across-messages search
  endpoint — searching message *content* would mean fetching every
  session's full history client-side, which doesn't scale. Fine for a
  personal/local tool; a real multi-session-history search should be a
  backend endpoint (e.g. a message-content index) before this goes to
  more than a handful of sessions per user.
- **`GET /sessions` returns full session bodies (including every
  message), not lightweight summaries.** The sidebar only reads
  `title`/`session_id`/`last_active` off each entry, but the backend
  sends everything — fine at small scale, wasteful once sessions have
  long histories or there are many of them. A real fix is a backend
  summary endpoint (title/timestamps/counts only) rather than reusing
  the full-detail endpoint for the list view.
- **No auth, and `/config` is genuinely global**, per the backend's own
  documented scope — this frontend doesn't add any authorization on top
  of that. Don't expose this past local/trusted use without addressing
  both.
- **Estimated cost shows as $0.00** unless the backend is given a
  `pricing_lookup` callable (it isn't, by default) — the field is real
  and wired correctly, it's just always zero until that's configured,
  which is a backend-level `Astra`/`AstraSettings` setting, not
  something this frontend can fix on its own.
- **Bundle size**: `react-syntax-highlighter` pulls in every language
  grammar and dominates the production bundle (~1MB before gzip, ~340KB
  after). Fine for local/internal use; for a real deployment, switch to
  its async/lazy-loading build variant or a lighter highlighter.
