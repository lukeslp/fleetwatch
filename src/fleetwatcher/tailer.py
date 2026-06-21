"""Bounded, crash-proof reads of append-only JSONL transcripts.

Session transcripts can be tens of megabytes (one on this machine is 25 MB) and
are written while we read them, so the final line is frequently a half-finished
write. Everything here reads only a bounded tail and never raises on a partial
final line or a missing file.
"""

from __future__ import annotations

import json
import os
from typing import Any

DEFAULT_TAIL_BYTES = 512_000  # ~0.5 MB of tail captures plenty of recent activity


def read_tail_lines(path: str, max_bytes: int = DEFAULT_TAIL_BYTES) -> list[str]:
    """Return complete, non-empty text lines from the last ``max_bytes`` of a file.

    If we did not start at byte 0, the first (possibly partial) line is dropped.
    Returns ``[]`` for a missing, empty, or unreadable file instead of raising.
    """
    try:
        size = os.path.getsize(path)
    except OSError:
        return []
    if size == 0:
        return []
    start = max(0, size - max_bytes)
    try:
        with open(path, "rb") as fh:
            fh.seek(start)
            chunk = fh.read()
    except OSError:
        return []
    text = chunk.decode("utf-8", errors="replace")
    lines = text.split("\n")
    if start > 0 and lines:
        lines = lines[1:]  # the leading line is truncated; drop it
    return [ln for ln in lines if ln.strip()]


def parse_jsonl_lines(lines: list[str]) -> list[dict[str, Any]]:
    """Parse lines into dicts, silently skipping any that do not parse.

    The last line is often a partial live write, so unparseable lines are
    expected and ignored rather than treated as errors.
    """
    out: list[dict[str, Any]] = []
    for ln in lines:
        try:
            obj = json.loads(ln)
        except (ValueError, TypeError):
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def read_tail_records(path: str, max_bytes: int = DEFAULT_TAIL_BYTES) -> list[dict[str, Any]]:
    """Convenience: tail a file and parse its JSONL lines in one step."""
    return parse_jsonl_lines(read_tail_lines(path, max_bytes))
