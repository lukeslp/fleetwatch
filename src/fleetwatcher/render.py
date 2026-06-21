"""Plain-text snapshot rendering for ``fleetwatcher --once``.

This module is deliberately dependency-light: it produces a single multi-line
string from a list of :class:`~fleetwatcher.models.SessionState`. The TUI uses
Textual, but ``--once`` (and any pipe-to-a-log usage) only needs text, so this
stays importable and runnable with nothing but the standard library.
"""

from __future__ import annotations

import time
from typing import Iterable, Optional

from .models import SessionState, State
from .palette import paint, state_glyph, state_style, vendor_style


# Order counts appear in the header line, matching the lifecycle in State.
_COUNT_ORDER = ("active", "waiting", "idle", "done", "error")


def humanize_age(last_activity: Optional[float], now: Optional[float] = None) -> str:
    """Render a seconds-since-epoch timestamp as a compact age (e.g. "5m").

    ``None`` -> "-". Buckets: <60s -> Ns, <1h -> Nm, <1d -> Nh, else Nd.
    """
    if last_activity is None:
        return "-"
    now = time.time() if now is None else now
    delta = now - last_activity
    if delta < 0:
        delta = 0
    if delta < 60:
        return f"{int(delta)}s"
    if delta < 3600:
        return f"{int(delta // 60)}m"
    if delta < 86400:
        return f"{int(delta // 3600)}h"
    return f"{int(delta // 86400)}d"


def _truncate(text: str, width: int) -> str:
    text = (text or "").replace("\n", " ").strip()
    if len(text) <= width:
        return text
    if width <= 1:
        return text[:width]
    return text[: width - 1] + "…"  # ellipsis


def _header(counts: Optional[dict], now: float, color: bool = False) -> list[str]:
    clock = time.strftime("%H:%M:%S", time.localtime(now))
    head = paint("cyan", "fleetwatcher", color)
    clock_s = paint("grey58", clock, color)
    if not counts:
        return [f"{head}  {clock_s}"]
    parts = []
    for name in _COUNT_ORDER:
        if name in counts:
            chunk = f"{name} {counts[name]}"
            if name in State._value2member_map_:
                chunk = paint(state_style(State(name)), chunk, color)
            parts.append(chunk)
    total = counts.get("total")
    summary = "  ".join(parts)
    if total is not None:
        total_s = paint("bold", f"total {total}", color)
        summary = f"{summary}  ({total_s})" if summary else total_s
    return [f"{head}  {clock_s}  {summary}".rstrip()]


def render_snapshot(
    sessions: Iterable[SessionState],
    counts: Optional[dict] = None,
    now: Optional[float] = None,
    color: bool = False,
) -> str:
    """Return a clean multi-line text table for ``fleetwatcher --once``.

    One line per session: vendor, project, state, humanized idle age, a "!"
    when the session needs attention, and the doing/needs text. A header with
    counts is included when ``counts`` is provided. An empty list renders a
    friendly placeholder.

    With ``color=True`` the same state/vendor palette as the dashboard is
    written as ANSI; the CLI turns it on only when stdout is a TTY, so piping
    ``--once`` to a file or a log stays plain text. Column widths are computed
    on the uncolored text, so alignment holds whether or not color is on.
    """
    sessions = list(sessions)
    now = time.time() if now is None else now

    lines = _header(counts, now, color)

    if not sessions:
        lines.append("")
        lines.append("No active sessions.")
        return "\n".join(lines)

    # Only show a host column when watching more than one source, so the
    # single-machine view stays uncluttered.
    sources = {s.source for s in sessions}
    show_host = len(sources) > 1

    # Column widths sized to content, with sane caps.
    host_w = min(10, max(4, *(len(s.source) for s in sessions))) if show_host else 0
    vendor_w = max(6, *(len(s.vendor) for s in sessions))
    project_w = min(24, max(7, *(len(s.project) for s in sessions)))
    state_w = max(len(str(s.state)) for s in sessions)
    state_w = max(state_w, 5)

    host_head = f"{'HOST':<{host_w}}  " if show_host else ""
    header_row = (
        f"{host_head}"
        f"{'VENDOR':<{vendor_w}}  "
        f"{'PROJECT':<{project_w}}  "
        f"  {'STATE':<{state_w}}  "  # two-space gutter under the status glyph
        f"{'IDLE':>4}  "
        f"{'!':<1}  "
        f"WHAT"
    )
    lines.append("")
    lines.append(header_row)
    lines.append("─" * len(header_row))

    for s in sessions:
        age = humanize_age(s.last_activity, now)
        bang = "!" if s.needs_attention else " "
        # Prefer the human-facing "needs" reason when attention is wanted,
        # otherwise the adapter's "doing" one-liner.
        what = s.needs if (s.needs_attention and s.needs) else s.doing

        # Pad to width first, then paint, so the ANSI bytes never throw the
        # column alignment off.
        vendor_c = paint(vendor_style(s.vendor),
                         f"{_truncate(s.vendor, vendor_w):<{vendor_w}}", color)
        state_c = paint(state_style(s.state),
                        f"{state_glyph(s.state)} {str(s.state):<{state_w}}", color)
        age_c = paint("grey58", f"{age:>4}", color)
        bang_c = (paint("bold bright_red", f"{bang:<1}", color)
                  if s.needs_attention else f"{bang:<1}")
        host_cell = ""
        if show_host:
            host_cell = paint("grey58",
                              f"{_truncate(s.source, host_w):<{host_w}}", color) + "  "

        lines.append(
            f"{host_cell}"
            f"{vendor_c}  "
            f"{_truncate(s.project, project_w):<{project_w}}  "
            f"{state_c}  "
            f"{age_c}  "
            f"{bang_c}  "
            f"{_truncate(what, 60)}"
        )

    return "\n".join(lines)
