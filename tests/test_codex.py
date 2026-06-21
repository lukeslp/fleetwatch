"""Tests for the Codex adapter.

The fixtures under ``tests/fixtures/codex/`` are sanitized copies of real Codex
rollout records (same field names: ``session_meta``/``payload.id``/``cwd``,
``response_item``/``function_call``/``call_id``, ``event_msg``/``user_message``
/``agent_message``/``task_complete``, and an ``update_plan`` call with
``step``/``status``). They are tiny and contain no secrets.

We drive the adapter through a ``FakeSource`` that maps ``~/.codex`` onto the
fixtures tree and lets each test pin the mtime of a rollout, so ACTIVE vs
WAITING vs IDLE vs DONE are decided deterministically regardless of the clock.
"""

from __future__ import annotations

import os
import time

from fleetwatcher.adapters.base import Source
from fleetwatcher.adapters.codex import CodexAdapter
from fleetwatcher.config import ACTIVE_WINDOW, DONE_AFTER
from fleetwatcher.models import State
from fleetwatcher.tailer import read_tail_lines, read_tail_records

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "codex")

SID_BASE = "019ed000-1111-7000-aaaa-000000000001"   # ACTIVE/IDLE/DONE rollout
SID_WAIT = "019ed000-1111-7000-aaaa-000000000002"   # dangling exec call
SID_TRUNC = "019ed000-1111-7000-aaaa-000000000003"  # truncated final line


class FakeSource(Source):
    """A Source rooted at the fixtures tree with a per-path mtime override.

    Paths that begin with ``~/.codex`` are rewritten under ``FIXTURES``; any
    other path is treated as missing. ``mtimes`` lets a test pin file ages.
    """

    host = "local"

    def __init__(self, mtimes: dict[str, float] | None = None):
        self.mtimes = mtimes or {}

    def _real(self, path: str) -> str:
        p = path
        for prefix in ("~/.codex/", os.path.expanduser("~/.codex/")):
            if p.startswith(prefix):
                return os.path.join(FIXTURES, p[len(prefix):])
        if p in ("~/.codex", os.path.expanduser("~/.codex")):
            return FIXTURES
        return os.path.join(FIXTURES, "__missing__", p)

    def expand(self, path: str) -> str:
        return self._real(path)

    def exists(self, path: str) -> bool:
        return os.path.exists(self._real(path))

    def glob(self, pattern: str) -> list[str]:
        import glob as _glob

        # Return the original-style ~/.codex paths so refs stay vendor-shaped.
        real = self._real(pattern)
        out = []
        for hit in _glob.glob(real, recursive=True):
            rel = os.path.relpath(hit, FIXTURES)
            out.append(os.path.join("~/.codex", rel))
        return sorted(out)

    def mtime(self, path: str) -> float:
        if path in self.mtimes:
            return self.mtimes[path]
        real = self._real(path)
        try:
            return os.path.getmtime(real)
        except OSError:
            return 0.0

    def read_text(self, path: str) -> str:
        try:
            with open(self._real(path), "r", encoding="utf-8", errors="replace") as fh:
                return fh.read()
        except OSError:
            return ""

    def tail_lines(self, path: str, max_bytes: int = 512_000) -> list[str]:
        return read_tail_lines(self._real(path), max_bytes)

    def tail_records(self, path: str, max_bytes: int = 512_000) -> list[dict]:
        return read_tail_records(self._real(path), max_bytes)


def _ref(adapter, source, session_id):
    for ref in adapter.discover(source):
        if session_id in ref.path:
            return ref
    raise AssertionError(f"no ref discovered for {session_id}")


# --------------------------------------------------------------------------- #
# discover()
# --------------------------------------------------------------------------- #
def test_discover_finds_fixture_sessions():
    adapter = CodexAdapter()
    refs = adapter.discover(FakeSource())
    ids = {r.session_id for r in refs}
    assert SID_BASE in ids
    assert SID_WAIT in ids
    assert SID_TRUNC in ids
    # the id is parsed off the rollout filename (uuid tail)
    for r in refs:
        assert r.path.endswith(".jsonl")


def test_discover_absent_sessions_returns_empty(tmp_path):
    class EmptySource(FakeSource):
        def exists(self, path):
            if path == "~/.codex/sessions":
                return False
            return super().exists(path)

    assert CodexAdapter().discover(EmptySource()) == []


# --------------------------------------------------------------------------- #
# read() state machine
# --------------------------------------------------------------------------- #
def test_read_active_when_fresh():
    adapter = CodexAdapter()
    now = time.time()
    source = FakeSource()
    ref = _ref(adapter, source, SID_BASE)
    source.mtimes[ref.path] = now - 1  # within ACTIVE_WINDOW
    st = adapter.read(source, ref, None)
    assert st is not None
    assert st.state is State.ACTIVE
    assert st.vendor == "codex"
    assert st.session_id == SID_BASE
    assert st.cwd == "/Users/luke/workspace/fleetwatcher"
    assert st.project == "fleetwatcher"
    assert st.last_user == "add a codex adapter and run the tests"
    assert "codex adapter is written" in st.last_agent
    # update_plan mapped to todos with real step/status fields
    assert [t.status for t in st.todos] == ["completed", "in_progress", "pending"]
    assert st.todos[1].text == "Write the codex adapter"


def test_read_waiting_on_dangling_exec_when_stale():
    adapter = CodexAdapter()
    now = time.time()
    source = FakeSource()
    ref = _ref(adapter, source, SID_WAIT)
    source.mtimes[ref.path] = now - (ACTIVE_WINDOW + 60)  # stale, not active
    st = adapter.read(source, ref, None)
    assert st is not None
    assert st.state is State.WAITING
    assert st.needs is not None
    assert "approval" in st.needs.lower()
    assert "rm -rf build" in st.needs  # the real pending command surfaces
    assert st.project == "orrery"


def test_read_active_overrides_dangling_call():
    """A dangling call but a fresh mtime is ACTIVE, not WAITING (activity wins)."""
    adapter = CodexAdapter()
    now = time.time()
    source = FakeSource()
    ref = _ref(adapter, source, SID_WAIT)
    source.mtimes[ref.path] = now - 2
    st = adapter.read(source, ref, None)
    assert st.state is State.ACTIVE
    assert st.needs is None


def test_read_idle_when_completed_recently():
    adapter = CodexAdapter()
    now = time.time()
    source = FakeSource()
    ref = _ref(adapter, source, SID_BASE)
    source.mtimes[ref.path] = now - (ACTIVE_WINDOW + 30)  # stale but < DONE_AFTER
    st = adapter.read(source, ref, None)
    assert st.state is State.IDLE
    assert st.needs is None


def test_read_done_when_completed_long_ago():
    adapter = CodexAdapter()
    now = time.time()
    source = FakeSource()
    ref = _ref(adapter, source, SID_BASE)
    source.mtimes[ref.path] = now - (DONE_AFTER + 60)
    st = adapter.read(source, ref, None)
    assert st.state is State.DONE


def test_truncated_final_line_does_not_break_read():
    adapter = CodexAdapter()
    now = time.time()
    source = FakeSource()
    ref = _ref(adapter, source, SID_TRUNC)
    source.mtimes[ref.path] = now - (ACTIVE_WINDOW + 30)
    st = adapter.read(source, ref, None)
    # The last line is a half-written record; it must be skipped, not crash.
    assert st is not None
    assert st.state is State.IDLE
    assert st.project == "whatcolor"
    assert "tapped pixel" in st.last_agent


def test_read_never_raises_on_garbage_path():
    adapter = CodexAdapter()
    from fleetwatcher.adapters.base import SessionRef

    bad = SessionRef(path="~/.codex/sessions/does/not/exist.jsonl", session_id="nope")
    st = adapter.read(FakeSource(), bad, None)
    assert st is not None
    assert st.state is State.ERROR
    assert st.error


def test_truncated_lines_are_serializable():
    adapter = CodexAdapter()
    source = FakeSource()
    ref = _ref(adapter, source, SID_TRUNC)
    st = adapter.read(source, ref, None)
    d = st.to_dict()
    assert d["vendor"] == "codex"
    assert d["state"] in {s.value for s in State}
