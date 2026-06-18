"""Small shared helpers for turning raw tool input into human one-liners."""

from __future__ import annotations

import re

_CD_PREFIX = re.compile(r"^cd\s+\S+\s*&&\s*(.+)$", re.DOTALL)


def clean_command(cmd: str, width: int = 48) -> str:
    """Condense a shell command into a short, readable label.

    Strips heredoc bodies, collapses whitespace and newlines, peels a leading
    ``cd <dir> &&`` so the real command shows, and truncates with an ellipsis.
    """
    if not cmd:
        return ""
    s = cmd.replace("\r", " ")
    # Drop heredoc markers and the body after them (pure noise in a status line).
    if "<<" in s:
        s = s.split("<<", 1)[0]
    s = re.sub(r"\s+", " ", s).strip()
    m = _CD_PREFIX.match(s)
    if m:
        s = m.group(1).strip()
    if len(s) > width:
        s = s[: width - 1].rstrip() + "…"
    return s
