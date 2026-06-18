# fleetwatch

One screen for every terminal coding session you have running.

fleetwatch watches the coding CLIs you already run (Claude Code, Codex, Grok,
Gemini) and shows a single live dashboard of what each session is doing and which
ones are waiting on you. It reads each tool's own session files on disk,
read-only. There is nothing to install into those tools, no daemon, no hooks. If
a CLI writes a transcript, fleetwatch can watch it.

```
fleetwatch  17:18:43  active 1  waiting 1  idle 2  done 7  (total 11)

HOST     VENDOR  PROJECT     STATE    IDLE  !  WHAT
local    claude  orrery      active     3s     editing PhaseSpaceCoordinator.swift
local    claude  bipolar     waiting   40s  !  waiting for you to approve: rm -rf build
dreamer  codex   storyblocks idle       4m     finished a turn
dreamer  gemini  hivescape   done       2h     responding
```

Status: early, but in daily use across a local Mac and a VPS. Current release
`0.3.x`.

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

## Install

Not on PyPI yet. Install from source:

```sh
git clone https://github.com/lukeslp/fleetwatch
cd fleetwatch
python3 -m venv .venv
.venv/bin/pip install -e .                 # dashboard only: free, no network
.venv/bin/pip install -e ".[summaries]"    # add plain-language summaries (Claude Haiku)
```

Python 3.10 or newer. The install puts `fleetwatch` and `fw` on the PATH inside
the venv; activate the venv, or symlink `.venv/bin/fleetwatch` onto your PATH.

## Usage

```sh
fleetwatch                       # live dashboard
fleetwatch --once                # one text snapshot, then exit
fleetwatch --export-json         # machine-readable snapshot (scripting, remote hosts)
fleetwatch --no-model            # heuristics only, no network
fleetwatch --vendors claude,codex   # watch a subset
fleetwatch --hosts dreamer=user@host   # also watch another machine over ssh
fleetwatch --export-json --summarize-all   # full report: summarize every session
```

In the dashboard: `q` quit, `r` refresh now, `s` summarize the selected session,
`S` summarize the whole fleet, arrows or `j`/`k` to move. Selecting a session
also summarizes it on its own, and the detail panel shows the summary, the plan,
and the last exchange for whichever session is selected.

## Supported CLIs

| CLI | Reads |
|-----|-------|
| Claude Code | `~/.claude/projects/<cwd>/<uuid>.jsonl` |
| Codex | `~/.codex/sessions/**/rollout-*.jsonl` |
| Grok | `~/.grok/sessions/<cwd>/` (history + per-session events) |
| Gemini | `~/.gemini/tmp/<project>/chats/*.jsonl` |

Gemini CLI has no human-approval gate, so its sessions report active, idle, and
done but never `waiting`. There is no on-disk "blocked on you" signal to read.

Each adapter is small and isolated, so adding a vendor is a contained job. See
[CONTRIBUTING.md](CONTRIBUTING.md).

## Summaries (optional)

Every row carries a quick heuristic line for free. On top of that, fleetwatch can
write a one-sentence plain-language status with Claude Haiku.

Summaries turn on automatically when the `summaries` extra is installed and
`ANTHROPIC_API_KEY` is set. They run for sessions that need attention and for
whichever session you select, in the background, cached per session, so a fleet
you are not looking at costs nothing. Press `S` (or run `--summarize-all`) to
sweep every session at once.

Without the extra or the key, fleetwatch never makes a network call and shows the
heuristic line instead. `--no-model` forces heuristics-only even when summaries
are available.

## Watching other machines

`fleetwatch --hosts dreamer=user@host` adds a remote host. Each refresh runs one
`ssh <host> fleetwatch --export-json`, so the remote normalizes its own sessions
and hands back the result: one command per host, the conversation content stays
on the channel you already trust, and a host that goes unreachable goes stale
rather than vanishing from the board. The remote just needs `fleetwatch` on its
`PATH`. Once more than one host is in view, a `HOST` column appears so you can
tell which machine each session is on.

## Privacy

fleetwatch reads your CLIs' session files read-only and never writes to them. The
dashboard displays short excerpts (the last user and agent messages, the plan)
locally.

A summary is the only thing that ever leaves your machine, and only when you have
opted into summaries (the `summaries` extra plus `ANTHROPIC_API_KEY`): a short
slice of one session's recent activity is sent to Claude Haiku to write a single
sentence. With `--no-model`, or without the extra or key, nothing is sent
anywhere. Remote hosts are read over your own ssh connection; their session
content travels only over that channel.

## How it works

Four small layers, each independently testable:

1. **Adapters** translate one vendor's files into a single normalized
   `SessionState`. One adapter per vendor, each tested against captured fixture
   transcripts.
2. **The aggregator** polls adapters by file modification time and reads only a
   bounded tail of each transcript, so a 25 MB session file is never read whole.
   It sorts the fleet needs-first and merges remote hosts.
3. **The summarizer** adds the Haiku sentence, cached and off the UI thread.
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

## Contributing

Bug reports, vendor adapters, and fixes are welcome. See
[CONTRIBUTING.md](CONTRIBUTING.md) for the setup, the adapter contract, and how
to add a new CLI.

## License

MIT. See [LICENSE](LICENSE).
