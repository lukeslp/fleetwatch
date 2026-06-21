"""Gemini CLI adapter.

The Gemini CLI keeps one directory per working tree under ``~/.gemini/tmp``. The
directory name is the *basename* of the project (not the full encoded cwd, as
Claude does), e.g. a session run in ``/Users/luke/workspace/cyoa-ios`` lands in
``~/.gemini/tmp/cyoa-ios/``. Each such directory holds:

    ~/.gemini/tmp/<project>/.project_root          one line: the full cwd
    ~/.gemini/tmp/<project>/chats/session-<ISO>-<8hex>.jsonl   the transcript(s)
    ~/.gemini/tmp/<project>/logs.json              compact [{...user prompt...}]

There is one ``session-*.jsonl`` per session and one JSON record per line. The
record shapes this adapter cares about, as observed in the real transcripts on
this machine:

* **header / ``main``** — the very first record. It has *no* ``type`` key;
  instead it carries ``{"sessionId", "projectHash", "startTime", "lastUpdated",
  "kind": "main"}``. ``sessionId`` is the canonical id.
* **``user``** — a turn from the human's side. ``content`` is a list. A *real*
  human prompt is ``[{"text": "..."}]``; but Gemini also writes tool results
  back as ``user`` records whose ``content`` is ``[{"functionResponse": {...}}]``
  — those are NOT human text and must be skipped when picking ``last_user``.
* **``gemini``** — a model response: ``content`` is a string (often ``""`` while
  the turn is still tool-calling), plus ``thoughts`` (array), ``toolCalls``
  (array), ``tokens``, ``model``.
* **``info``** — UI/system notices (e.g. "an extension update is available").
  Not a conversation turn; ignored.
* Trailing **``$set``** bookkeeping records (no ``type``) also appear; ignored.

Tool calls are NOT stranded across records the way Claude's are: each entry in a
``gemini`` record's ``toolCalls`` already carries its ``result`` (with an inline
``functionResponse``) and a ``status`` in the *same* record. There is therefore
no dangling-tool_use signal, and Gemini CLI has **no human-approval gate** — a
tool runs without pausing for the user to grant permission.

WAITING — by design this adapter NEVER emits ``State.WAITING``. There is no
reliable "blocked on the human" signal in this format: tool calls resolve inline
(no permission prompt to wait on), and a transcript that ends on a ``user``
record just means the model is still thinking (waiting on the *model*, not on the
human). That is ACTIVE if the file is fresh and IDLE/DONE once it goes cold. The
state machine is purely recency-driven:

    ACTIVE : now - mtime <= ACTIVE_WINDOW
    IDLE   : ACTIVE_WINDOW < now - mtime < DONE_AFTER
    DONE   : now - mtime >= DONE_AFTER
    ERROR  : unreadable / no records

Todos: Gemini has no todo/plan tool in this format (its ``update_topic`` call is
a research-framing note, not a task list), so ``todos`` is always empty.
"""

from __future__ import annotations

import os
import time
from datetime import datetime
from typing import Optional

from ..config import ACTIVE_WINDOW, DONE_AFTER
from ..models import SessionState, State
from .base import Adapter, SessionRef, Source

GEMINI_ROOT = "~/.gemini"
TMP_ROOT = "~/.gemini/tmp"
SESSIONS_GLOB = "~/.gemini/tmp/*/chats/session-*.jsonl"


def _clip(text: str, limit: int = 280) -> str:
    """Collapse whitespace and clip to ``limit`` chars for the detail pane."""
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _basename(path: Optional[str]) -> str:
    if not path:
        return ""
    return os.path.basename(path.replace("\\", "/").rstrip("/"))


def _parse_ts(value) -> Optional[float]:
    """ISO8601 (``...Z``) -> epoch seconds, or ``None`` if unparseable."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _project_dir(path: str) -> Optional[str]:
    """The ``<project>`` directory name for a ``.../tmp/<project>/chats/x.jsonl``.

    That is the parent of ``chats/`` (i.e. the chats dir's parent's basename).
    """
    parts = path.replace("\\", "/").rstrip("/").split("/")
    # .../tmp/<project>/chats/<file>.jsonl -> <project> is parts[-3]
    if len(parts) >= 3 and parts[-2] == "chats":
        return parts[-3]
    return None


def _session_id_from_filename(path: str) -> str:
    """Best-effort session id from ``session-<ISO>-<8hex>.jsonl`` (fallback only)."""
    stem = _basename(path)
    if stem.endswith(".jsonl"):
        stem = stem[: -len(".jsonl")]
    return stem


def _cwd_from_project_root(source: Source, path: str) -> Optional[str]:
    """Read the full cwd from the sibling ``.project_root`` file, if present.

    ``path`` is the session file; ``.project_root`` lives two levels up, beside
    the ``chats/`` directory: ``.../tmp/<project>/.project_root``.
    """
    norm = path.replace("\\", "/").rstrip("/")
    parts = norm.split("/")
    if len(parts) < 3 or parts[-2] != "chats":
        return None
    project_dir = "/".join(parts[:-2])  # drop "chats/<file>.jsonl"
    root_file = project_dir + "/.project_root"
    try:
        if not source.exists(root_file):
            return None
        text = source.read_text(root_file).strip()
    except Exception:
        return None
    return text or None


def _user_text(record: dict) -> str:
    """Human-readable text from a ``user`` record.

    ``content`` is a list; real human prompts carry ``{"text": ...}`` items.
    ``functionResponse`` items (tool results echoed back as a user turn) are not
    human text and contribute nothing.
    """
    content = record.get("content")
    if isinstance(content, str):  # defensive; not seen in the wild
        return content
    if not isinstance(content, list):
        return ""
    chunks = []
    for item in content:
        if isinstance(item, dict) and isinstance(item.get("text"), str):
            chunks.append(item["text"])
    return " ".join(chunks).strip()


def _is_system_prompt(text: str) -> bool:
    """The session-bootstrap prompts the UI shouldn't show as a human turn.

    Gemini seeds a session with a ``<session_context>`` block and ``/init``-style
    instruction templates; treat those as scaffolding, not real prompts.
    """
    head = text.lstrip()[:64]
    return ("<session_context>" in text
            or head.startswith("You are an AI agent")
            or head.startswith("You are Gemini"))


def _gemini_text(record: dict) -> str:
    """The model's visible text for a ``gemini`` record (``content`` string)."""
    content = record.get("content")
    return content if isinstance(content, str) else ""


def _last_user(records: list) -> str:
    """The most recent real human prompt across the transcript."""
    for rec in reversed(records):
        if rec.get("type") != "user":
            continue
        text = _user_text(rec)
        if text and not _is_system_prompt(text):
            return text
    return ""


def _last_agent(records: list) -> str:
    """The most recent non-empty model text across the transcript."""
    for rec in reversed(records):
        if rec.get("type") != "gemini":
            continue
        text = _gemini_text(rec)
        if text.strip():
            return text
    return ""


def _last_user_from_logs(source: Source, session_path: str, session_id: str) -> str:
    """Fallback for ``last_user``: the compact ``logs.json`` beside the project.

    ``logs.json`` is a list of ``{sessionId, messageId, type, message, timestamp}``
    records (the human prompts only). Used only when the transcript tail yields
    no usable prompt (e.g. a long tool-only tail past the 0.5 MB window).
    """
    norm = session_path.replace("\\", "/").rstrip("/")
    parts = norm.split("/")
    if len(parts) < 3 or parts[-2] != "chats":
        return ""
    logs_file = "/".join(parts[:-2]) + "/logs.json"
    try:
        if not source.exists(logs_file):
            return ""
        records = source.tail_records(logs_file)
    except Exception:
        return ""
    # tail_records parses JSONL; logs.json is a single JSON array, so fall back to
    # reading + json-loading it directly if the line parse came back empty.
    if not records:
        try:
            import json as _json

            data = _json.loads(source.read_text(logs_file))
            records = data if isinstance(data, list) else []
        except Exception:
            return ""
    best = ""
    for rec in records:
        if not isinstance(rec, dict):
            continue
        if rec.get("sessionId") not in (None, session_id):
            continue
        msg = rec.get("message")
        if isinstance(msg, str) and msg.strip() and not msg.startswith("/"):
            best = msg  # keep walking; last match wins
    return best


def _doing(records: list) -> str:
    """A short one-liner describing what the session is doing right now.

    Driven by the last meaningful record:

    * last record is a ``user`` turn  -> "thinking" (model is composing a reply)
    * last ``gemini`` record has tool calls -> "calling <fn>"
    * otherwise the model produced text -> "responding"
    """
    last_meaningful = None
    for rec in reversed(records):
        if rec.get("type") in ("user", "gemini"):
            last_meaningful = rec
            break
    if last_meaningful is None:
        return ""
    if last_meaningful.get("type") == "user":
        return "thinking"
    tool_calls = last_meaningful.get("toolCalls")
    if isinstance(tool_calls, list) and tool_calls:
        last_call = tool_calls[-1]
        name = ""
        if isinstance(last_call, dict):
            name = last_call.get("name") or last_call.get("displayName") or ""
        return f"calling {name}".rstrip() if name else "calling a tool"
    return "responding"


def _session_id(records: list, ref: SessionRef) -> str:
    """The canonical id from the ``main`` header, else the filename fallback."""
    for rec in records:
        if rec.get("kind") == "main":
            sid = rec.get("sessionId")
            if isinstance(sid, str) and sid:
                return sid
    return ref.session_id


def _last_timestamp(records: list) -> Optional[float]:
    for rec in reversed(records):
        ts = _parse_ts(rec.get("timestamp"))
        if ts is not None:
            return ts
    return None


class GeminiAdapter(Adapter):
    vendor = "gemini"

    def discover(self, source: Source) -> list[SessionRef]:
        try:
            if not source.exists(GEMINI_ROOT):
                return []
            refs: list[SessionRef] = []
            for path in source.glob(SESSIONS_GLOB):
                norm = path.replace("\\", "/")
                if "/chats/" not in norm:
                    continue
                refs.append(
                    SessionRef(
                        path=path,
                        session_id=_session_id_from_filename(path),
                        cwd=_cwd_from_project_root(source, path),
                    )
                )
            return refs
        except Exception:
            return []

    def read(
        self, source: Source, ref: SessionRef, prev: Optional[SessionState]
    ) -> Optional[SessionState]:
        try:
            return self._read(source, ref)
        except Exception as exc:  # read() must never raise
            return SessionState(
                vendor=self.vendor,
                session_id=ref.session_id,
                project=_project_dir(ref.path) or _basename(ref.cwd) or ref.session_id,
                cwd=ref.cwd,
                source=getattr(source, "host", "local"),
                last_activity=None,
                state=State.ERROR,
                error=f"read failed: {type(exc).__name__}",
            )

    def _read(self, source: Source, ref: SessionRef) -> Optional[SessionState]:
        records = source.tail_records(ref.path)

        now = time.time()
        mtime = source.mtime(ref.path)
        recency = now - mtime if mtime else None

        cwd = ref.cwd or _cwd_from_project_root(source, ref.path)
        project = _project_dir(ref.path) or _basename(cwd) or ref.session_id

        if not records:
            # Empty or wholly unparseable tail — flag rather than fabricate state.
            return SessionState(
                vendor=self.vendor,
                session_id=ref.session_id,
                project=project,
                cwd=cwd,
                source=getattr(source, "host", "local"),
                last_activity=mtime or None,
                state=State.ERROR,
                error="no readable records",
            )

        session_id = _session_id(records, ref)
        last_ts = _last_timestamp(records)
        last_activity = mtime or last_ts

        last_user = _last_user(records)
        if not last_user:
            last_user = _last_user_from_logs(source, ref.path, session_id)
        last_user = _clip(last_user)
        last_agent = _clip(_last_agent(records))
        doing = _doing(records)

        # Purely recency-driven. WAITING is intentionally never emitted: Gemini
        # has no approval gate and resolves tool calls inline, so there is no
        # reliable "blocked on the human" signal to key off of.
        is_active = recency is not None and recency <= ACTIVE_WINDOW
        if is_active:
            state = State.ACTIVE
        elif recency is not None and recency >= DONE_AFTER:
            state = State.DONE
        else:
            state = State.IDLE

        return SessionState(
            vendor=self.vendor,
            session_id=session_id,
            project=project,
            cwd=cwd,
            source=getattr(source, "host", "local"),
            last_activity=last_activity,
            state=state,
            doing=doing,
            needs=None,
            summary=None,
            todos=[],
            last_user=last_user,
            last_agent=last_agent,
        )
