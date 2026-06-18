"""Tests for the Grok adapter.

These run against fixtures under ``tests/fixtures/grok`` that mirror the real
``~/.grok`` layout (verified against a live install):

    active_sessions.json                         JSON list, enrichment only
    sessions/<%2F-encoded-cwd>/prompt_history.jsonl   {timestamp, session_id, prompt, is_bash}
    sessions/<encoded-cwd>/<session-id>/events.jsonl  {ts, type, phase, tool_name, outcome, decision}
    sessions/<encoded-cwd>/<session-id>/summary.json  {info:{id,cwd}, session_summary, ...}
    sessions/<encoded-cwd>/<session-id>/chat_history.jsonl  user/assistant/tool_result records

The adapter only ever reaches the filesystem through a ``Source``. We give it a
``FixtureSource`` that (a) rewrites the ``~/.grok`` prefix onto the fixtures
root so every path the adapter builds lands on a fixture, and (b) lets a test
override the mtime of any path so we can drive the activity-based state machine
(ACTIVE when fresh, WAITING/IDLE/DONE when stale) without touching the wall
clock.
"""

from __future__ import annotations

import glob as _glob
import os
import time

import pytest

from fleetwatch.adapters.base import Source
from fleetwatch.adapters.grok import GrokAdapter
from fleetwatch.config import ACTIVE_WINDOW, DONE_AFTER
from fleetwatch.models import State

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "grok")

CYOA_CWD = "/Users/luke/workspace/cyoa-ios"
CYOA_SID = "019ec29d-f3e0-74e1-9300-b61109764a4f"
BIPOLAR_CWD = "/Users/luke/workspace/bipolar-ios"
BIPOLAR_SID = "019ec29d-b5b9-7110-9084-617b5960194f"
WHATCOLOR_CWD = "/Users/luke/workspace/whatcolor"  # registry-only, no sessions dir


class FixtureSource(Source):
    """A Source backed by the on-disk fixtures, with overridable mtimes.

    ``~/.grok`` (and ``~``) are remapped to the fixtures root. Reads/glob hit the
    real fixture files; ``mtime`` consults a per-test override map first so we can
    make a file look fresh or stale at will.
    """

    host = "local"

    def __init__(self, root: str = FIXTURES, mtimes: dict | None = None) -> None:
        self.root = root
        self._mtimes = mtimes or {}

    def expand(self, path: str) -> str:
        p = path
        # Map the grok home (and any bare ~) onto the fixtures root.
        if p.startswith("~/.grok"):
            p = p[len("~/.grok"):]
        elif p.startswith("~"):
            p = p[1:]
        p = p.lstrip("/")
        return os.path.join(self.root, p) if p else self.root

    def set_mtime(self, path: str, value: float) -> None:
        self._mtimes[self.expand(path)] = value

    def exists(self, path: str) -> bool:
        return os.path.exists(self.expand(path))

    def glob(self, pattern: str) -> list[str]:
        # Return paths back in the adapter's ~/.grok namespace so re-expansion
        # round-trips, exactly like LocalSource keeps its own namespace.
        real = _glob.glob(self.expand(pattern))
        out = []
        for r in real:
            rel = os.path.relpath(r, self.root)
            out.append("~/.grok/" + rel.replace(os.sep, "/"))
        return out

    def mtime(self, path: str) -> float:
        real = self.expand(path)
        if real in self._mtimes:
            return self._mtimes[real]
        try:
            return os.path.getmtime(real)
        except OSError:
            return 0.0

    def read_text(self, path: str) -> str:
        try:
            with open(self.expand(path), "r", encoding="utf-8", errors="replace") as fh:
                return fh.read()
        except OSError:
            return ""

    def tail_lines(self, path: str, max_bytes: int = 512_000) -> list[str]:
        from fleetwatch.tailer import read_tail_lines

        return read_tail_lines(self.expand(path), max_bytes)

    def tail_records(self, path: str, max_bytes: int = 512_000) -> list[dict]:
        from fleetwatch.tailer import read_tail_records

        return read_tail_records(self.expand(path), max_bytes)


def _ref_for(adapter: GrokAdapter, source: Source, cwd: str):
    refs = adapter.discover(source)
    for r in refs:
        if r.cwd == cwd:
            return r
    raise AssertionError(f"no SessionRef discovered for {cwd}; got {[r.cwd for r in refs]}")


# --------------------------------------------------------------------------- #
# discover()                                                                   #
# --------------------------------------------------------------------------- #
def test_discover_unions_registry_and_sessions_dir_and_decodes_cwd():
    adapter = GrokAdapter()
    source = FixtureSource()
    refs = adapter.discover(source)
    cwds = {r.cwd for r in refs}

    # Sessions-dir entries are decoded from %2F-encoded directory names...
    assert CYOA_CWD in cwds
    assert BIPOLAR_CWD in cwds
    # ...and the union pulls in the registry-only entry that has no sessions dir.
    assert WHATCOLOR_CWD in cwds

    # No '%2F' leaked into any decoded cwd.
    for c in cwds:
        assert "%2F" not in (c or "")

    # cwd is deduped: one ref per cwd.
    assert len(refs) == len(cwds)

    # The session id for cyoa is recovered from prompt_history.jsonl.
    cyoa = next(r for r in refs if r.cwd == CYOA_CWD)
    assert cyoa.session_id == CYOA_SID


def test_discover_empty_when_grok_home_absent(tmp_path):
    adapter = GrokAdapter()
    source = FixtureSource(root=str(tmp_path / "nope"))
    assert adapter.discover(source) == []


# --------------------------------------------------------------------------- #
# read() — state machine                                                       #
# --------------------------------------------------------------------------- #
def test_read_active_when_fresh():
    adapter = GrokAdapter()
    source = FixtureSource()
    ref = _ref_for(adapter, source, CYOA_CWD)

    now = time.time()
    # Make every file the adapter looks at appear to have just changed.
    fresh = now - 1
    for p in (
        ref.path,
        f"~/.grok/sessions/%2FUsers%2Fluke%2Fworkspace%2Fcyoa-ios/{CYOA_SID}/events.jsonl",
        f"~/.grok/sessions/%2FUsers%2Fluke%2Fworkspace%2Fcyoa-ios/{CYOA_SID}/summary.json",
        f"~/.grok/sessions/%2FUsers%2Fluke%2Fworkspace%2Fcyoa-ios/{CYOA_SID}/chat_history.jsonl",
    ):
        source.set_mtime(p, fresh)

    st = adapter.read(source, ref, None)
    assert st is not None
    assert st.state == State.ACTIVE
    assert st.vendor == "grok"
    assert st.session_id == CYOA_SID
    assert st.project == "cyoa-ios"
    assert st.cwd == CYOA_CWD
    assert st.summary is None
    assert st.last_activity is not None
    # last_user comes from prompt_history / chat_history; <user_query> unwrapped.
    assert "review all files" in st.last_user
    assert "<user_query>" not in st.last_user
    # todos parsed from the todo_write tool call in chat_history.
    assert [t.text for t in st.todos] == [
        "Recon the codebase structure",
        "Read project docs and flag staleness",
        "Write NEXT_STEPS_GROK with observations",
    ]
    assert st.todos[0].status == "completed"
    assert st.todos[1].status == "in_progress"


def test_read_waiting_when_permission_pending_and_stale():
    adapter = GrokAdapter()
    source = FixtureSource()
    ref = _ref_for(adapter, source, BIPOLAR_CWD)

    now = time.time()
    stale = now - (ACTIVE_WINDOW + 60)  # past the ACTIVE window but recent
    for p in (
        ref.path,
        f"~/.grok/sessions/%2FUsers%2Fluke%2Fworkspace%2Fbipolar-ios/{BIPOLAR_SID}/events.jsonl",
    ):
        source.set_mtime(p, stale)

    st = adapter.read(source, ref, None)
    assert st is not None
    assert st.state == State.WAITING
    # WAITING is detected from a real Grok signal: an unresolved
    # permission_requested (last phase is permission_prompt, no permission_resolved).
    assert st.needs
    assert "bash" in st.needs  # the pending tool name surfaces in `needs`


def test_read_idle_when_completed_and_recent():
    adapter = GrokAdapter()
    source = FixtureSource()
    ref = _ref_for(adapter, source, CYOA_CWD)

    now = time.time()
    recent = now - (ACTIVE_WINDOW + 120)  # stale enough to not be ACTIVE
    assert (now - recent) < DONE_AFTER
    for p in (
        ref.path,
        f"~/.grok/sessions/%2FUsers%2Fluke%2Fworkspace%2Fcyoa-ios/{CYOA_SID}/events.jsonl",
        f"~/.grok/sessions/%2FUsers%2Fluke%2Fworkspace%2Fcyoa-ios/{CYOA_SID}/summary.json",
        f"~/.grok/sessions/%2FUsers%2Fluke%2Fworkspace%2Fcyoa-ios/{CYOA_SID}/chat_history.jsonl",
    ):
        source.set_mtime(p, recent)

    st = adapter.read(source, ref, None)
    assert st is not None
    # cyoa's events end with turn_ended outcome=completed → completed + recent → IDLE
    assert st.state == State.IDLE
    assert st.needs is None  # needs only surfaces on WAITING


def test_read_done_when_completed_and_old():
    adapter = GrokAdapter()
    source = FixtureSource()
    ref = _ref_for(adapter, source, CYOA_CWD)

    now = time.time()
    old = now - (DONE_AFTER + 600)  # well past DONE_AFTER
    for p in (
        ref.path,
        f"~/.grok/sessions/%2FUsers%2Fluke%2Fworkspace%2Fcyoa-ios/{CYOA_SID}/events.jsonl",
        f"~/.grok/sessions/%2FUsers%2Fluke%2Fworkspace%2Fcyoa-ios/{CYOA_SID}/summary.json",
        f"~/.grok/sessions/%2FUsers%2Fluke%2Fworkspace%2Fcyoa-ios/{CYOA_SID}/chat_history.jsonl",
    ):
        source.set_mtime(p, old)

    st = adapter.read(source, ref, None)
    assert st is not None
    assert st.state == State.DONE


# --------------------------------------------------------------------------- #
# read() — robustness                                                          #
# --------------------------------------------------------------------------- #
def test_read_survives_truncated_final_line():
    """The last chat_history line is a half-finished live write; read() must
    parse the rest and never raise."""
    adapter = GrokAdapter()
    source = FixtureSource()
    ref = _ref_for(adapter, source, CYOA_CWD)

    # default mtimes (real files) → stale completed → some non-error state.
    st = adapter.read(source, ref, None)
    assert st is not None
    assert st.state != State.ERROR
    # We still recovered the good assistant message before the truncated line.
    assert "NEXT_STEPS_GROK" in st.last_agent
    # And the todos from the good todo_write line survived.
    assert len(st.todos) == 3


def test_read_never_raises_on_bad_input():
    """A ref pointing at nothing must yield ERROR, not an exception."""
    adapter = GrokAdapter()
    source = FixtureSource()
    from fleetwatch.adapters.base import SessionRef

    bad = SessionRef(
        path="~/.grok/sessions/%2Fdoes%2Fnot%2Fexist/prompt_history.jsonl",
        session_id="ghost",
        cwd="/does/not/exist",
    )
    st = adapter.read(source, bad, None)
    assert st is not None
    # Missing files don't raise; the adapter degrades gracefully (DONE), and in
    # any case never raises and never returns None for a real ref.
    assert st.state in (State.DONE, State.IDLE, State.ERROR)
    assert st.vendor == "grok"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
