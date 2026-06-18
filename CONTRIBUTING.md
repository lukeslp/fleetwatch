# Contributing to fleetwatch

Thanks for taking a look. Bug reports, fixes, and new vendor adapters are all
welcome.

## Setup

```sh
git clone https://github.com/lukeslp/fleetwatch
cd fleetwatch
python3 -m venv .venv
.venv/bin/pip install -e ".[summaries,dev]"
.venv/bin/python -m pytest -q
```

Python 3.10 or newer. The test suite runs offline and needs no API key.

## Layout

```
src/fleetwatch/
  models.py        SessionState: the one record every layer speaks
  tailer.py        bounded, crash-proof JSONL tail reads
  adapters/
    base.py        Adapter + Source contracts, LocalSource
    claude.py …    one adapter per vendor
  core.py          Aggregator: poll, normalize, sort, dispatch summaries
  summarize.py     optional Claude Haiku summaries (cached, background)
  remote.py        watch other machines over ssh
  render.py        text snapshot (--once)
  tui.py           Textual dashboard
tests/             one test file + fixtures per vendor
```

## Adding a vendor adapter

This is the most common contribution. An adapter turns one tool's on-disk
session files into `SessionState` records. Look at `adapters/claude.py` as a
worked example, then:

1. Create `adapters/<vendor>.py` with a class that subclasses `Adapter`, sets
   `vendor = "<vendor>"`, and implements:
   - `discover(source) -> list[SessionRef]`: find candidate sessions; cheap, it
     runs every refresh; return `[]` when the tool is not installed.
   - `read(source, ref, prev) -> SessionState | None`: build the current state.
2. Reach the filesystem only through `source` (`source.glob`, `source.mtime`,
   `source.tail_records`, and so on), never with `open()` directly. That is what
   lets the same adapter run against a remote host unchanged.
3. `read()` must never raise. On trouble, return a `SessionState` with
   `state=State.ERROR` and a short message in `error`.
4. Map the tool's files to the shared state machine: `active` when the file
   changed within `ACTIVE_WINDOW`, `waiting` when it is genuinely blocked on the
   human, `idle`/`done` by age. Activity always wins over `waiting`.
5. Register the class in `adapters/__init__.py` and add it to the default vendor
   list in `config.py`.
6. Capture one or two small, sanitized fixture transcripts under
   `tests/fixtures/<vendor>/` and add `tests/test_<vendor>.py`. Test at least the
   `waiting` case, an `idle`/`done` case, and a truncated final line.

Reverse-engineer the real files rather than guessing field names, and keep
fixtures free of secrets or private content.

## Style

- Match the surrounding code: small focused modules, docstrings that explain the
  vendor's real file shapes, type hints.
- Keep `discover()` cheap and `read()` total (no exceptions escape).
- Write "LLM", "Claude", or the model name rather than "AI" in user-facing text.

## Pull requests

Run `pytest -q` before opening a PR, describe what changed and how you verified
it, and keep each PR focused on one thing.
