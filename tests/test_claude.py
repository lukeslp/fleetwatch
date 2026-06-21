"""Tests for the Claude Code adapter.

These run against small sanitized transcript fixtures under
``tests/fixtures/claude/``. To exercise the recency-driven state machine
deterministically we wrap a real ``LocalSource`` in ``FakeSource``, whose
``mtime()`` we set per-test to simulate a session that is fresh (ACTIVE) versus
stale (so WAITING / IDLE / DONE can surface). Everything else (glob, exists,
tailing) is delegated to the real ``LocalSource``, so the actual JSONL parsing
path is the one under test.
"""

from __future__ import annotations

import os
import time

import pytest

from fleetwatcher.adapters.base import LocalSource, SessionRef, Source
from fleetwatcher.adapters.claude import ClaudeAdapter
from fleetwatcher.config import ACTIVE_WINDOW, DONE_AFTER
from fleetwatcher.models import State

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "claude")

WAITING_ID = "11111111-1111-1111-1111-111111111111"
IDLE_ID = "22222222-2222-2222-2222-222222222222"
TODO_ID = "33333333-3333-3333-3333-333333333333"


class FakeSource(Source):
    """A LocalSource with an overridable mtime, to control recency in tests."""

    host = "local"

    def __init__(self, mtime: float):
        self._local = LocalSource()
        self._mtime = mtime

    def set_mtime(self, mtime: float) -> None:
        self._mtime = mtime

    def expand(self, path: str) -> str:
        return self._local.expand(path)

    def exists(self, path: str) -> bool:
        return self._local.exists(path)

    def glob(self, pattern: str) -> list[str]:
        return self._local.glob(pattern)

    def mtime(self, path: str) -> float:
        return self._mtime

    def read_text(self, path: str) -> str:
        return self._local.read_text(path)

    def tail_lines(self, path: str, max_bytes: int = 512_000) -> list[str]:
        return self._local.tail_lines(path, max_bytes)

    def tail_records(self, path: str, max_bytes: int = 512_000) -> list[dict]:
        return self._local.tail_records(path, max_bytes)


def _ref(source: Source, session_id: str) -> SessionRef:
    refs = ClaudeAdapter().discover(source)
    for r in refs:
        if r.session_id == session_id:
            return r
    raise AssertionError(f"fixture session {session_id} not discovered")


def _fresh() -> float:
    return time.time()


def _stale_idle() -> float:
    # Older than ACTIVE_WINDOW but well within DONE_AFTER.
    return time.time() - (ACTIVE_WINDOW + 60)


def _stale_done() -> float:
    return time.time() - (DONE_AFTER + 60)


# --- discover -------------------------------------------------------------

def test_discover_finds_fixture_sessions(monkeypatch):
    """discover() points at our fixtures root and finds all three sessions."""
    # Redirect the adapter's glob/exists at the fixtures tree.
    import fleetwatcher.adapters.claude as mod

    monkeypatch.setattr(mod, "PROJECTS_ROOT", FIXTURES)
    monkeypatch.setattr(mod, "PROJECTS_GLOB", os.path.join(FIXTURES, "*", "*.jsonl"))

    src = FakeSource(_fresh())
    ids = {r.session_id for r in ClaudeAdapter().discover(src)}
    assert WAITING_ID in ids
    assert IDLE_ID in ids
    assert TODO_ID in ids


def test_discover_missing_root_returns_empty():
    """A nonexistent projects root yields [] rather than raising."""
    import fleetwatcher.adapters.claude as mod

    adapter = ClaudeAdapter()
    src = LocalSource()
    # Point at a path that does not exist; discover must be quiet.
    orig_root, orig_glob = mod.PROJECTS_ROOT, mod.PROJECTS_GLOB
    try:
        mod.PROJECTS_ROOT = "/no/such/fleetwatcher/projects/root"
        mod.PROJECTS_GLOB = "/no/such/fleetwatcher/projects/root/*/*.jsonl"
        assert adapter.discover(src) == []
    finally:
        mod.PROJECTS_ROOT, mod.PROJECTS_GLOB = orig_root, orig_glob


# --- read: state machine --------------------------------------------------

@pytest.fixture()
def patched(monkeypatch):
    import fleetwatcher.adapters.claude as mod

    monkeypatch.setattr(mod, "PROJECTS_ROOT", FIXTURES)
    monkeypatch.setattr(mod, "PROJECTS_GLOB", os.path.join(FIXTURES, "*", "*.jsonl"))
    return mod


def test_read_active_when_fresh(patched):
    """A fresh mtime makes the session ACTIVE even with a dangling tool_use."""
    src = FakeSource(_fresh())
    ref = _ref(src, WAITING_ID)
    state = ClaudeAdapter().read(src, ref, None)
    assert state is not None
    assert state.state == State.ACTIVE
    # The dangling tool_use is a Bash command mid-run -> doing reflects it.
    assert state.doing.startswith("running:")
    assert state.needs is None
    assert state.project == "demo"
    assert state.cwd == "/Users/luke/workspace/demo"
    assert state.vendor == "claude"
    assert state.session_id == WAITING_ID


def test_read_waiting_when_dangling_tool_and_stale(patched):
    """Dangling tool_use + stale mtime -> WAITING with a needs phrase."""
    src = FakeSource(_stale_idle())
    ref = _ref(src, WAITING_ID)
    state = ClaudeAdapter().read(src, ref, None)
    assert state is not None
    assert state.state == State.WAITING
    assert state.needs
    assert "tool" in state.needs or "permission" in state.needs
    assert state.last_user  # human prompt captured
    assert state.summary is None


def test_read_idle_when_completed_and_recent(patched):
    """A completed turn that is stale-but-recent -> IDLE."""
    src = FakeSource(_stale_idle())
    ref = _ref(src, IDLE_ID)
    state = ClaudeAdapter().read(src, ref, None)
    assert state is not None
    assert state.state == State.IDLE
    assert state.needs is None
    assert state.last_agent  # final assistant text captured


def test_read_done_when_completed_and_cold(patched):
    """The same completed turn, but cold (older than DONE_AFTER) -> DONE."""
    src = FakeSource(_stale_done())
    ref = _ref(src, IDLE_ID)
    state = ClaudeAdapter().read(src, ref, None)
    assert state is not None
    assert state.state == State.DONE
    assert state.needs is None


# --- read: robustness + todos --------------------------------------------

def test_partial_final_line_does_not_break_read(patched):
    """A truncated final JSON line is skipped; read still succeeds with todos."""
    src = FakeSource(_stale_idle())
    ref = _ref(src, TODO_ID)
    state = ClaudeAdapter().read(src, ref, None)
    assert state is not None
    assert state.state != State.ERROR
    # The valid records still parse; the broken Write tool_use is dropped, so the
    # last real turn (assistant text) governs the state -> IDLE.
    assert state.state == State.IDLE


def test_todos_parse_from_todowrite(patched):
    """TodoWrite input.todos maps onto TodoItem(text, status)."""
    src = FakeSource(_stale_idle())
    ref = _ref(src, TODO_ID)
    state = ClaudeAdapter().read(src, ref, None)
    assert state is not None
    texts = {t.text: t.status for t in state.todos}
    assert texts == {
        "Audit current schema": "completed",
        "Write migration script": "in_progress",
        "Run migration in staging": "pending",
    }


def test_read_never_raises_on_garbage(patched, tmp_path):
    """read() returns an ERROR SessionState instead of raising on bad input."""
    # Point a ref at a file with no parseable records.
    bad = tmp_path / "deadbeef.jsonl"
    bad.write_text("not json at all\n{also not\n", encoding="utf-8")
    src = FakeSource(_stale_idle())
    ref = SessionRef(path=str(bad), session_id="deadbeef", cwd="/tmp/demo")
    state = ClaudeAdapter().read(src, ref, None)
    assert state is not None
    assert state.state == State.ERROR
    assert state.error
