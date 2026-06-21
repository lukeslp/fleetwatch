"""The one place fleetwatcher decides what color anything is.

Two renderers consume these colors — the Textual dashboard (:mod:`tui`), which
speaks Rich style strings, and the plain-text snapshot (:mod:`render`), which
speaks ANSI for ``--once`` on a terminal. Keeping both mappings here means the
live board and the text view never disagree about what "waiting" looks like.

The scheme is built for color-blind readers: state is carried by luminance and
hue *position*, never red-vs-green alone. ``active`` is cyan (not green) so it
never collides with ``error``'s red on a deuteranope's screen, and ``idle`` /
``done`` recede into grey. Vendor accents live in a separate hue family
(orange, green, magenta, blue) so a vendor tag is never read as a status.
"""

from __future__ import annotations

from .models import State

# Bright primaries for the three states that carry signal — blue is "live",
# yellow is "your move", red is "broken". WAITING and ERROR are bold so they
# jump out of a full table. IDLE and DONE recede into grey. No state leans on a
# red-vs-green distinction, the one pair color-blind readers can't separate.
STATE_STYLE: dict[State, str] = {
    State.ACTIVE: "bright_blue",
    State.WAITING: "bold bright_yellow",
    State.ERROR: "bold bright_red",
    State.IDLE: "grey58",
    State.DONE: "grey42",
}

# A glyph per state: a second, color-free channel. Read by shape alone, the
# board still parses for a reader who can't see the hue — and ● / ○ / · also
# track "presence" (live, idle, faded) at a glance.
STATE_GLYPH: dict[State, str] = {
    State.ACTIVE: "●",
    State.WAITING: "◆",
    State.ERROR: "✗",
    State.IDLE: "○",
    State.DONE: "·",
}

# Per-vendor accent: a hue family of its own (orange, cyan, magenta, violet),
# every one distinct from the state colors so a vendor tag never reads as a
# status. No green here either.
VENDOR_STYLE: dict[str, str] = {
    "claude": "dark_orange",
    "codex": "bright_cyan",
    "grok": "bright_magenta",
    "gemini": "medium_purple1",
}

_DEFAULT = "white"


def state_style(state: State) -> str:
    """Rich style for a session state; white for anything unrecognized."""
    return STATE_STYLE.get(state, _DEFAULT)


def state_glyph(state: State) -> str:
    """Single-character status mark for a state; a space if unrecognized."""
    return STATE_GLYPH.get(state, " ")


def state_label(state: State) -> str:
    """Glyph + name, e.g. ``"● active"`` — the state as it reads in a cell."""
    return f"{state_glyph(state)} {state}"


def vendor_style(vendor: str) -> str:
    """Rich style for a vendor accent; white for an unknown vendor."""
    return VENDOR_STYLE.get((vendor or "").lower(), _DEFAULT)


# --- ANSI for the plain-text snapshot ------------------------------------- #
# SGR codes mirroring the Rich styles above, so `fleetwatcher --once` on a TTY
# carries the same scheme. Unknown styles paint nothing (graceful no-op).
_ANSI: dict[str, str] = {
    "cyan": "36",
    "bright_blue": "94",
    "bold bright_yellow": "1;93",
    "bold bright_red": "1;91",
    "grey58": "38;5;246",
    "grey42": "38;5;240",
    "dark_orange": "38;5;208",
    "bright_cyan": "96",
    "bright_magenta": "95",
    "medium_purple1": "38;5;141",
    "white": "37",
    "dim": "2",
    "bold": "1",
}


def paint(style: str, text: str, enabled: bool = True) -> str:
    """Wrap ``text`` in the ANSI code for ``style`` when ``enabled``.

    Returns ``text`` untouched when disabled or when the style has no ANSI
    mapping, so callers can always route through here and let a non-TTY (or an
    unknown style) degrade to plain text.
    """
    if not enabled:
        return text
    code = _ANSI.get(style)
    if not code:
        return text
    return f"\x1b[{code}m{text}\x1b[0m"
