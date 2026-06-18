"""Plain-text snapshot rendering for ``fleetwatch --once``.

This module is deliberately dependency-light: it produces a single multi-line
string from a list of :class:`~fleetwatch.models.SessionState`. The TUI uses
Textual, but ``--once`` (and any pipe-to-a-log usage) only needs text, so this
stays importable and runnable with nothing but the standard library.
"""

from __future__ import annotations

import time
from typing import Iterable, Optional

from .models import SessionState, State


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


def _header(counts: Optional[dict], now: float) -> list[str]:
    clock = time.strftime("%H:%M:%S", time.localtime(now))
    if not counts:
        return [f"fleetwatch  {clock}"]
    parts = []
    for name in _COUNT_ORDER:
        if name in counts:
            parts.append(f"{name} {counts[name]}")
    total = counts.get("total")
    summary = "  ".join(parts)
    if total is not None:
        summary = f"{summary}  (total {total})" if summary else f"total {total}"
    return [f"fleetwatch  {clock}  {summary}".rstrip()]


def render_snapshot(
    sessions: Iterable[SessionState],
    counts: Optional[dict] = None,
    now: Optional[float] = None,
) -> str:
    """Return a clean multi-line text table for ``fleetwatch --once``.

    One line per session: vendor, project, state, humanized idle age, a "!"
    when the session needs attention, and the doing/needs text. A header with
    counts is included when ``counts`` is provided. An empty list renders a
    friendly placeholder.
    """
    sessions = list(sessions)
    now = time.time() if now is None else now

    lines = _header(counts, now)

    if not sessions:
        lines.append("")
        lines.append("No active sessions.")
        return "\n".join(lines)

    # Column widths sized to content, with sane caps.
    vendor_w = max(6, *(len(s.vendor) for s in sessions))
    project_w = min(24, max(7, *(len(s.project) for s in sessions)))
    state_w = max(len(str(s.state)) for s in sessions)
    state_w = max(state_w, 5)

    header_row = (
        f"{'VENDOR':<{vendor_w}}  "
        f"{'PROJECT':<{project_w}}  "
        f"{'STATE':<{state_w}}  "
        f"{'IDLE':>4}  "
        f"{'!':<1}  "
        f"WHAT"
    )
    lines.append("")
    lines.append(header_row)
    lines.append("-" * len(header_row))

    for s in sessions:
        age = humanize_age(s.last_activity, now)
        bang = "!" if s.needs_attention else " "
        # Prefer the human-facing "needs" reason when attention is wanted,
        # otherwise the adapter's "doing" one-liner.
        what = s.needs if (s.needs_attention and s.needs) else s.doing
        lines.append(
            f"{_truncate(s.vendor, vendor_w):<{vendor_w}}  "
            f"{_truncate(s.project, project_w):<{project_w}}  "
            f"{str(s.state):<{state_w}}  "
            f"{age:>4}  "
            f"{bang:<1}  "
            f"{_truncate(what, 60)}"
        )

    return "\n".join(lines)
