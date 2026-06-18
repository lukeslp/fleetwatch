"""Hybrid plain-language summaries.

Every session already carries a free, instant heuristic one-liner (``doing``,
set by its adapter). On top of that, this module asks a fast model (Haiku) for a
single plain-language sentence — but only for sessions that need attention, and
only once per distinct activity timestamp. Results are cached and written back
onto the SessionState in place, off the UI thread, so the dashboard never blocks
on the network and idle fleets cost nothing.

If there is no API key, or the model is disabled, this degrades silently to the
heuristic line (``summary`` simply stays ``None`` and the UI falls back to
``doing``).
"""

from __future__ import annotations

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from . import config
from .models import SessionState

_SYSTEM = (
    "You report the status of a developer's terminal coding session in ONE "
    "plain-language sentence, 25 words max. Say what it is doing and, if it is "
    "blocked, exactly what it needs from the user. No preamble, no markdown, no "
    "quotes — just the sentence."
)


class Summarizer:
    """Background, cached, best-effort model summaries."""

    def __init__(self, model: Optional[str] = None, use_model: Optional[bool] = None):
        self.model = model or config.SUMMARY_MODEL
        use = config.USE_MODEL if use_model is None else use_model
        self._client = None
        self._pool: Optional[ThreadPoolExecutor] = None
        self._cache: dict = {}      # (key, last_activity) -> sentence
        self._inflight: set = set()
        self._lock = threading.Lock()

        if use and os.environ.get("ANTHROPIC_API_KEY"):
            try:
                import anthropic

                self._client = anthropic.Anthropic()
                self._pool = ThreadPoolExecutor(
                    max_workers=2, thread_name_prefix="fw-summary"
                )
            except Exception:
                self._client = None  # any import/auth trouble → heuristics only

    @property
    def enabled(self) -> bool:
        return self._client is not None

    def cached(self, st: SessionState) -> Optional[str]:
        """Return a cached sentence for this session's current activity, if any."""
        with self._lock:
            return self._cache.get((st.key, st.last_activity))

    def request(self, st: SessionState, force: bool = False) -> None:
        """Ensure a summary exists for ``st``, computing it in the background.

        Applies a cached result immediately when available; otherwise schedules
        one model call (deduplicated per activity timestamp unless ``force``).
        """
        if not self.enabled:
            return
        ckey = (st.key, st.last_activity)
        with self._lock:
            if ckey in self._cache:
                st.summary = self._cache[ckey]
                if not force:
                    return
            if ckey in self._inflight:
                return
            self._inflight.add(ckey)
        assert self._pool is not None
        self._pool.submit(self._run, st, ckey)

    def drain(self, timeout: float = 6.0) -> None:
        """Block until in-flight summaries finish (or timeout). Used by --once."""
        if not self.enabled:
            return
        end = time.time() + timeout
        while time.time() < end:
            with self._lock:
                if not self._inflight:
                    return
            time.sleep(0.1)

    # --- internals ---

    def _run(self, st: SessionState, ckey) -> None:
        text = None
        try:
            text = self._call_model(st)
        except Exception:
            text = None  # network/model failure → leave heuristic in place
        with self._lock:
            self._inflight.discard(ckey)
            if text:
                self._cache[ckey] = text
        if text:
            st.summary = text

    def _call_model(self, st: SessionState) -> Optional[str]:
        msg = self._client.messages.create(  # type: ignore[union-attr]
            model=self.model,
            max_tokens=120,
            system=_SYSTEM,
            messages=[{"role": "user", "content": self._build_prompt(st)}],
        )
        parts = [
            b.text for b in msg.content if getattr(b, "type", None) == "text"
        ]
        # Collapse to a single clean line: a model that returns multiple text
        # blocks or internal newlines must never read as concatenated summaries.
        out = " ".join(" ".join(parts).split()).strip()
        if len(out) > 240:
            out = out[:239].rstrip() + "…"
        return out or None

    def _build_prompt(self, st: SessionState) -> str:
        todos = "\n".join(f"- [{t.status}] {t.text}" for t in st.todos[:8])
        return (
            f"Vendor: {st.vendor}\n"
            f"Project: {st.project}\n"
            f"State: {st.state}\n"
            f"Needs: {st.needs or '-'}\n"
            f"Heuristic activity: {st.doing or '-'}\n"
            f"Most recent user message: {st.last_user[:400] or '-'}\n"
            f"Most recent agent message: {st.last_agent[:400] or '-'}\n"
            f"Plan / todos:\n{todos or '-'}"
        )
