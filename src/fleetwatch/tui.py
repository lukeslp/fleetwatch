"""Full-screen Textual dashboard for fleetwatch.

Importing this module must never touch the terminal — only :func:`run_tui`
starts the app, so the module stays safe to import headless (CI, ``--export-json``,
tests). The app is built against a *duck-typed* aggregator with this interface::

    agg.refresh() -> None
    agg.sessions() -> list[SessionState]   # already needs-first sorted
    agg.counts() -> dict[str, int]
    agg.request_summary(key) -> None       # optional / may be a no-op

We never import the concrete aggregator here so this module and ``core.py`` can
be written concurrently without colliding.
"""

from __future__ import annotations

import time
from typing import Optional

from rich.text import Text

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import DataTable, Footer, Header, Static

from . import config
from .models import SessionState, State
from .render import humanize_age


# State -> Rich color used for the state cell and the legend.
STATE_COLORS: dict[State, str] = {
    State.ACTIVE: "green",
    State.WAITING: "yellow",
    State.ERROR: "red",
    State.IDLE: "grey58",     # dim / grey
    State.DONE: "grey42",     # dark grey
}

_TODO_GLYPHS = {
    "completed": "✔",
    "in_progress": "▸",
    "pending": "○",
}

_COUNT_ORDER = ("active", "waiting", "idle", "done", "error")


def _state_color(state: State) -> str:
    return STATE_COLORS.get(state, "white")


def _legend() -> Text:
    """One-line color legend for the header subtitle area."""
    legend = Text("legend: ", style="dim")
    first = True
    for st in (State.ACTIVE, State.WAITING, State.IDLE, State.DONE, State.ERROR):
        if not first:
            legend.append("  ")
        first = False
        legend.append(str(st), style=_state_color(st))
    return legend


def _counts_text(counts: Optional[dict]) -> Text:
    if not counts:
        return Text("no counts", style="dim")
    text = Text()
    first = True
    for name in _COUNT_ORDER:
        if name not in counts:
            continue
        if not first:
            text.append("  ")
        first = False
        style = _state_color(State(name)) if name in State._value2member_map_ else "white"
        text.append(f"{name} {counts[name]}", style=style)
    total = counts.get("total")
    if total is not None:
        text.append(f"   total {total}", style="bold")
    return text


def _excerpt(text: str, width: int = 200) -> str:
    text = (text or "").strip().replace("\r", "")
    if len(text) <= width:
        return text
    return text[: width - 1].rstrip() + "…"


class StatusBar(Static):
    """Header strip: counts + last-refresh clock + color legend."""

    def update_status(self, counts: Optional[dict], refreshed_at: float) -> None:
        clock = time.strftime("%H:%M:%S", time.localtime(refreshed_at))
        line = Text()
        line.append_text(_counts_text(counts))
        line.append("    ")
        line.append(f"refreshed {clock}", style="cyan")
        line.append("    ")
        line.append_text(_legend())
        self.update(line)


class DetailPanel(Static):
    """Side panel describing the currently selected session."""

    def show_placeholder(self) -> None:
        self.update(Text("Select a session to see details.", style="dim"))

    def show_session(self, s: SessionState) -> None:
        body = Text()
        body.append(s.project or "(no project)", style="bold")
        if s.cwd:
            body.append(f"\n{s.cwd}", style="dim")
        body.append("\n\n")
        body.append("vendor  ", style="dim")
        body.append(f"{s.vendor}\n")
        body.append("state   ", style="dim")
        body.append(f"{s.state}", style=_state_color(s.state))
        if s.needs_attention:
            body.append("  !", style="bold red")
        body.append("\n")

        if s.needs:
            body.append("needs   ", style="dim")
            body.append(f"{s.needs}\n", style="yellow")

        # Model-written summary, falling back to the adapter's one-liner.
        body.append("\nsummary\n", style="dim")
        body.append(_excerpt(s.summary if s.summary else s.doing) or "(none)")
        body.append("\n")

        if s.todos:
            body.append("\ntodos\n", style="dim")
            for t in s.todos:
                glyph = _TODO_GLYPHS.get(t.status, "○")
                style = "green" if t.status == "completed" else (
                    "yellow" if t.status == "in_progress" else "white"
                )
                body.append(f"  {glyph} ", style=style)
                body.append(f"{t.text}\n")

        if s.last_user:
            body.append("\nlast user\n", style="dim")
            body.append(_excerpt(s.last_user, 160) + "\n")
        if s.last_agent:
            body.append("\nlast agent\n", style="dim")
            body.append(_excerpt(s.last_agent, 160) + "\n")

        if s.error:
            body.append("\nerror\n", style="dim")
            body.append(_excerpt(s.error, 200), style="red")

        self.update(body)


class FleetApp(App):
    """The live dashboard. Build with an aggregator, then ``run()`` it."""

    CSS = """
    Screen { layout: vertical; }
    StatusBar { height: 1; padding: 0 1; background: $panel; }
    #main { height: 1fr; }
    #table { width: 2fr; }
    DetailPanel {
        width: 1fr;
        padding: 0 1;
        border-left: solid $primary;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("r", "refresh_now", "Refresh"),
        Binding("s", "summary", "Summarize"),
        Binding("S", "summarize_all", "Summ. all"),
        Binding("j", "cursor_down", "Down", show=False),
        Binding("k", "cursor_up", "Up", show=False),
    ]

    _COLUMNS = ("vendor", "project", "state", "idle", "!", "doing")

    def __init__(self, agg) -> None:
        super().__init__()
        self._agg = agg
        # Show a host column only when remote hosts are being watched, so the
        # single-machine view stays uncluttered.
        self._show_host = bool(getattr(agg, "hosts", None))
        # Rows in display order; index aligns with DataTable row order. Keys are
        # the session.key tuple so we can re-select the same session after a
        # refresh even if its position changed.
        self._rows: list[SessionState] = []
        self._refreshed_at: float = time.time()

    # --- composition -----------------------------------------------------
    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        yield StatusBar(id="status")
        with Horizontal(id="main"):
            with Vertical(id="table"):
                yield DataTable(id="sessions", cursor_type="row", zebra_stripes=True)
            yield DetailPanel(id="detail")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#sessions", DataTable)
        columns = (("host",) + self._COLUMNS) if self._show_host else self._COLUMNS
        for col in columns:
            table.add_column(col, key=col)
        self.query_one("#detail", DetailPanel).show_placeholder()
        self._reload(initial=True)
        # Live refresh on the configured interval.
        self.set_interval(config.REFRESH_INTERVAL, self._tick)

    # --- data flow -------------------------------------------------------
    def _tick(self) -> None:
        try:
            self._agg.refresh()
        except Exception:  # a flaky scan should never crash the screen
            pass
        self._reload()

    def _reload(self, initial: bool = False) -> None:
        """Repopulate the table from the aggregator, preserving the selection."""
        table = self.query_one("#sessions", DataTable)

        # Remember which session the cursor was on, by stable key.
        selected_key = None
        if not initial and self._rows and table.row_count:
            idx = table.cursor_row
            if idx is not None and 0 <= idx < len(self._rows):
                selected_key = self._rows[idx].key

        sessions = list(self._agg.sessions())
        self._rows = sessions
        self._refreshed_at = time.time()

        table.clear()
        now = self._refreshed_at
        for s in sessions:
            table.add_row(*self._row_cells(s, now), key=self._row_key(s))

        self.query_one("#status", StatusBar).update_status(
            self._safe_counts(), self._refreshed_at
        )

        # Restore the cursor onto the same session when it still exists.
        new_index = 0
        if selected_key is not None:
            for i, s in enumerate(sessions):
                if s.key == selected_key:
                    new_index = i
                    break
        if sessions:
            table.move_cursor(row=new_index)
            self._show_detail(new_index)
        else:
            self.query_one("#detail", DetailPanel).show_placeholder()

    def _safe_counts(self) -> Optional[dict]:
        try:
            return self._agg.counts()
        except Exception:
            return None

    def _row_key(self, s: SessionState) -> str:
        return "|".join(s.key)

    def _row_cells(self, s: SessionState, now: float):
        state_cell = Text(str(s.state), style=_state_color(s.state))
        bang = Text("!", style="bold red") if s.needs_attention else Text("")
        doing = s.needs if (s.needs_attention and s.needs) else s.doing
        cells = [
            s.vendor,
            s.project,
            state_cell,
            humanize_age(s.last_activity, now),
            bang,
            _excerpt(doing, 80),
        ]
        if self._show_host:
            cells.insert(0, s.source)
        return tuple(cells)

    def _show_detail(self, index: int) -> None:
        panel = self.query_one("#detail", DetailPanel)
        if 0 <= index < len(self._rows):
            s = self._rows[index]
            panel.show_session(s)
            self._ensure_summary(s)
        else:
            panel.show_placeholder()

    def _ensure_summary(self, s: SessionState) -> None:
        """Generate a real plain-language summary for the session being viewed,
        so the detail panel shows a sentence rather than the raw ``doing`` line.

        Skipped for ACTIVE sessions (their live ``doing`` is the freshest signal)
        and a no-op once a summary is cached, so browsing costs at most one model
        call per distinct idle/done/waiting session. The summary lands in the
        background and appears on the next refresh tick.
        """
        if s.summary or s.state == State.ACTIVE:
            return
        request = getattr(self._agg, "request_summary", None)
        if not callable(request):
            return
        try:
            request(s.key, force=False)
        except TypeError:
            # Aggregator without the force kwarg (older/fake): fall back.
            try:
                request(s.key)
            except Exception:
                pass
        except Exception:
            pass

    def _selected(self) -> Optional[SessionState]:
        table = self.query_one("#sessions", DataTable)
        idx = table.cursor_row
        if idx is not None and 0 <= idx < len(self._rows):
            return self._rows[idx]
        return None

    # --- events ----------------------------------------------------------
    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        table = self.query_one("#sessions", DataTable)
        idx = table.cursor_row
        if idx is not None:
            self._show_detail(idx)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        table = self.query_one("#sessions", DataTable)
        idx = table.cursor_row
        if idx is not None:
            self._show_detail(idx)

    # --- actions ---------------------------------------------------------
    def action_refresh_now(self) -> None:
        self._tick()

    def action_summary(self) -> None:
        s = self._selected()
        if s is None:
            return
        request = getattr(self._agg, "request_summary", None)
        if callable(request):
            try:
                request(s.key)
            except Exception:
                pass

    def action_summarize_all(self) -> None:
        fn = getattr(self._agg, "summarize_all", None)
        if not callable(fn):
            return
        try:
            n = fn()
        except Exception:
            n = 0
        try:
            if n:
                self.notify(f"Summarizing {n} session{'s' if n != 1 else ''}…")
            else:
                self.notify("Summaries are off (no API key).", severity="warning")
        except Exception:
            pass

    def action_cursor_down(self) -> None:
        table = self.query_one("#sessions", DataTable)
        table.action_cursor_down()

    def action_cursor_up(self) -> None:
        table = self.query_one("#sessions", DataTable)
        table.action_cursor_up()


def run_tui(agg) -> None:
    """Build and run the full-screen dashboard against ``agg``.

    This is the only entry point that touches the terminal.
    """
    FleetApp(agg).run()


# --- demo / manual smoke test ------------------------------------------------
# Guarded so importing this module never builds fixtures or starts the app.
if __name__ == "__main__":
    from .models import TodoItem

    class _FakeAggregator:
        """A fixed fleet covering every State, for ``python -m fleetwatch.tui``."""

        def __init__(self) -> None:
            now = time.time()
            self._sessions = [
                SessionState(
                    vendor="claude", session_id="w1", project="fleetwatch",
                    cwd="/Users/luke/workspace/fleetwatch", state=State.WAITING,
                    last_activity=now - 8,
                    doing="ran the test suite",
                    needs="approve running pytest?",
                    summary="Claude is blocked on a permission prompt: it wants to run the "
                            "fleetwatch test suite and is waiting for you to approve it.",
                    last_user="please add the TUI tests",
                    last_agent="May I run pytest tests/test_tui.py?",
                ),
                SessionState(
                    vendor="codex", session_id="e1", project="orrery",
                    cwd="/Users/luke/workspace/orrery", state=State.ERROR,
                    last_activity=now - 40,
                    doing="build failed",
                    needs="TypeScript build error in orbit.ts",
                    error="error TS2345: Argument of type 'string' is not assignable.",
                    last_user="fix the build",
                    last_agent="The build is failing on orbit.ts line 42.",
                ),
                SessionState(
                    vendor="claude", session_id="a1", project="whatcolor",
                    cwd="/Users/luke/workspace/whatcolor", state=State.ACTIVE,
                    last_activity=now - 2,
                    doing="editing ContentView.swift",
                    todos=[
                        TodoItem("scaffold the picker", "completed"),
                        TodoItem("wire up the palette", "in_progress"),
                        TodoItem("add accessibility labels", "pending"),
                    ],
                    last_user="add a color picker",
                    last_agent="Adding the picker now.",
                ),
                SessionState(
                    vendor="grok", session_id="i1", project="cube",
                    cwd="/Users/luke/workspace/cube", state=State.IDLE,
                    last_activity=now - 600, doing="waiting for next instruction",
                ),
                SessionState(
                    vendor="codex", session_id="d1", project="mandaza",
                    cwd="/Users/luke/workspace/mandaza", state=State.DONE,
                    last_activity=now - 7200, doing="shipped the release",
                ),
            ]

        def refresh(self) -> None:
            pass

        def sessions(self):
            return list(self._sessions)

        def counts(self):
            c = {k: 0 for k in ("active", "waiting", "idle", "done", "error")}
            for s in self._sessions:
                c[str(s.state)] += 1
            c["total"] = len(self._sessions)
            return c

        def request_summary(self, key):
            pass

    run_tui(_FakeAggregator())
