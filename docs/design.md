# fleetwatcher — cross-CLI session monitor

**Date:** 2026-06-17
**Status:** approved, implementing
**Author:** Luke Steuber

## Problem

Multiple terminal coding sessions run at once — locally and on a VPS, across
different vendor CLIs (Claude Code, Codex, Grok, and others). Managing them
individually is fine; what is missing is a single, ongoing, plain-language view
of what every session is doing and which ones need attention.

## Goal

A separate CLI that watches all running sessions and renders a live dashboard:
for each session, what it is working on, when it last moved, and whether it is
blocked waiting on the human.

## Key insight

Every one of these CLIs already writes per-project session state to disk, so the
monitor needs no cooperation from them — it reads their files read-only.

| CLI | Location | Shape |
|-----|----------|-------|
| Claude Code | `~/.claude/projects/<enc-cwd>/<uuid>.jsonl` | full transcript, 1 msg/line |
| Codex | `~/.codex/sessions/` + `~/.codex/history.jsonl` | dated rollout files |
| Grok | `~/.grok/sessions/<enc-cwd>/prompt_history.jsonl` + `~/.grok/active_sessions.json` | per-cwd history + a live registry |

Verified facts that shape the design:
- Claude keeps **no** `~/.claude/todos/` sidecar; todos come from the latest
  `TodoWrite` tool_use in the transcript.
- The "needs you" signature is a **dangling `tool_use`** (an assistant record
  with a tool call and no following `tool_result`) combined with a stale mtime.
- Grok's `active_sessions.json` is a real registry but can be empty; cross-check
  it against `prompt_history.jsonl` mtime so it never hides a session.

## Decisions (locked)

- **Summaries:** hybrid — heuristic one-liner always (free, instant); a fast
  model (Haiku, `claude-haiku-4-5-20251001`) paragraph only for sessions that
  need attention, cached by `(session_id, last_activity)`.
- **Reach:** local Mac first, with a `Source` seam so a VPS drops in later.
- **Vendors:** Claude Code, Codex, Grok.
- **Display:** live TUI dashboard (Textual).

## Architecture — four layers

```
adapters/{claude,codex,grok}.py   discover + parse vendor files -> SessionState
core.py (Aggregator)              poll by mtime, fold state, diff, sort, dispatch summaries
summarize.py                      heuristic line + lazy Haiku paragraph, cached
tui.py (Textual)                  live rows, needs-first, detail pane
```

### The contract: `SessionState`

```
vendor, session_id, project, cwd, source
last_activity, state(active|waiting|idle|done|error)
doing, needs, summary
todos, last_user, last_agent, error
```

JSON-serializable so a remote host exports it verbatim.

### Adapter interface

- `discover(source) -> [SessionRef]` — cheap, every refresh; `[]` if not installed.
- `read(source, ref, prev) -> SessionState | None` — never raises; on trouble
  returns `state=ERROR`. May use `prev` for context but stays correct when None.

Adapters reach the filesystem only through `Source` (today `LocalSource`); a
future `RemoteSource` keeps adapters unchanged.

### State & needs detection

- **active** — mtime within `ACTIVE_WINDOW` (default 12s).
- **waiting** — dangling tool_use, or a trailing question, with stale mtime → set
  `needs` ("waiting on permission" / "asked a question").
- **idle** — last turn completed, quiet < `DONE_AFTER` (30m).
- **done** — completed and quiet ≥ `DONE_AFTER`.
- **error** — vendor error record or unreadable file.

### Performance

mtime scan every ~2s; only changed files are parsed; reads are a bounded tail
(~512 KB) so a 25 MB transcript is never read whole. Summaries run off the UI
thread and update state objects in place.

### Error handling

Partial last JSONL line → skipped, never crashes a refresh. Missing vendor dir
→ empty. Model/API failure → fall back to the heuristic line with a small
"summary unavailable" marker; the UI never blocks on the network.

## Out of scope (v1)

VPS aggregation (seam only), Gemini/Cursor adapters, scrolling-feed and
push-alert UIs (the aggregator keeps the event diff internally so they drop in
later), persistence/analytics DB.

## Testing

Committed sanitized fixture transcripts per vendor assert state detection
(especially the dangling-tool_use "waiting" case and the partial-line case) and
tail correctness; summarizer cache hit/miss and model-failure fallback; a TUI
smoke render over a fixed state set.
