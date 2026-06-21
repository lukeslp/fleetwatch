"""Adapter for OpenAI Codex CLI sessions.

Codex writes one append-only JSONL "rollout" file per session under
``~/.codex/sessions/YYYY/MM/DD/rollout-<ISO>-<uuid>.jsonl``. Every line is a
record ``{"timestamp": "<iso>", "type": <kind>, "payload": {...}}``. The kinds
seen in real rollouts:

* ``session_meta`` — first line; ``payload`` carries ``id`` (the session id),
  ``cwd`` (the working directory), ``cli_version`` and ``source``.
* ``turn_context`` — per-turn settings: ``cwd``, ``approval_policy``,
  ``sandbox_policy``, ``model``.
* ``response_item`` — a model "response item". ``payload.type`` is one of:
    - ``message`` with ``role`` in {developer, user, assistant} and a
      ``content`` list of ``{type: input_text|output_text, text}`` parts.
      Assistant text is the agent's reply; ``user`` parts are often
      instruction wrappers (AGENTS.md / environment_context), so the cleaner
      user signal is the ``event_msg``/``user_message`` event below.
    - ``reasoning`` (usually encrypted, ignored).
    - ``function_call`` with ``name`` (e.g. ``exec_command``, ``update_plan``)
      and a JSON-string ``arguments`` plus a ``call_id``.
    - ``function_call_output`` with the matching ``call_id`` and ``output``.
    - ``custom_tool_call`` / ``custom_tool_call_output`` (e.g. ``apply_patch``).
    - ``web_search_call``.
* ``event_msg`` — UI events. ``payload.type`` includes ``task_started``,
  ``task_complete`` (carries ``last_agent_message``), ``user_message`` (clean
  user text), ``agent_message`` (clean assistant text), ``token_count``,
  ``turn_aborted``, ``patch_apply_end``.

State detection (activity wins):

* ACTIVE — file mtime within ``ACTIVE_WINDOW``.
* WAITING — the agent is paused for the human. Codex run in interactive mode
  pauses on a command/patch approval; on disk this shows up as a *dangling
  tool call*: a ``function_call`` / ``custom_tool_call`` whose ``call_id`` has
  no following ``*_output`` and which is not closed by a ``task_complete`` or
  ``turn_aborted``. When that holds and the file is stale (not ACTIVE), we
  report WAITING with a ``needs`` describing the pending command/patch. (Fully
  non-interactive ``exec`` sessions never strand a call this way; they finish
  with ``task_complete``.)
* IDLE — turn finished (``task_complete`` is the last meaningful record) and
  mtime younger than ``DONE_AFTER``.
* DONE — finished and mtime at least ``DONE_AFTER`` old.
* ERROR — the file could not be read at all.

``discover()`` returns ``[]`` when ``~/.codex/sessions`` is absent. When the
sessions tree is sparse it still works against whatever rollouts exist; the
global ``~/.codex/history.jsonl`` is only a fallback for ``last_user`` text.
"""

from __future__ import annotations

import json
import os
import time
from typing import Optional

from ..config import ACTIVE_WINDOW, DONE_AFTER
from ..models import SessionState, State, TodoItem
from ..util import clean_command
from .base import Adapter, SessionRef, Source

# Codex nests rollouts as sessions/YYYY/MM/DD/rollout-*.jsonl. ``Source.glob``
# does not enable recursive ``**``, so we enumerate the date depth explicitly
# and also catch a flatter layout (older builds / fixtures) just in case.
SESSIONS_DIR = "~/.codex/sessions"
SESSIONS_GLOBS = (
    "~/.codex/sessions/*/*/*/rollout-*.jsonl",  # YYYY/MM/DD
    "~/.codex/sessions/*/rollout-*.jsonl",
    "~/.codex/sessions/rollout-*.jsonl",
)
HISTORY_PATH = "~/.codex/history.jsonl"

_TRUNC = 280


def _clip(text: str, limit: int = _TRUNC) -> str:
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _payload(rec: dict) -> dict:
    p = rec.get("payload")
    return p if isinstance(p, dict) else {}


def _message_text(payload: dict) -> str:
    """Join the text parts of a response_item ``message`` payload."""
    parts = payload.get("content")
    if not isinstance(parts, list):
        return ""
    out = []
    for part in parts:
        if isinstance(part, dict):
            txt = part.get("text")
            if isinstance(txt, str) and txt.strip():
                out.append(txt)
    return "\n".join(out)


# Wrapped, machine-generated "user" turns Codex injects (instructions, context,
# environment). They are not things the human typed, so skip them when hunting
# for the last real user message inside response_item records.
_NOISE_PREFIXES = (
    "<permissions instructions>",
    "<apps_instructions>",
    "<skills_instructions>",
    "<environment_context>",
    "<user_instructions>",
    "# AGENTS.md",
    "## My request for Codex",
)


def _looks_like_noise(text: str) -> bool:
    stripped = text.lstrip()
    return any(stripped.startswith(pfx) for pfx in _NOISE_PREFIXES)


def _short_cmd(arguments: str) -> str:
    """Pull a human-ish command string out of a function_call's JSON args."""
    try:
        args = json.loads(arguments)
    except (ValueError, TypeError):
        return ""
    if not isinstance(args, dict):
        return ""
    for key in ("cmd", "command", "shell", "script"):
        val = args.get(key)
        if isinstance(val, list):
            val = " ".join(str(v) for v in val)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return ""


def _plan_to_todos(arguments: str) -> list[TodoItem]:
    try:
        args = json.loads(arguments)
    except (ValueError, TypeError):
        return []
    plan = args.get("plan") if isinstance(args, dict) else None
    if not isinstance(plan, list):
        return []
    todos: list[TodoItem] = []
    for item in plan:
        if not isinstance(item, dict):
            continue
        text = item.get("step") or item.get("text") or ""
        if not isinstance(text, str) or not text.strip():
            continue
        status = item.get("status") or "pending"
        if status not in ("pending", "in_progress", "completed"):
            status = "pending"
        todos.append(TodoItem(text=_clip(text, 120), status=status))
    return todos


class CodexAdapter(Adapter):
    vendor = "codex"

    def discover(self, source: Source) -> list[SessionRef]:
        if not source.exists(SESSIONS_DIR):
            return []
        refs: list[SessionRef] = []
        seen: set[str] = set()
        for pattern in SESSIONS_GLOBS:
            for path in source.glob(pattern):
                if path in seen:
                    continue
                seen.add(path)
                refs.append(SessionRef(path=path, session_id=self._id_from_path(path)))
        return refs

    @staticmethod
    def _id_from_path(path: str) -> str:
        name = os.path.basename(path)
        if name.endswith(".jsonl"):
            name = name[: -len(".jsonl")]
        # rollout-<ISO timestamp>-<uuid>; the uuid is the stable id tail.
        if name.startswith("rollout-"):
            name = name[len("rollout-"):]
        parts = name.rsplit("-", 5)  # uuid is 5 dash-separated groups
        if len(parts) == 6:
            return "-".join(parts[1:])
        return name

    def read(
        self, source: Source, ref: SessionRef, prev: Optional[SessionState]
    ) -> Optional[SessionState]:
        try:
            return self._read(source, ref, prev)
        except Exception as exc:  # never raise out of read()
            return SessionState(
                vendor=self.vendor,
                session_id=ref.session_id,
                project=os.path.basename(ref.cwd) if ref.cwd else ref.session_id,
                cwd=ref.cwd,
                source=getattr(source, "host", "local"),
                state=State.ERROR,
                error=f"codex read failed: {exc}",
            )

    def _read(
        self, source: Source, ref: SessionRef, prev: Optional[SessionState]
    ) -> Optional[SessionState]:
        now = time.time()
        mtime = source.mtime(ref.path)
        recency = now - mtime

        records = source.tail_records(ref.path)
        if not records:
            # Could be empty/missing/unreadable, or a tail that is one giant
            # half-written line. Distinguish unreadable from genuinely empty.
            if not source.exists(ref.path):
                return SessionState(
                    vendor=self.vendor,
                    session_id=ref.session_id,
                    project=ref.session_id,
                    cwd=ref.cwd,
                    source=getattr(source, "host", "local"),
                    last_activity=mtime or None,
                    state=State.ERROR,
                    error="codex rollout not found",
                )
            return SessionState(
                vendor=self.vendor,
                session_id=ref.session_id,
                project=ref.session_id,
                cwd=ref.cwd,
                source=getattr(source, "host", "local"),
                last_activity=mtime or None,
                state=State.ERROR,
                error="codex rollout had no parseable records",
            )

        # --- identity: session meta if present, else the filename id ---
        session_id = ref.session_id
        cwd = ref.cwd
        for rec in records:
            if rec.get("type") == "session_meta":
                p = _payload(rec)
                if isinstance(p.get("id"), str):
                    session_id = p["id"]
                if isinstance(p.get("cwd"), str):
                    cwd = p["cwd"]
                break
        # turn_context also carries cwd; prefer the latest one we see.
        for rec in records:
            if rec.get("type") == "turn_context":
                c = _payload(rec).get("cwd")
                if isinstance(c, str) and c:
                    cwd = c

        project = os.path.basename(cwd.rstrip("/")) if cwd else session_id

        # --- walk the tail collecting messages, calls, plans, completion ---
        last_user = ""
        last_agent = ""
        todos: list[TodoItem] = []
        open_calls: dict[str, dict] = {}     # call_id -> {name, args}
        last_open_call: Optional[dict] = None
        completed = False                    # task finished / aborted after last call
        last_doing = ""

        for rec in records:
            rtype = rec.get("type")
            p = _payload(rec)
            ptype = p.get("type")

            if rtype == "event_msg":
                if ptype == "user_message":
                    msg = p.get("message")
                    if isinstance(msg, str) and msg.strip():
                        last_user = msg
                elif ptype == "agent_message":
                    msg = p.get("message")
                    if isinstance(msg, str) and msg.strip():
                        last_agent = msg
                elif ptype == "task_complete":
                    completed = True
                    open_calls.clear()
                    last_open_call = None
                    msg = p.get("last_agent_message")
                    if isinstance(msg, str) and msg.strip():
                        last_agent = msg
                elif ptype in ("turn_aborted", "task_started"):
                    # a new turn started, or the previous turn ended: any
                    # call that was dangling is no longer pending.
                    completed = ptype == "turn_aborted"
                    open_calls.clear()
                    last_open_call = None

            elif rtype == "response_item":
                if ptype == "message":
                    role = p.get("role")
                    text = _message_text(p)
                    if not text.strip():
                        continue
                    if role == "assistant":
                        last_agent = text
                        completed = False
                    elif role == "user" and not _looks_like_noise(text):
                        last_user = text
                        completed = False
                elif ptype in ("function_call", "custom_tool_call"):
                    name = p.get("name") or ""
                    call_id = p.get("call_id")
                    arguments = p.get("arguments")
                    if not isinstance(arguments, str):
                        arguments = p.get("input") if isinstance(p.get("input"), str) else ""
                    if name == "update_plan":
                        mapped = _plan_to_todos(arguments)
                        if mapped:
                            todos = mapped
                        # plan updates are not pending approvals
                        continue
                    info = {"name": name, "args": arguments}
                    if isinstance(call_id, str):
                        open_calls[call_id] = info
                    last_open_call = info
                    completed = False
                    if name in ("exec_command", "shell", "local_shell"):
                        cmd = clean_command(_short_cmd(arguments))
                        last_doing = f"running: {cmd}" if cmd else "running a command"
                    elif name == "apply_patch":
                        last_doing = "applying a patch"
                    else:
                        last_doing = f"calling {name}" if name else "calling a tool"
                elif ptype in ("function_call_output", "custom_tool_call_output"):
                    call_id = p.get("call_id")
                    if isinstance(call_id, str):
                        open_calls.pop(call_id, None)
                    last_open_call = None
                elif ptype == "web_search_call":
                    completed = False
                    last_doing = "searching the web"
                elif ptype == "reasoning":
                    if not last_doing:
                        last_doing = "thinking"

        # --- pending (dangling) tool call = the WAITING signal ---
        pending = None
        if open_calls:
            # the most recently opened still-unmatched call
            pending = next(reversed(open_calls.values()))
        elif last_open_call is not None:
            pending = last_open_call

        # --- decide state (activity wins) ---
        needs: Optional[str] = None
        if recency <= ACTIVE_WINDOW:
            state = State.ACTIVE
            doing = last_doing or "working"
        elif pending is not None and not completed:
            state = State.WAITING
            name = pending.get("name") or ""
            if name in ("exec_command", "shell", "local_shell"):
                cmd = clean_command(_short_cmd(pending.get("args", "")))
                needs = (
                    f"waiting on command approval: {cmd}"
                    if cmd
                    else "waiting on command approval"
                )
                doing = f"awaiting approval: {cmd}" if cmd else "awaiting approval"
            elif name == "apply_patch":
                needs = "waiting on patch approval"
                doing = "awaiting patch approval"
            else:
                needs = f"waiting on {name or 'tool'} result"
                doing = "awaiting approval"
        elif recency < DONE_AFTER:
            state = State.IDLE
            doing = last_doing or "finished a turn"
        else:
            state = State.DONE
            doing = last_doing or "finished a turn"

        # --- last_user fallback: global history for this session ---
        if not last_user:
            last_user = self._history_last_user(source, session_id)

        return SessionState(
            vendor=self.vendor,
            session_id=session_id,
            project=project,
            cwd=cwd,
            source=getattr(source, "host", "local"),
            last_activity=mtime or None,
            state=state,
            doing=doing,
            needs=needs,
            summary=None,
            todos=todos,
            last_user=_clip(last_user),
            last_agent=_clip(last_agent),
        )

    @staticmethod
    def _history_last_user(source: Source, session_id: str) -> str:
        """Fallback last_user from the global ~/.codex/history.jsonl."""
        if not source.exists(HISTORY_PATH):
            return ""
        best = ""
        for rec in source.tail_records(HISTORY_PATH):
            if rec.get("session_id") == session_id:
                text = rec.get("text")
                if isinstance(text, str) and text.strip():
                    best = text
        return best
