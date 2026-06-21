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


def test_color_off_by_default_is_plain():
    out = render_snapshot(
        [_s("local", vendor="claude", state=State.WAITING, needs="approve")],
        counts={"waiting": 1, "total": 1},
        now=100.0,
    )
    assert "\x1b[" not in out          # no ANSI escapes when color is off
    assert "waiting 1" in out          # and the plain counts read normally


def test_color_on_emits_ansi_without_losing_text():
    sessions = [_s("local", vendor="claude", state=State.WAITING, needs="approve")]
    out = render_snapshot(sessions, counts={"waiting": 1, "total": 1},
                          now=100.0, color=True)
    assert "\x1b[" in out              # ANSI escapes present
    # The underlying text survives around the escape codes (alignment intact).
    for token in ("claude", "waiting", "approve"):
        assert token in out


def test_state_glyphs_present_without_color():
    # The glyph is a color-free channel: the board must read by shape alone,
    # so every state's mark shows up even in plain text.
    sessions = [
        _s("local", project="a", state=State.ACTIVE),
        _s("local", project="b", state=State.WAITING, needs="x"),
        _s("local", project="c", state=State.IDLE),
        _s("local", project="d", state=State.DONE),
        _s("local", project="e", state=State.ERROR),
    ]
    out = render_snapshot(sessions, now=100.0)
    for glyph in ("●", "◆", "✗", "○", "·"):
        assert glyph in out
