# Astra — frontend

A standalone React + Vite + Tailwind chat UI for the `astra` REST API
(`POST /chat/stream`, sessions, config, clarification resume). This is
the redesigned "instrument panel" theme, rebuilt as a plain Vite app
with no platform-specific dependencies — no auth, no database, no
custom server middleware. It talks directly to your running `astra`
backend.

## Run it

```bash
npm install
npm run dev
```

Open `http://localhost:5173`. By default the app talks to
`http://localhost:8000`. To point at a different backend origin, copy
`.env.example` to `.env` and set `VITE_ASTRA_API_URL`.

If the backend is unreachable, the app falls back to **demo mode**
automatically, with seeded conversations, so the UI is still usable
without a live backend.

## What changed from the original rebuild

The workspace this was extracted from was scaffolded inside a
proprietary app-builder platform (TanStack Start + Nitro server +
better-auth + Postgres/pglite + platform-only virtual modules like
`server/middleware/grok-pwa.ts`). None of that is needed for this UI —
the actual chat logic (`src/lib/chat/*`) never touched auth or a
database. This rebuild:

- Drops TanStack Start/Router, Nitro, better-auth, Kysely, pg, pglite,
  and all `src/lib/auth`, `src/lib/app-data`, `src/lib/multiplayer`,
  `server/`, and `migrations/` code.
- Replaces the router's URL-search-param sync (`?c=<sessionId>`) with
  a small dependency-free hook, `src/lib/use-url-session.ts`, that does
  the same thing with the plain History API.
- Replaces the two router-generated shell files (`routes/__root.tsx` +
  `routes/index.tsx`) with a plain `index.html` + `src/main.tsx` +
  `src/App.tsx`.
- Everything else — every component under `src/components/astra` and
  `src/components/ui`, the whole `src/lib/chat/*` chat engine
  (streaming, demo mode, session transforms, types), and
  `src/styles.css` (the full theme) — is carried over unchanged.

## Retheming

One file controls every color: `src/styles.css` (the `@theme` block
plus the `[data-theme="light"]` override). No component has a
hardcoded color.

## Shortcuts

| Key | Action |
| --- | --- |
| `⌘/Ctrl K` | Command palette |
| `⌘/Ctrl N` | New chat |
| `⌘/Ctrl [` | Toggle sidebar |
| `⌘/Ctrl ]` | Toggle inspector |
| `Enter` | Send |
| `Shift+Enter` | New line |

## Note

I could not run `npm install` / build this in the sandbox I built it
in (no network access there), so please run `npm install && npm run
dev` (or `npm run build`) yourself and let me know if TypeScript or
the bundler surfaces anything — happy to fix it up.
