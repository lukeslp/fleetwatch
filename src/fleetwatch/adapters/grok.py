"""Grok CLI adapter.

Grok (the xAI CLI) keeps everything under ``~/.grok``. The pieces this adapter
reads, and what each one is good for:

``~/.grok/active_sessions.json``
    A JSON *list* of currently-running session objects. On this machine it is
    almost always ``[]`` — Grok seems to clear it the moment a session exits —
    so it is treated as pure enrichment, never as the source of truth for which
    sessions exist or whether they are live.

``~/.grok/sessions/<url-encoded-cwd>/prompt_history.jsonl``
    One file per working directory. The directory name is the cwd with ``/``
    written as ``%2F`` (URL-encoded), so ``urllib.parse.unquote`` recovers the
    real path. Each line is ``{timestamp, session_id, prompt, is_bash}``. This
    is the spine of discovery and the most reliable recency signal we have: its
    mtime tracks the human typing, and its tail gives us the last user prompt
    and the session id.

``~/.grok/sessions/<encoded-cwd>/<session-id>/events.jsonl``
    The structured per-session event log (``{ts, type, phase, tool_name,
    decision, outcome}``). The tail of this file is where WAITING lives: a
    ``permission_requested`` with no following ``permission_resolved``, or a last
    ``phase_changed`` of ``permission_prompt``, means Grok is blocked on a human
    approving a tool call. A ``turn_ended`` with ``outcome == "completed"`` means
    the agent finished its turn.

``~/.grok/sessions/<encoded-cwd>/<session-id>/summary.json``
    ``{info:{id,cwd}, session_summary, last_active_at, agent_name}`` — a tidy
    one-liner and a precise last-active timestamp used to enrich ``doing``.

``~/.grok/sessions/<encoded-cwd>/<session-id>/chat_history.jsonl``
    The full conversation. User turns carry ``content`` as a list of
    ``{type:"text", text:...}``; assistant turns carry ``content`` as a string
    plus ``tool_calls``. The latest ``todo_write`` tool call holds the plan as
    ``{id, content, status}`` items, which map straight onto ``TodoItem``.

Liveness rule of thumb: because ``active_sessions.json`` is unreliable, recency
is computed from the *newest* of the prompt_history / events / summary mtimes,
and ``active_sessions.json`` + ``events.jsonl`` only refine the state on top of
that.
"""

from __future__ import annotations

import json
import time
from typing import Optional
from urllib.parse import unquote

from ..config import ACTIVE_WINDOW, DONE_AFTER
from ..models import SessionState, State, TodoItem
from .base import Adapter, SessionRef, Source

GROK_HOME = "~/.grok"
SESSIONS_GLOB = "~/.grok/sessions/*/prompt_history.jsonl"
ACTIVE_SESSIONS = "~/.grok/active_sessions.json"


def _clip(text: str, limit: int = 280) -> str:
    """Collapse whitespace and clip to ``limit`` chars for the detail pane."""
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _encoded_dir_name(path: str) -> str:
    """The encoded directory name (last path segment) of a prompt_history path."""
    # path looks like .../sessions/<encoded-cwd>/prompt_history.jsonl
    parts = path.replace("\\", "/").rstrip("/").split("/")
    if len(parts) >= 2:
        return parts[-2]
    return parts[-1] if parts else ""


def _decode_cwd(encoded: str) -> str:
    """Recover a real cwd from a Grok-encoded directory name (``%2F`` -> ``/``)."""
    return unquote(encoded)


def _user_text(content) -> str:
    """Pull human-readable text out of a chat_history ``user`` content payload.

    User content is a list of ``{type, text}`` blocks; assistant content is a
    plain string. We also strip Grok's ``<user_query>`` wrapper when present so
    the dashboard shows the actual prompt, not the XML scaffolding.
    """
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        chunks = []
        for block in content:
            if isinstance(block, dict):
                t = block.get("text") or block.get("content") or ""
                if isinstance(t, str):
                    chunks.append(t)
            elif isinstance(block, str):
                chunks.append(block)
        text = " ".join(chunks)
    else:
        text = ""
    # Unwrap <user_query>...</user_query> if it is the leading element.
    if "<user_query>" in text and "</user_query>" in text:
        start = text.index("<user_query>") + len("<user_query>")
        end = text.index("</user_query>", start)
        text = text[start:end]
    return text


def _todos_from_tool_call(tc: dict) -> list[TodoItem]:
    """Turn a ``todo_write`` tool call into TodoItems, or [] if it isn't one."""
    name = tc.get("name")
    args = tc.get("arguments")
    if name is None:
        fn = tc.get("function") or {}
        name = fn.get("name")
        if args is None:
            args = fn.get("arguments")
    if name != "todo_write":
        return []
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except (ValueError, TypeError):
            return []
    if not isinstance(args, dict):
        return []
    out: list[TodoItem] = []
    for item in args.get("todos", []) or []:
        if not isinstance(item, dict):
            continue
        text = item.get("content") or item.get("text") or ""
        status = item.get("status", "pending")
        if text:
            out.append(TodoItem(text=str(text), status=str(status)))
    return out


class GrokAdapter(Adapter):
    vendor = "grok"

    # ------------------------------------------------------------------ discover
    def discover(self, source: Source) -> list[SessionRef]:
        """Union of (a) sessions named in ``active_sessions.json`` and (b) every
        cwd directory under ``~/.grok/sessions`` holding a ``prompt_history.jsonl``.
        Deduped by cwd. Returns ``[]`` when ``~/.grok`` is absent."""
        if not source.exists(GROK_HOME):
            return []

        # cwd -> SessionRef, so two views of the same directory collapse to one.
        by_cwd: dict[str, SessionRef] = {}

        # (a) enrichment registry: active_sessions.json (usually empty).
        for entry in self._load_active_sessions(source):
            cwd = entry.get("cwd") or entry.get("workdir") or entry.get("directory")
            sid = (
                entry.get("session_id")
                or entry.get("id")
                or entry.get("sessionId")
            )
            if not cwd:
                continue
            cwd = source.expand(str(cwd)) if str(cwd).startswith("~") else str(cwd)
            encoded = self._encode_for_path(cwd)
            history = f"{GROK_HOME}/sessions/{encoded}/prompt_history.jsonl"
            by_cwd[cwd] = SessionRef(
                path=history,
                session_id=str(sid) if sid else self._stable_id(encoded),
                cwd=cwd,
            )

        # (b) the durable truth: every prompt_history.jsonl on disk.
        for history_path in source.glob(SESSIONS_GLOB):
            encoded = _encoded_dir_name(history_path)
            cwd = _decode_cwd(encoded)
            if cwd in by_cwd:
                # Keep the richer registry entry but make sure its path points at
                # a file that actually exists.
                if not source.exists(by_cwd[cwd].path):
                    by_cwd[cwd] = SessionRef(
                        path=history_path,
                        session_id=by_cwd[cwd].session_id,
                        cwd=cwd,
                    )
                continue
            sid = self._latest_session_id(source, history_path) or self._stable_id(
                encoded
            )
            by_cwd[cwd] = SessionRef(path=history_path, session_id=sid, cwd=cwd)

        return list(by_cwd.values())

    # ---------------------------------------------------------------------- read
    def read(
        self, source: Source, ref: SessionRef, prev: Optional[SessionState]
    ) -> Optional[SessionState]:
        """Build the current SessionState for one Grok session. Never raises."""
        try:
            return self._read(source, ref, prev)
        except Exception as exc:  # contract: read() must not raise
            cwd = ref.cwd
            return SessionState(
                vendor=self.vendor,
                session_id=ref.session_id,
                project=self._project(cwd),
                cwd=cwd,
                source=source.host,
                last_activity=None,
                state=State.ERROR,
                error=f"grok read failed: {exc}",
            )

    def _read(
        self, source: Source, ref: SessionRef, prev: Optional[SessionState]
    ) -> Optional[SessionState]:
        now = time.time()
        cwd = ref.cwd or _decode_cwd(_encoded_dir_name(ref.path))

        # Recency: newest of the files we know about for this session. The
        # prompt_history mtime is the floor; events/summary may be fresher.
        encoded = self._encode_for_path(cwd)
        session_dir = f"{GROK_HOME}/sessions/{encoded}/{ref.session_id}"
        events_path = f"{session_dir}/events.jsonl"
        summary_path = f"{session_dir}/summary.json"
        chat_path = f"{session_dir}/chat_history.jsonl"

        mtimes = [source.mtime(ref.path)]
        for p in (events_path, summary_path, chat_path):
            if source.exists(p):
                mtimes.append(source.mtime(p))
        last_activity = max(mtimes) if mtimes else 0.0
        if not last_activity:
            last_activity = source.mtime(ref.path)
        recency = now - last_activity if last_activity else float("inf")

        # --- last user prompt (from prompt_history; fall back to chat_history) ---
        last_user = ""
        history = source.tail_records(ref.path)
        if history:
            last_user = _clip(str(history[-1].get("prompt", "")))

        # --- last agent message + todos (from chat_history) ---
        last_agent = ""
        todos: list[TodoItem] = []
        doing = ""
        if source.exists(chat_path):
            for rec in source.tail_records(chat_path):
                rtype = rec.get("type")
                if rtype == "user":
                    txt = _clip(_user_text(rec.get("content")))
                    if txt:
                        last_user = txt
                elif rtype == "assistant":
                    content = rec.get("content")
                    if isinstance(content, str) and content.strip():
                        last_agent = _clip(content)
                    for tc in rec.get("tool_calls") or []:
                        if isinstance(tc, dict):
                            found = _todos_from_tool_call(tc)
                            if found:
                                todos = found

        # --- summary.json enriches `doing` ---
        completed = False
        if source.exists(summary_path):
            try:
                summ = json.loads(source.read_text(summary_path))
            except (ValueError, TypeError):
                summ = {}
            if isinstance(summ, dict):
                title = summ.get("session_summary") or summ.get("generated_title")
                if title:
                    doing = _clip(str(title), 120)

        # --- WAITING / completion signal from events.jsonl ---
        waiting = False
        needs: Optional[str] = None
        events = source.tail_records(events_path) if source.exists(events_path) else []
        if events:
            waiting, needs, completed = self._scan_events(events)

        # --- active_sessions.json enrichment (rare, but authoritative when set) ---
        reg = self._registry_entry(source, cwd, ref.session_id)
        if reg is not None:
            if self._registry_says_waiting(reg):
                waiting = True
                if not needs:
                    needs = self._registry_need(reg) or "awaiting your input"

        if not doing:
            doing = last_user or last_agent or "grok session"

        # --- state machine: activity wins -----------------------------------
        if recency <= ACTIVE_WINDOW:
            state = State.ACTIVE
        elif waiting:
            state = State.WAITING
            if not needs:
                needs = "awaiting your input"
        elif completed and recency < DONE_AFTER:
            state = State.IDLE
        elif completed:
            state = State.DONE
        elif recency < DONE_AFTER:
            # No explicit completion signal and not fresh: treat as recently idle.
            state = State.IDLE
        else:
            state = State.DONE

        return SessionState(
            vendor=self.vendor,
            session_id=ref.session_id,
            project=self._project(cwd),
            cwd=cwd,
            source=source.host,
            last_activity=last_activity or None,
            state=state,
            doing=doing,
            needs=needs if state == State.WAITING else None,
            summary=None,
            todos=todos,
            last_user=last_user,
            last_agent=last_agent,
        )

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _project(cwd: Optional[str]) -> str:
        if not cwd:
            return "grok"
        base = cwd.replace("\\", "/").rstrip("/").split("/")[-1]
        return base or cwd

    @staticmethod
    def _encode_for_path(cwd: str) -> str:
        """Re-encode a cwd the way Grok names its session directories.

        Grok encodes ``/`` as ``%2F``; other characters are left as-is on the
        machines observed, so a targeted replace round-trips ``_decode_cwd``.
        """
        return cwd.replace("/", "%2F")

    @staticmethod
    def _stable_id(encoded_dir: str) -> str:
        """A deterministic id when no real session id is available, derived from
        the encoded cwd directory name so it stays constant across refreshes."""
        return f"grok:{encoded_dir}"

    def _load_active_sessions(self, source: Source) -> list[dict]:
        if not source.exists(ACTIVE_SESSIONS):
            return []
        raw = source.read_text(ACTIVE_SESSIONS)
        if not raw.strip():
            return []
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            return []
        if isinstance(data, list):
            return [d for d in data if isinstance(d, dict)]
        if isinstance(data, dict):
            # tolerate {"sessions": [...]} just in case the shape varies
            inner = data.get("sessions")
            if isinstance(inner, list):
                return [d for d in inner if isinstance(d, dict)]
            return [data]
        return []

    def _registry_entry(
        self, source: Source, cwd: str, session_id: str
    ) -> Optional[dict]:
        for entry in self._load_active_sessions(source):
            ecwd = entry.get("cwd") or entry.get("workdir") or entry.get("directory")
            esid = (
                entry.get("session_id")
                or entry.get("id")
                or entry.get("sessionId")
            )
            if ecwd and str(ecwd).rstrip("/") == str(cwd).rstrip("/"):
                return entry
            if esid and str(esid) == str(session_id):
                return entry
        return None

    @staticmethod
    def _registry_says_waiting(entry: dict) -> bool:
        status = str(
            entry.get("status") or entry.get("state") or entry.get("phase") or ""
        ).lower()
        if any(
            tok in status
            for tok in ("wait", "await", "block", "permission", "approval", "prompt")
        ):
            return True
        for flag in (
            "awaiting_input",
            "awaiting_approval",
            "needs_input",
            "needs_approval",
            "blocked",
            "waiting_for_user",
        ):
            if bool(entry.get(flag)):
                return True
        return False

    @staticmethod
    def _registry_need(entry: dict) -> Optional[str]:
        for key in ("needs", "reason", "prompt", "question", "pending_tool"):
            val = entry.get(key)
            if val:
                return str(val)
        return None

    def _latest_session_id(
        self, source: Source, history_path: str
    ) -> Optional[str]:
        recs = source.tail_records(history_path)
        for rec in reversed(recs):
            sid = rec.get("session_id")
            if sid:
                return str(sid)
        return None

    @staticmethod
    def _scan_events(events: list[dict]) -> tuple[bool, Optional[str], bool]:
        """Inspect the tail of a per-session events.jsonl.

        Returns ``(waiting, needs, completed)``.

        WAITING is detected from a real Grok signal: either an unmatched
        ``permission_requested`` (a tool call awaiting human approval), or the
        most recent ``phase_changed`` being ``permission_prompt``. ``completed``
        is set when the last ``turn_ended`` reported ``outcome == "completed"``.
        """
        waiting = False
        needs: Optional[str] = None
        completed = False

        pending_permission: Optional[str] = None  # tool_name awaiting a decision
        last_phase: Optional[str] = None
        last_turn_outcome: Optional[str] = None

        for ev in events:
            etype = ev.get("type")
            if etype == "permission_requested":
                pending_permission = ev.get("tool_name") or "a tool"
            elif etype == "permission_resolved":
                pending_permission = None  # the human (or auto-allow) answered
            elif etype == "phase_changed":
                last_phase = ev.get("phase")
            elif etype == "turn_ended":
                last_turn_outcome = ev.get("outcome")
                pending_permission = None  # the turn is over; nothing pending

        if pending_permission is not None:
            waiting = True
            needs = f"approve {pending_permission}"
        elif last_phase == "permission_prompt":
            waiting = True
            needs = "approve a tool call"

        if last_turn_outcome == "completed":
            completed = True

        return waiting, needs, completed
