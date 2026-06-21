"""Tests for the fleetwatch dashboard: text snapshot + Textual app.

pytest-asyncio is not installed, so the async pilot tests wrap their coroutine
in ``asyncio.run`` by hand.
"""

from __future__ import annotations

import asyncio
import time

from fleetwatch.models import SessionState, State, TodoItem
from fleetwatch.render import humanize_age, render_snapshot
from fleetwatch.tui import FleetApp


# --------------------------------------------------------------------------- #
# Fake aggregator covering every State, one WAITING with needs+summary, one    #
# with todos. The list is pre-sorted needs-first (as the real one would be).   #
# --------------------------------------------------------------------------- #
def make_sessions(now: float | None = None) -> list[SessionState]:
    now = time.time() if now is None else now
    waiting = SessionState(
        vendor="claude", session_id="w1", project="fleetwatch",
        cwd="/Users/luke/workspace/fleetwatch", state=State.WAITING,
        last_activity=now - 5,
        doing="ran the test suite",
        needs="approve running pytest?",
        summary="Claude is blocked on a permission prompt and wants to run pytest.",
        last_user="please add the tests",
        last_agent="May I run pytest?",
    )
    error = SessionState(
        vendor="codex", session_id="e1", project="orrery",
        cwd="/Users/luke/workspace/orrery", state=State.ERROR,
        last_activity=now - 45,
        doing="build failed",
        needs="TypeScript build error",
        error="error TS2345: bad argument type",
    )
    active = SessionState(
        vendor="claude", session_id="a1", project="whatcolor",
        cwd="/Users/luke/workspace/whatcolor", state=State.ACTIVE,
        last_activity=now - 1,
        doing="editing ContentView.swift",
        todos=[
            TodoItem("scaffold the picker", "completed"),
            TodoItem("wire up the palette", "in_progress"),
            TodoItem("add a11y labels", "pending"),
        ],
        last_user="add a color picker",
        last_agent="adding it now",
    )
    idle = SessionState(
        vendor="grok", session_id="i1", project="cube",
        cwd="/Users/luke/workspace/cube", state=State.IDLE,
        last_activity=now - 600, doing="waiting for instruction",
    )
    done = SessionState(
        vendor="codex", session_id="d1", project="mandaza",
        cwd="/Users/luke/workspace/mandaza", state=State.DONE,
        last_activity=now - 90000, doing="shipped",
    )
    # needs-first, then last_activity desc
    return [waiting, error, active, idle, done]


class FakeAggregator:
    def __init__(self, sessions: list[SessionState] | None = None) -> None:
        self._sessions = sessions if sessions is not None else make_sessions()
        self.refresh_calls = 0
        self.summary_requests: list = []

    def refresh(self) -> None:
        self.refresh_calls += 1

    def sessions(self) -> list[SessionState]:
        return list(self._sessions)

    def counts(self) -> dict:
        c = {k: 0 for k in ("active", "waiting", "idle", "done", "error")}
        for s in self._sessions:
            c[str(s.state)] += 1
        c["total"] = len(self._sessions)
        return c

    def request_summary(self, key) -> None:
        self.summary_requests.append(key)


# --------------------------------------------------------------------------- #
# humanize_age                                                                 #
# --------------------------------------------------------------------------- #
def test_humanize_age_buckets():
    now = 1_000_000.0
    assert humanize_age(None) == "-"
    assert humanize_age(now - 5, now) == "5s"
    assert humanize_age(now - 90, now) == "1m"
    assert humanize_age(now - 7200, now) == "2h"
    assert humanize_age(now - 2 * 86400, now) == "2d"
    # future timestamp clamps to 0s, never negative
    assert humanize_age(now + 100, now) == "0s"


# --------------------------------------------------------------------------- #
# render_snapshot                                                              #
# --------------------------------------------------------------------------- #
def test_render_snapshot_multiple_states():
    now = 2_000_000.0
    sessions = make_sessions(now)
    counts = FakeAggregator(sessions).counts()
    out = render_snapshot(sessions, counts=counts, now=now)

    # Every vendor and project shows up
    for token in ("claude", "codex", "grok", "fleetwatch", "orrery", "whatcolor",
                  "cube", "mandaza"):
        assert token in out

    # Every state label appears
    for st in ("active", "waiting", "idle", "done", "error"):
        assert st in out

    # Header carries the counts
    assert "waiting 1" in out
    assert "total 5" in out

    # The needs reason is shown for the waiting session, not just "doing"
    assert "approve running pytest?" in out


def test_render_snapshot_marks_needs_attention():
    now = 2_000_000.0
    out = render_snapshot(make_sessions(now), now=now)
    # The "!" marker must be present for waiting/error sessions
    assert "!" in out
    # Line for the waiting session carries both its state and the bang
    waiting_lines = [ln for ln in out.splitlines() if "fleetwatch" in ln and "waiting" in ln]
    assert waiting_lines
    assert "!" in waiting_lines[0]


def test_render_snapshot_empty():
    out = render_snapshot([])
    assert "No active sessions." in out
    # Still safe with counts provided
    out2 = render_snapshot([], counts={"total": 0})
    assert "No active sessions." in out2


# --------------------------------------------------------------------------- #
# Textual App via the pilot                                                    #
# --------------------------------------------------------------------------- #
def test_app_builds_and_populates():
    agg = FakeAggregator()

    async def go():
        app = FleetApp(agg)
        async with app.run_test() as pilot:
            await pilot.pause()
            from textual.widgets import DataTable
            table = app.query_one("#sessions", DataTable)
            # One row per session, six columns
            assert table.row_count == len(agg.sessions())
            assert len(table.columns) == 6

    asyncio.run(go())


def test_app_refresh_and_summary_bindings():
    agg = FakeAggregator()

    async def go():
        app = FleetApp(agg)
        async with app.run_test() as pilot:
            await pilot.pause()

            # 'r' triggers an aggregator refresh
            before = agg.refresh_calls
            await pilot.press("r")
            await pilot.pause()
            assert agg.refresh_calls > before

            # 's' requests a summary for the selected (first / waiting) row
            await pilot.press("s")
            await pilot.pause()
            assert agg.summary_requests, "expected a summary request"
            # selected row is the waiting session at the top
            assert agg.summary_requests[-1] == agg.sessions()[0].key

    asyncio.run(go())


def test_app_cursor_moves_update_detail():
    agg = FakeAggregator()

    async def go():
        app = FleetApp(agg)
        async with app.run_test() as pilot:
            await pilot.pause()
            from textual.widgets import DataTable
            table = app.query_one("#sessions", DataTable)
            assert table.cursor_row == 0
            await pilot.press("j")
            await pilot.pause()
            assert table.cursor_row == 1

    asyncio.run(go())


def test_build_detail_is_provider_and_state_accented():
    from fleetwatch.tui import build_detail
    from fleetwatch.palette import state_style, vendor_style

    s = make_sessions()[0]  # waiting / claude, with needs + summary
    text = build_detail(s)

    # The content is all there.
    plain = text.plain
    for token in (s.project, "vendor", "state", "needs", "summary"):
        assert token in plain

    styles = {str(span.style) for span in text.spans}
    # Panel accents are the vendor color (color-coded by provider)...
    acc = vendor_style(s.vendor)
    assert any(acc in st for st in styles), styles
    # ...and the state line carries the state color.
    assert any(state_style(s.state) in st for st in styles), styles


def test_app_handles_empty_fleet():
    agg = FakeAggregator(sessions=[])

    async def go():
        app = FleetApp(agg)
        async with app.run_test() as pilot:
            await pilot.pause()
            from textual.widgets import DataTable
            table = app.query_one("#sessions", DataTable)
            assert table.row_count == 0
            # 's' with no selection is a no-op, not a crash
            await pilot.press("s")
            await pilot.pause()
            assert agg.summary_requests == []

    asyncio.run(go())
