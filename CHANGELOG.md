# Changelog

All notable changes to fleetwatch. Dates are when the work landed.

## 0.4.0 - 2026-06-21

- Color throughout. States read in bright primaries, each with a glyph: blue
  `● active`, yellow `◆ waiting`, red `✗ error`, with `○ idle` and `· done` in
  grey. The glyph is a second, color-free channel, so the board still parses for
  a reader who cannot see the hue, and nothing leans on a red-vs-green contrast.
  Vendors carry their own accent (orange, cyan, magenta, violet), kept clear of
  the state colors. The detail pane takes the selected session's vendor color as
  its accent, so the right side is coded by provider too. `fleetwatch --once`
  prints in color on a terminal and stays plain when piped to a file or a log.
  It all lives in one palette, so the live board and the text snapshot stay in
  step.
- Hardened the summarizer: a guarded return replaces an assertion that could
  fire on a background thread, and the whole module is now under test: cache
  keying, in-flight de-duplication, the drain timeout, and the model-failure
  fallback to the heuristic line.

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
