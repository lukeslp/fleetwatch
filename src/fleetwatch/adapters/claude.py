"""Claude Code adapter.

Claude Code keeps one directory per working tree under ``~/.claude/projects``.
The directory name is the cwd with every path separator (and other awkward
characters) rewritten as ``-`` — e.g. ``/Users/luke/workspace`` becomes
``-Users-luke-workspace``. Inside each directory is one ``<session-uuid>.jsonl``
file per session (plus an occasional ``subagents/`` subdirectory, which we
ignore — those are not top-level sessions).

    ~/.claude/projects/<encoded-cwd>/<session-uuid>.jsonl

Each line is one JSON record. The record shapes this adapter cares about, as
observed in the real transcripts on this machine:

``type`` is the discriminator. The big two are ``"user"`` and ``"assistant"``;
the file is also peppered with bookkeeping records (``"attachment"``,
``"summary"``, ``"system"``, ``"file-history-snapshot"``, ``"ai-title"``,
``"mode"``, ``"last-prompt"``, ...) that have no ``message`` and are skipped.

A message-bearing record carries:

    {
      "type": "user" | "assistant",
      "message": {"role": ..., "content": <str | [block, ...]>},
      "timestamp": "2026-06-17T23:03:50.692Z",   # ISO8601, UTC
      "sessionId": "<uuid>",
      "cwd": "/Users/luke/workspace",
      "uuid": ..., "parentUuid": ...
    }

``message.content`` is either a plain string (a typed human prompt) or a list
of typed blocks. Block ``type`` is one of ``text``, ``thinking``, ``tool_use``,
``tool_result`` (and a few others). A ``tool_use`` block has ``{id, name,
input}``; the matching ``tool_result`` block (which arrives inside a *later*
``user`` record) carries ``tool_use_id`` pointing back at it.

WAITING — the whole point of the tool — is detected from a *dangling* tool_use:
the newest assistant ``tool_use`` whose ``id`` never shows up as a later
``tool_result``'s ``tool_use_id``. If that exists and the file is stale (not
ACTIVE), the agent is parked waiting on a tool to run or a permission to be
granted. While the file is fresh, the same dangling tool_use just means a tool
is mid-flight, so the session is ACTIVE. An ``AskUserQuestion`` tool_use is an
explicit "blocked on the human" signal, and a stale plain-text turn ending in
``?`` means the agent asked a question and is waiting on a reply.

Plans: the current Claude Code builds its task list with ``TaskCreate`` /
``TaskUpdate`` tool calls rather than a single ``TodoWrite`` blob, so this
adapter reads both. ``TaskCreate`` input is ``{subject, description?,
activeForm?}`` and ``TaskUpdate`` input is ``{taskId, status}``; we replay them
in order to reconstruct the live task list. The classic ``TodoWrite`` shape
(``input.todos`` = ``[{content, status, activeForm?}]``) is still honored when
present.
"""

from __future__ import annotations

import os
import time
from datetime import datetime
from typing import Optional

from ..config import ACTIVE_WINDOW, DONE_AFTER
from ..models import SessionState, State, TodoItem
from .base import Adapter, SessionRef, Source

PROJECTS_GLOB = "~/.claude/projects/*/*.jsonl"
PROJECTS_ROOT = "~/.claude/projects"


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


def _decode_dir_cwd(path: str) -> Optional[str]:
    """Best-effort recovery of a cwd from the encoded project directory name.

    ``~/.claude/projects/-Users-luke-workspace/<uuid>.jsonl`` -> ``/Users/luke
    /workspace``. This is lossy: Claude rewrites both ``/`` and literal ``-`` in
    directory names as ``-``, so a real ``clocks-app`` is indistinguishable from
    ``clocks/app``. It is only a fallback for the ``cwd`` field inside records,
    which is exact.
    """
    parts = path.replace("\\", "/").rstrip("/").split("/")
    if len(parts) < 2:
        return None
    name = parts[-2]
    if not name.startswith("-"):
        return None
    return "/" + name[1:].replace("-", "/")


def _content_blocks(record: dict) -> list:
    """The list of typed content blocks for a record, or ``[]`` for string/empty."""
    msg = record.get("message")
    if not isinstance(msg, dict):
        return []
    content = msg.get("content")
    return content if isinstance(content, list) else []


def _content_str(record: dict) -> Optional[str]:
    """The raw string content of a record, when ``message.content`` is a string."""
    msg = record.get("message")
    if not isinstance(msg, dict):
        return None
    content = msg.get("content")
    return content if isinstance(content, str) else None


def _is_command_meta(text: str) -> bool:
    """Slash-command scaffolding the UI shouldn't show as a real human prompt."""
    return ("<command-name>" in text or "<local-command-" in text
            or "<command-message>" in text)


def _record_text(record: dict, role: str) -> str:
    """Human-readable text from a user/assistant record (string or text blocks)."""
    s = _content_str(record)
    if s is not None:
        return "" if _is_command_meta(s) else s
    chunks = []
    for b in _content_blocks(record):
        if isinstance(b, dict) and b.get("type") == "text":
            t = b.get("text")
            if isinstance(t, str):
                chunks.append(t)
    return " ".join(chunks)


def _doing_from_tool_use(block: dict) -> str:
    """A short, human one-liner describing the latest tool the agent invoked."""
    name = block.get("name") or "tool"
    inp = block.get("input") if isinstance(block.get("input"), dict) else {}
    if name in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
        return f"editing {_basename(inp.get('file_path'))}".rstrip()
    if name == "Read":
        return f"reading {_basename(inp.get('file_path'))}".rstrip()
    if name == "Bash":
        cmd = (inp.get("command") or "").strip().replace("\n", " ")
        return f"running: {cmd[:40]}" if cmd else "running a command"
    if name in ("Task", "Agent"):
        return "delegating to subagent"
    if name in ("TodoWrite", "TaskCreate", "TaskUpdate"):
        return "updating plan"
    if name in ("Grep", "Glob"):
        return "searching"
    if name == "AskUserQuestion":
        return "asking a question"
    if name.startswith("mcp__"):
        return f"running {name.split('__')[-1]}"
    return f"running {name}"


def _status_of(item: dict) -> str:
    status = item.get("status")
    if status in ("pending", "in_progress", "completed"):
        return status
    return "pending"


def _todos_from_records(records: list) -> list[TodoItem]:
    """Reconstruct the live task list from the transcript.

    Two shapes are supported, newest-wins:

    * Classic ``TodoWrite`` — ``input.todos`` is the whole list. The most recent
      one replaces everything.
    * Current ``TaskCreate`` / ``TaskUpdate`` — replayed in order. ``TaskCreate``
      adds an item (keyed by position, since ``taskId`` is "1", "2", ... in
      creation order); ``TaskUpdate`` flips an existing item's status.
    """
    todo_write: Optional[list[TodoItem]] = None
    created: list[dict] = []  # ordered {"text", "status"} for TaskCreate/Update

    for rec in records:
        if rec.get("type") != "assistant":
            continue
        for b in _content_blocks(rec):
            if not isinstance(b, dict) or b.get("type") != "tool_use":
                continue
            name = b.get("name")
            inp = b.get("input") if isinstance(b.get("input"), dict) else {}
            if name == "TodoWrite":
                items = inp.get("todos")
                if isinstance(items, list):
                    todo_write = [
                        TodoItem(text=str(it.get("content", "")).strip(),
                                 status=_status_of(it))
                        for it in items
                        if isinstance(it, dict) and it.get("content")
                    ]
            elif name == "TaskCreate":
                subject = (inp.get("subject")
                           or inp.get("activeForm")
                           or inp.get("description") or "").strip()
                if subject:
                    created.append({"text": subject, "status": "pending"})
            elif name == "TaskUpdate":
                tid = inp.get("taskId")
                status = inp.get("status")
                try:
                    idx = int(tid) - 1
                except (TypeError, ValueError):
                    idx = -1
                if 0 <= idx < len(created) and status in (
                    "pending", "in_progress", "completed"
                ):
                    created[idx]["status"] = status

    if todo_write is not None:
        return [t for t in todo_write if t.text]
    return [TodoItem(text=c["text"], status=c["status"]) for c in created if c["text"]]


def _latest_assistant_tool_use(records: list) -> Optional[dict]:
    """The newest assistant ``tool_use`` block in the transcript, or ``None``."""
    for rec in reversed(records):
        if rec.get("type") != "assistant":
            continue
        for b in reversed(_content_blocks(rec)):
            if isinstance(b, dict) and b.get("type") == "tool_use":
                return b
    return None


def _resolved_tool_use_ids(records: list) -> set:
    """Every ``tool_use_id`` that has a matching ``tool_result`` (i.e. completed)."""
    done = set()
    for rec in records:
        for b in _content_blocks(rec):
            if isinstance(b, dict) and b.get("type") == "tool_result":
                tid = b.get("tool_use_id")
                if tid:
                    done.add(tid)
    return done


def _last_role_text(records: list, role: str) -> str:
    """The most recent non-empty human-readable text for ``role``."""
    rtype = "user" if role == "user" else "assistant"
    for rec in reversed(records):
        if rec.get("type") != rtype:
            continue
        text = _record_text(rec, role)
        if text.strip():
            return text
    return ""


def _last_timestamp(records: list) -> Optional[float]:
    for rec in reversed(records):
        ts = _parse_ts(rec.get("timestamp"))
        if ts is not None:
            return ts
    return None


def _cwd_from_records(records: list) -> Optional[str]:
    for rec in reversed(records):
        cwd = rec.get("cwd")
        if isinstance(cwd, str) and cwd:
            return cwd
    return None


class ClaudeAdapter(Adapter):
    vendor = "claude"

    def discover(self, source: Source) -> list[SessionRef]:
        try:
            if not source.exists(PROJECTS_ROOT):
                return []
            refs: list[SessionRef] = []
            for path in source.glob(PROJECTS_GLOB):
                # Ignore subagent transcripts nested under <session>/subagents/.
                norm = path.replace("\\", "/")
                if "/subagents/" in norm:
                    continue
                stem = _basename(path)
                if stem.endswith(".jsonl"):
                    stem = stem[: -len(".jsonl")]
                refs.append(
                    SessionRef(
                        path=path,
                        session_id=stem,
                        cwd=_decode_dir_cwd(path),
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
                project=_basename(ref.cwd) or ref.session_id,
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

        cwd = _cwd_from_records(records) or ref.cwd
        project = _basename(cwd) or ref.session_id

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

        last_ts = _last_timestamp(records)
        last_activity = mtime or last_ts

        last_user = _clip(_last_role_text(records, "user"))
        last_agent = _clip(_last_role_text(records, "assistant"))
        todos = _todos_from_records(records)

        latest_tool = _latest_assistant_tool_use(records)
        resolved = _resolved_tool_use_ids(records)
        dangling_tool = (
            latest_tool is not None
            and latest_tool.get("id") is not None
            and latest_tool.get("id") not in resolved
        )

        doing = ""
        if latest_tool is not None:
            doing = _doing_from_tool_use(latest_tool)
        elif last_agent:
            doing = _clip(last_agent, 80)

        is_active = recency is not None and recency <= ACTIVE_WINDOW
        is_stale = not is_active

        state = State.IDLE
        needs: Optional[str] = None

        if is_active:
            # Activity beats everything. A dangling tool_use here is just a tool
            # mid-run, not a block.
            state = State.ACTIVE
        else:
            asking = (
                latest_tool is not None
                and latest_tool.get("name") == "AskUserQuestion"
            )
            ends_in_question = (
                latest_tool is None
                and bool(last_agent)
                and last_agent.rstrip().endswith("?")
            )
            if dangling_tool and asking:
                state = State.WAITING
                needs = "answer a question"
            elif dangling_tool:
                state = State.WAITING
                needs = "waiting on tool/permission"
            elif ends_in_question:
                state = State.WAITING
                needs = "asked a question"
            else:
                # A completed turn: IDLE while recent, DONE once it goes cold.
                if recency is not None and recency >= DONE_AFTER:
                    state = State.DONE
                else:
                    state = State.IDLE

        return SessionState(
            vendor=self.vendor,
            session_id=ref.session_id,
            project=project,
            cwd=cwd,
            source=getattr(source, "host", "local"),
            last_activity=last_activity,
            state=state,
            doing=doing,
            needs=needs,
            summary=None,
            todos=todos,
            last_user=last_user,
            last_agent=last_agent,
        )
