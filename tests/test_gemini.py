"""Tests for the Gemini CLI adapter.

The fixtures under ``tests/fixtures/gemini/`` are sanitized copies of real
Gemini CLI transcripts. They mirror the real on-disk field names:

    ~/.gemini/tmp/<project>/.project_root          full cwd, one line
    ~/.gemini/tmp/<project>/chats/session-<ISO>-<8hex>.jsonl
    ~/.gemini/tmp/<project>/logs.json              compact user-prompt log

Records use the real shapes: a ``main`` header (``sessionId``/``projectHash``/
``startTime``/``kind``, no ``type`` key), ``user`` turns whose ``content`` is a
list of ``{text}`` (or ``{functionResponse}`` for echoed tool results),
``gemini`` responses (``content`` str, ``thoughts``, ``toolCalls`` with inline
``result``, ``tokens``, ``model``), ``info`` notices, and a trailing ``$set``.

The adapter only ever reaches the filesystem through a ``Source``. We give it a
``FixtureSource`` that (a) rewrites the ``~/.gemini`` prefix onto the fixtures
root so every path the adapter builds lands on a fixture, and (b) lets a test
override the mtime of any path so the recency-driven state machine (ACTIVE when
fresh, IDLE/DONE when stale) is exercised without touching the wall clock.

WAITING is asserted to never appear: Gemini has no approval gate and resolves
tool calls inline, so there is no reliable "blocked on the human" signal.
"""

from __future__ import annotations

import glob as _glob
import os
import time

from fleetwatcher.adapters.base import Source
from fleetwatcher.adapters.gemini import GeminiAdapter
from fleetwatcher.config import ACTIVE_WINDOW, DONE_AFTER
from fleetwatcher.models import State
from fleetwatcher.tailer import read_tail_lines, read_tail_records

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "gemini")

CYOA_SID = "1276a803-80c7-46b0-8d55-6610b65d0618"   # completed-turn session
ORRERY_SID = "28f49277-1111-4abc-9000-aaaaaaaaaaaa"  # ends on a user turn + truncated tail


class FixtureSource(Source):
    """A Source rooted at the fixtures tree with overridable mtimes.

    The Gemini home (``~/.gemini``) and any bare ``~`` are remapped onto the
    fixtures root, so the adapter's ``~/.gemini/...`` paths resolve to fixtures.
    ``glob()`` returns the original ``~/.gemini``-shaped paths so refs stay
    vendor-shaped. ``mtimes`` lets a test pin file ages.
    """

    host = "local"

    def __init__(self, mtimes: dict | None = None) -> None:
        self.mtimes = mtimes or {}

    def _real(self, path: str) -> str:
        p = path
        for prefix in ("~/.gemini/", os.path.expanduser("~/.gemini/")):
            if p.startswith(prefix):
                return os.path.join(FIXTURES, p[len(prefix):])
        if p in ("~/.gemini", os.path.expanduser("~/.gemini")):
            return FIXTURES
        return os.path.join(FIXTURES, "__missing__", p.lstrip("/~"))

    def expand(self, path: str) -> str:
        return self._real(path)

    def exists(self, path: str) -> bool:
        return os.path.exists(self._real(path))

    def glob(self, pattern: str) -> list[str]:
        real = self._real(pattern)
        out = []
        for hit in _glob.glob(real, recursive=True):
            rel = os.path.relpath(hit, FIXTURES)
            out.append(os.path.join("~/.gemini", rel))
        return sorted(out)

    def mtime(self, path: str) -> float:
        if path in self.mtimes:
            return self.mtimes[path]
        try:
            return os.path.getmtime(self._real(path))
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


def _ref(adapter, source, session_token):
    for ref in adapter.discover(source):
        if session_token in ref.path:
            return ref
    raise AssertionError(f"no ref discovered for {session_token}")


# --------------------------------------------------------------------------- #
# discover()
# --------------------------------------------------------------------------- #
def test_discover_finds_fixture_sessions():
    adapter = GeminiAdapter()
    refs = adapter.discover(FixtureSource())
    # the filename id is the session-<...> stem; the canonical sessionId is read
    # from the header in read(). Locate refs by the 8-hex tail in the filename.
    paths = {r.path for r in refs}
    assert any("session-2026-06-13T20-01-1276a803.jsonl" in p for p in paths)
    assert any("session-2026-06-15T22-15-28f49277.jsonl" in p for p in paths)
    for r in refs:
        assert r.path.endswith(".jsonl")
        assert "/chats/" in r.path.replace("\\", "/")
    # cwd recovered from the sibling .project_root file
    cyoa = _ref(adapter, FixtureSource(), "1276a803")
    assert cyoa.cwd == "/Users/luke/workspace/cyoa-ios"


def test_discover_extracts_sessionid_and_project():
    adapter = GeminiAdapter()
    source = FixtureSource()
    ref = _ref(adapter, source, "1276a803")
    st = adapter.read(source, ref, None)
    assert st is not None
    # sessionId comes from the `main` header, not the filename
    assert st.session_id == CYOA_SID
    # project is the <project> directory under tmp/
    assert st.project == "cyoa-ios"
    assert st.cwd == "/Users/luke/workspace/cyoa-ios"


def test_discover_absent_gemini_returns_empty():
    class EmptySource(FixtureSource):
        def exists(self, path):
            if path == "~/.gemini":
                return False
            return super().exists(path)

    assert GeminiAdapter().discover(EmptySource()) == []


# --------------------------------------------------------------------------- #
# read() state machine (recency-driven; never WAITING)
# --------------------------------------------------------------------------- #
def test_read_active_when_fresh():
    adapter = GeminiAdapter()
    now = time.time()
    source = FixtureSource()
    ref = _ref(adapter, source, "1276a803")
    source.mtimes[ref.path] = now - 1  # within ACTIVE_WINDOW
    st = adapter.read(source, ref, None)
    assert st is not None
    assert st.state is State.ACTIVE
    assert st.vendor == "gemini"
    assert st.session_id == CYOA_SID


def test_read_idle_when_recently_quiet():
    adapter = GeminiAdapter()
    now = time.time()
    source = FixtureSource()
    ref = _ref(adapter, source, "1276a803")
    source.mtimes[ref.path] = now - (ACTIVE_WINDOW + 60)  # stale but < DONE_AFTER
    st = adapter.read(source, ref, None)
    assert st.state is State.IDLE


def test_read_done_when_long_quiet():
    adapter = GeminiAdapter()
    now = time.time()
    source = FixtureSource()
    ref = _ref(adapter, source, "1276a803")
    source.mtimes[ref.path] = now - (DONE_AFTER + 60)  # well past DONE_AFTER
    st = adapter.read(source, ref, None)
    assert st.state is State.DONE


def test_never_emits_waiting():
    """Across every fixture and every recency band, WAITING must never appear."""
    adapter = GeminiAdapter()
    now = time.time()
    bands = [now - 1, now - (ACTIVE_WINDOW + 60), now - (DONE_AFTER + 60)]
    for token in ("1276a803", "28f49277"):
        for mtime in bands:
            source = FixtureSource()
            ref = _ref(adapter, source, token)
            source.mtimes[ref.path] = mtime
            st = adapter.read(source, ref, None)
            assert st is not None
            assert st.state is not State.WAITING
            assert st.needs is None


def test_trailing_user_turn_is_thinking_not_waiting():
    """A transcript ending on a user record means the model is still thinking.

    That is waiting-on-model, not on-human: ACTIVE when fresh, never WAITING.
    """
    adapter = GeminiAdapter()
    now = time.time()
    source = FixtureSource()
    ref = _ref(adapter, source, "28f49277")
    source.mtimes[ref.path] = now - 2
    st = adapter.read(source, ref, None)
    assert st.state is State.ACTIVE
    assert st.doing == "thinking"
    assert st.needs is None


# --------------------------------------------------------------------------- #
# content extraction
# --------------------------------------------------------------------------- #
def test_last_user_and_last_agent_extraction():
    adapter = GeminiAdapter()
    now = time.time()
    source = FixtureSource()
    ref = _ref(adapter, source, "1276a803")
    source.mtimes[ref.path] = now - 1
    st = adapter.read(source, ref, None)
    # latest real human prompt (the /init system template is skipped)
    assert st.last_user == "make a GEMINI_NEXT_STEPS.md"
    # latest non-empty model text
    assert "GEMINI_NEXT_STEPS.md" in st.last_agent
    # doing comes from the last gemini record's toolCalls (write_file)
    assert st.doing == "calling write_file"
    # todos are always empty for Gemini
    assert st.todos == []


def test_truncated_final_line_does_not_break_read():
    adapter = GeminiAdapter()
    now = time.time()
    source = FixtureSource()
    ref = _ref(adapter, source, "28f49277")
    source.mtimes[ref.path] = now - 1
    st = adapter.read(source, ref, None)
    assert st is not None
    assert st.state is State.ACTIVE  # did not error out on the half-written line
    # the truncated final gemini line is dropped; the last good user turn surfaces
    assert st.last_user == "make it smoother"
    assert st.last_agent == "Sure, I will add an orbit animation to the renderer."


def test_read_never_raises_on_unreadable():
    adapter = GeminiAdapter()
    from fleetwatcher.adapters.base import SessionRef

    bogus = SessionRef(path="~/.gemini/tmp/nope/chats/session-x.jsonl",
                       session_id="session-x")
    st = adapter.read(FixtureSource(), bogus, None)
    assert st is not None
    assert st.state is State.ERROR
    assert st.error is not None
