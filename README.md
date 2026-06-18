# fleetwatch

One screen for every terminal coding session you have running.

If you keep more than one coding CLI going at once (Claude Code in one window,
Codex in another, Grok in a third, some local and some on a server), fleetwatch
gives you a single live dashboard of what they are all doing and which ones are
waiting on you.

It reads each tool's own session files on disk, read-only. There is nothing to
install into the other tools, no daemon, no hooks. If a CLI writes a transcript,
fleetwatch can watch it.

```
fleetwatch  17:18:43  active 1  waiting 0  idle 2  done 7  (total 10)

VENDOR  PROJECT     STATE   IDLE  !  WHAT
-----------------------------------------
claude  orrery      active    3s     editing PhaseSpaceCoordinator.swift
claude  bipolar     waiting  40s  !  waiting for you to approve: rm -rf build
codex   alt-text    idle      4m     finished a turn
grok    hivescape   done      2h
```

## What it tracks

For each session: the vendor, the project, what it is working on right now, how
long since it last moved, and a flag the moment it needs you.

| State | Meaning |
|-------|---------|
| `active` | the transcript just changed; it is working |
| `waiting` | blocked on you (a pending permission, an unanswered question) |
| `idle` | finished its turn a moment ago |
| `done` | finished a while ago |
| `error` | something failed, or the transcript cannot be read |

The `waiting` signal is the point of the whole tool, and it is built on the real
shape of each vendor's files: a Claude `tool_use` with no matching result, a
Codex command awaiting approval, a Grok `permission_requested` event with no
resolution. A session that is genuinely mid-tool stays `active`; only a stalled
one becomes `waiting`.

## Plain-language summaries

Every row carries a quick heuristic line for free. Sessions that need your
attention also get a one-sentence status written by Claude Haiku, so you read
"waiting for you to approve a destructive build command" instead of decoding raw
state. Summaries run in the background, are cached per session, and only fire for
sessions that need attention, so an idle fleet costs nothing.

Run fully offline with `--no-model` (or `FLEETWATCH_NO_MODEL=1`) and you keep the
heuristic lines.

## Install

```sh
cd fleetwatch
python3 -m venv .venv
.venv/bin/pip install -e .
```

Python 3.10 or newer. For summaries, set `ANTHROPIC_API_KEY`.

## Usage

```sh
fleetwatch                       # live dashboard
fleetwatch --once                # one text snapshot, then exit
fleetwatch --export-json         # machine-readable snapshot (scripting, remote hosts)
fleetwatch --no-model            # heuristics only, no network
fleetwatch --vendors claude,codex   # watch a subset
fleetwatch --hosts dreamer=luke@dr.eamer.dev   # also watch a VPS over ssh
```

In the dashboard: `q` quit, `r` refresh now, `s` summarize the selected session,
arrows or `j`/`k` to move, and the detail panel shows the summary, the plan, and
the last exchange for whichever session is selected.

## Supported CLIs

| CLI | Reads |
|-----|-------|
| Claude Code | `~/.claude/projects/<cwd>/<uuid>.jsonl` |
| Codex | `~/.codex/sessions/**/rollout-*.jsonl` |
| Grok | `~/.grok/sessions/<cwd>/` (history + per-session events) |
| Gemini | `~/.gemini/tmp/<project>/chats/*.jsonl` |

Gemini CLI has no human-approval gate, so its sessions report active, idle, and
done but never `waiting`. There is no on-disk "blocked on you" signal to read.

## How it works

Four small layers, each independently testable:

1. **Adapters** translate one vendor's files into a single normalized
   `SessionState`. One adapter per vendor, each tested against captured fixture
   transcripts.
2. **The aggregator** polls adapters by file modification time and reads only a
   bounded tail of each transcript, so a 25 MB session file is never read whole.
   It sorts the fleet needs-first.
3. **The summarizer** adds the Haiku sentence for sessions that need you.
4. **The dashboard** (Textual) renders it and refreshes on an interval.

## Configuration

All optional, via environment variables:

| Variable | Default | Effect |
|----------|---------|--------|
| `FLEETWATCH_ACTIVE_WINDOW` | `12` | seconds of quiet before a session stops counting as active |
| `FLEETWATCH_DONE_AFTER` | `1800` | seconds of quiet before idle becomes done |
| `FLEETWATCH_MAX_AGE` | `259200` | drop sessions older than this (3 days) |
| `FLEETWATCH_REFRESH` | `2` | dashboard refresh interval, seconds |
| `FLEETWATCH_MODEL` | `claude-haiku-4-5-20251001` | summary model |
| `FLEETWATCH_VENDORS` | `claude,codex,grok,gemini` | which CLIs to watch |
| `FLEETWATCH_HOSTS` | unset | remote hosts over ssh (`name` or `name=ssh_target`, comma-separated) |
| `FLEETWATCH_NO_MODEL` | unset | set to `1` to disable summaries |

## Watching other machines

`fleetwatch --hosts dreamer=luke@dr.eamer.dev` adds a remote host. Each refresh
runs one `ssh <host> fleetwatch --export-json`, so the remote normalizes its own
sessions and hands back the result: one command per host, the conversation
content stays on the channel you already trust, and a host that goes unreachable
goes stale rather than vanishing from the board. The remote just needs
`fleetwatch` on its `PATH`.

## What is next

Desktop notifications for the sessions that need you, an optional Cursor adapter,
and a PyPI release once summaries move behind an opt-in extra.

## License

MIT.
