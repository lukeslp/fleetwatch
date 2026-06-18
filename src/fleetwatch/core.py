"""The aggregator: poll every adapter, hold the current fleet, sort it.

This is the only place that knows about all the adapters at once. It scans them
on demand (``refresh``), keeps one normalized ``SessionState`` per session,
re-ages quiet sessions cheaply, drops the ones that vanished, and dispatches
plain-language summaries for whatever needs attention. The TUI and the CLI both
talk to exactly this surface:

    refresh()            re-scan; cheap; safe to call on an interval
    sessions()           current fleet, sorted needs-first then most-recent
    counts()             {"active": n, "waiting": n, ..., "total": n}
    request_summary(key) force a model summary for one session (the TUI 's' key)
"""

from __future__ import annotations

import threading
import time
from typing import Optional

from . import config
from .adapters import all_adapters
from .adapters.base import LocalSource, Source
from .models import SessionState, State
from .summarize import Summarizer

# Sort priority: the things that want you, first.
_STATE_ORDER = {
    State.WAITING: 0,
    State.ERROR: 1,
    State.ACTIVE: 2,
    State.IDLE: 3,
    State.DONE: 4,
}

# States that can change as time passes even when the file does not, so they
# must be re-read on every refresh rather than trusting the mtime cache.
_VOLATILE = {State.ACTIVE, State.WAITING}


class Aggregator:
    def __init__(
        self,
        source: Optional[Source] = None,
        summarizer: Optional[Summarizer] = None,
    ):
        self.source = source or LocalSource()
        self.adapters = all_adapters()
        self.summarizer = summarizer if summarizer is not None else Summarizer()
        self._states: dict[tuple, SessionState] = {}
        self._mtimes: dict[tuple, float] = {}
        self._lock = threading.Lock()

    # --- public surface ---

    def refresh(self) -> None:
        now = time.time()
        seen: set[tuple] = set()

        for adapter in self.adapters:
            try:
                refs = adapter.discover(self.source)
            except Exception:
                refs = []  # a broken adapter must never sink the whole scan
            for ref in refs:
                key = (self.source.host, adapter.vendor, ref.session_id)
                mtime = self.source.mtime(ref.path)

                # Skip files too old to care about without ever reading them —
                # this is what keeps a refresh cheap when hundreds of stale
                # transcripts sit on disk. Not adding to `seen` lets eviction
                # drop one that just aged out.
                if mtime and (now - mtime) > config.MAX_AGE:
                    continue

                seen.add(key)
                prev = self._states.get(key)

                unchanged = prev is not None and self._mtimes.get(key) == mtime
                if unchanged and prev.state not in _VOLATILE:
                    self._reage(prev, now)
                    continue

                try:
                    state = adapter.read(self.source, ref, prev)
                except Exception as exc:
                    state = self._error_state(adapter.vendor, ref, str(exc))
                if state is None:
                    continue

                # Carry a model summary across re-reads of the same activity.
                if not state.summary:
                    if prev and prev.summary and prev.last_activity == state.last_activity:
                        state.summary = prev.summary
                    else:
                        cached = self.summarizer.cached(state)
                        if cached:
                            state.summary = cached

                with self._lock:
                    self._states[key] = state
                    self._mtimes[key] = mtime

        self._evict(seen, now)
        self._dispatch_summaries()

    def sessions(self) -> list[SessionState]:
        with self._lock:
            items = list(self._states.values())
        items.sort(
            key=lambda s: (
                _STATE_ORDER.get(s.state, 9),
                -(s.last_activity or 0.0),
            )
        )
        return items

    def counts(self) -> dict:
        with self._lock:
            items = list(self._states.values())
        out = {s.value: 0 for s in State}
        for st in items:
            out[st.state.value] += 1
        out["total"] = len(items)
        return out

    def request_summary(self, key) -> None:
        with self._lock:
            st = self._states.get(tuple(key))
        if st is not None:
            self.summarizer.request(st, force=True)

    # --- internals ---

    def _reage(self, st: SessionState, now: float) -> None:
        """Promote a quiet IDLE session to DONE once it crosses DONE_AFTER,
        without re-reading the file."""
        if (
            st.state == State.IDLE
            and st.last_activity is not None
            and (now - st.last_activity) >= config.DONE_AFTER
        ):
            st.state = State.DONE

    def _evict(self, seen: set, now: float) -> None:
        with self._lock:
            for key in list(self._states):
                st = self._states[key]
                gone = key not in seen
                too_old = (
                    st.last_activity is not None
                    and (now - st.last_activity) > config.MAX_AGE
                )
                if gone or too_old:
                    self._states.pop(key, None)
                    self._mtimes.pop(key, None)

    def _dispatch_summaries(self) -> None:
        if not self.summarizer.enabled:
            return
        for st in self.sessions():
            if st.needs_attention and not st.summary:
                self.summarizer.request(st)

    @staticmethod
    def _error_state(vendor: str, ref, msg: str) -> SessionState:
        return SessionState(
            vendor=vendor,
            session_id=ref.session_id,
            project=(ref.cwd or "?").rstrip("/").split("/")[-1] or "?",
            cwd=ref.cwd,
            state=State.ERROR,
            doing="could not read session",
            needs="unreadable transcript",
            error=msg[:200],
        )
