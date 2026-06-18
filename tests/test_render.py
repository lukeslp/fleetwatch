"""Tests for the text snapshot renderer, incl. the multi-host HOST column."""

from __future__ import annotations

from fleetwatch.models import SessionState, State
from fleetwatch.render import render_snapshot


def _s(source, vendor="claude", project="p", state=State.IDLE, needs=None):
    return SessionState(
        vendor=vendor,
        session_id=f"{source}-{project}",
        project=project,
        source=source,
        state=state,
        needs=needs,
        last_activity=1.0,
    )


def test_empty_snapshot():
    assert "No active sessions." in render_snapshot([], now=100.0)


def test_host_column_hidden_for_single_source():
    out = render_snapshot([_s("local"), _s("local", project="q")], now=100.0)
    assert "HOST" not in out


def test_host_column_shown_for_multiple_sources():
    out = render_snapshot([_s("local"), _s("dreamer", project="q")], now=100.0)
    assert "HOST" in out
    assert "dreamer" in out


def test_needs_flag_rendered():
    out = render_snapshot(
        [_s("local", state=State.WAITING, needs="approve a command")], now=100.0
    )
    assert "!" in out
    assert "approve a command" in out
