"""The data contract shared by every adapter, the aggregator, and the UI.

A ``SessionState`` is a normalized snapshot of one CLI coding session, no matter
which vendor produced it. Adapters translate vendor-specific files into this
shape; the aggregator collects them; the TUI renders them. It stays fully
JSON-serializable on purpose: a remote host can export the exact same records
over the wire, which is how VPS support drops in later without reworking this.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class State(str, Enum):
    """Lifecycle of a session, in rough order of how much it wants you."""

    ACTIVE = "active"    # transcript changed very recently; the agent is working
    WAITING = "waiting"  # blocked on the human (permission prompt, a question)
    IDLE = "idle"        # last turn finished recently; likely still open
    DONE = "done"        # last turn finished long ago; effectively over
    ERROR = "error"      # vendor error, or the transcript could not be read

    def __str__(self) -> str:  # so f-strings print "active" not "State.ACTIVE"
        return self.value


@dataclass
class TodoItem:
    text: str
    status: str = "pending"  # pending | in_progress | completed


@dataclass
class SessionState:
    # --- identity ---
    vendor: str                    # "claude" | "codex" | "grok"
    session_id: str
    project: str                   # human label, usually the basename of cwd
    cwd: Optional[str] = None      # full working directory when known
    source: str = "local"          # host label; "local" now, a VPS hostname later

    # --- liveness ---
    last_activity: Optional[float] = None  # epoch seconds (file mtime / last ts)
    state: State = State.IDLE

    # --- what it's doing ---
    doing: str = ""                # heuristic one-liner the adapter fills in
    needs: Optional[str] = None    # human-facing reason it wants attention
    summary: Optional[str] = None  # model-written paragraph, filled in lazily

    # --- context for the detail pane ---
    todos: list[TodoItem] = field(default_factory=list)
    last_user: str = ""
    last_agent: str = ""

    # --- bookkeeping ---
    error: Optional[str] = None

    @property
    def key(self) -> tuple[str, str, str]:
        """Stable identity across refreshes and across hosts."""
        return (self.source, self.vendor, self.session_id)

    @property
    def needs_attention(self) -> bool:
        return self.state in (State.WAITING, State.ERROR)

    def to_dict(self) -> dict:
        return {
            "vendor": self.vendor,
            "session_id": self.session_id,
            "project": self.project,
            "cwd": self.cwd,
            "source": self.source,
            "last_activity": self.last_activity,
            "state": self.state.value,
            "doing": self.doing,
            "needs": self.needs,
            "summary": self.summary,
            "todos": [{"text": t.text, "status": t.status} for t in self.todos],
            "last_user": self.last_user,
            "last_agent": self.last_agent,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "SessionState":
        return cls(
            vendor=d["vendor"],
            session_id=d["session_id"],
            project=d["project"],
            cwd=d.get("cwd"),
            source=d.get("source", "local"),
            last_activity=d.get("last_activity"),
            state=State(d.get("state", "idle")),
            doing=d.get("doing", ""),
            needs=d.get("needs"),
            summary=d.get("summary"),
            todos=[
                TodoItem(text=t["text"], status=t.get("status", "pending"))
                for t in d.get("todos", [])
            ],
            last_user=d.get("last_user", ""),
            last_agent=d.get("last_agent", ""),
            error=d.get("error"),
        )
