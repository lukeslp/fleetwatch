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
ACTIVE_WINDOW = _f("FLEETWATCH_ACTIVE_WINDOW", 12)

# A finished session stays IDLE (not DONE) until it has been quiet this long.
DONE_AFTER = _f("FLEETWATCH_DONE_AFTER", 1800)  # 30 minutes

# Sessions whose last activity is older than this are dropped from view.
MAX_AGE = _f("FLEETWATCH_MAX_AGE", 60 * 60 * 24 * 3)  # 3 days

# How often the dashboard re-scans, in seconds.
REFRESH_INTERVAL = _f("FLEETWATCH_REFRESH", 2)

# Model used for plain-language summaries of sessions that need attention.
SUMMARY_MODEL = os.environ.get("FLEETWATCH_MODEL", "claude-haiku-4-5-20251001")

# Set FLEETWATCH_NO_MODEL=1 to stay fully offline (heuristic summaries only).
USE_MODEL = os.environ.get("FLEETWATCH_NO_MODEL", "") == ""

# Which vendor adapters to run.
ENABLED_VENDORS = [
    v.strip()
    for v in os.environ.get("FLEETWATCH_VENDORS", "claude,codex,grok").split(",")
    if v.strip()
]
