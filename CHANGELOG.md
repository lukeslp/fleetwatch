# Changelog

All notable changes to fleetwatch. Dates are when the work landed.

## 0.3.1 - 2026-06-17

- `anthropic` is now an optional extra (`pip install "fleetwatch[summaries]"`).
  A bare install pulls only Textual, makes no network calls, and never spends
  tokens. Summaries stay available the moment the extra and an API key are present.
- Open-source docs: full README, LICENSE (MIT), this changelog, contributing guide.

## 0.3.0 - 2026-06-17

- Full-fleet summary sweep: `S` in the dashboard and `--summarize-all` on the CLI
  generate a summary for every session at once.

## 0.2.4 - 2026-06-17

- Selecting a session now summarizes it on demand, so the detail panel shows a
  real plain-language sentence instead of falling back to the raw command line.
  Active sessions keep their live activity line.

## 0.2.3 - 2026-06-17

- Model summaries are collapsed to a single clean line.

## 0.2.2 - 2026-06-17

- A `HOST` column appears when more than one machine is in view.

## 0.2.1 - 2026-06-17

- Fixes from an external code review: the manual-summary key now resolves for
  adapters that rewrite session ids; one malformed todo no longer drops a whole
  remote session; removed a dead import.

## 0.2.0 - 2026-06-17

- Gemini CLI adapter (active/idle/done; Gemini has no approval gate, so no
  `waiting`).
- Watch other machines over ssh with `--hosts`, by pulling each host's own
  `--export-json`. Unreachable hosts go stale rather than disappearing.
- Fixed a Grok freshness blind spot: activity is now tracked across all of a
  session's files, rather than only its prompt history.
- Cleaner activity lines (heredocs and `cd … &&` prefixes stripped).

## 0.1.0 - 2026-06-17

- First version: adapters for Claude Code, Codex, and Grok; an aggregator that
  polls by modification time with bounded-tail reads; hybrid summaries (a free
  heuristic line plus an optional Claude Haiku sentence); a Textual dashboard
  with `--once` and `--export-json` modes.
