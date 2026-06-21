"""User-tunable knobs. Environment variables override the defaults so the tool
is easy to script and to point at different setups."""

from __future__ import annotations

import os


def _f(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


# A session counts as ACTIVE if its file changed within this many seconds.
ACTIVE_WINDOW = _f("FLEETWATCHER_ACTIVE_WINDOW", 12)

# A finished session stays IDLE (not DONE) until it has been quiet this long.
DONE_AFTER = _f("FLEETWATCHER_DONE_AFTER", 1800)  # 30 minutes

# Sessions whose last activity is older than this are dropped from view.
MAX_AGE = _f("FLEETWATCHER_MAX_AGE", 60 * 60 * 24 * 3)  # 3 days

# How often the dashboard re-scans, in seconds.
REFRESH_INTERVAL = _f("FLEETWATCHER_REFRESH", 2)

# Model used for plain-language summaries of sessions that need attention.
SUMMARY_MODEL = os.environ.get("FLEETWATCHER_MODEL", "claude-haiku-4-5-20251001")

# Set FLEETWATCHER_NO_MODEL=1 to stay fully offline (heuristic summaries only).
USE_MODEL = os.environ.get("FLEETWATCHER_NO_MODEL", "") == ""

# Which vendor adapters to run.
ENABLED_VENDORS = [
    v.strip()
    for v in os.environ.get("FLEETWATCHER_VENDORS", "claude,codex,grok,gemini").split(",")
    if v.strip()
]

# Remote hosts to watch over ssh, comma-separated. Each entry is `name` or
# `name=ssh_target` (e.g. "dreamer" or "dreamer=luke@dr.eamer.dev").
REMOTE_HOSTS = os.environ.get("FLEETWATCHER_HOSTS", "")
