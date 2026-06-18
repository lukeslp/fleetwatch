"""The adapter contract and the local-filesystem Source.

Adapters never touch the filesystem directly; they go through a ``Source``.
Today the only Source is ``LocalSource``. A future ``RemoteSource`` (SSH, or a
pushed JSON export from each host) implements the same surface, so adapters work
unchanged against VPS sessions.
"""

from __future__ import annotations

import glob as _glob
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

from ..models import SessionState
from ..tailer import read_tail_lines, read_tail_records


@dataclass
class SessionRef:
    """A pointer to one session's primary file, returned by ``discover()``."""

    path: str                   # the primary transcript / history file
    session_id: str
    cwd: Optional[str] = None   # decoded working directory when known


class Source(ABC):
    """Abstracts where session files live so the same adapters can read a remote
    host later. Implementations must never raise on missing files."""

    host: str = "local"

    @abstractmethod
    def expand(self, path: str) -> str: ...
    @abstractmethod
    def exists(self, path: str) -> bool: ...
    @abstractmethod
    def glob(self, pattern: str) -> list[str]: ...
    @abstractmethod
    def mtime(self, path: str) -> float: ...
    @abstractmethod
    def read_text(self, path: str) -> str: ...
    @abstractmethod
    def tail_lines(self, path: str, max_bytes: int = 512_000) -> list[str]: ...
    @abstractmethod
    def tail_records(self, path: str, max_bytes: int = 512_000) -> list[dict]: ...


class LocalSource(Source):
    host = "local"

    def expand(self, path: str) -> str:
        return os.path.expanduser(os.path.expandvars(path))

    def exists(self, path: str) -> bool:
        return os.path.exists(self.expand(path))

    def glob(self, pattern: str) -> list[str]:
        return _glob.glob(self.expand(pattern))

    def mtime(self, path: str) -> float:
        try:
            return os.path.getmtime(self.expand(path))
        except OSError:
            return 0.0

    def read_text(self, path: str) -> str:
        try:
            with open(self.expand(path), "r", encoding="utf-8", errors="replace") as fh:
                return fh.read()
        except OSError:
            return ""

    def tail_lines(self, path: str, max_bytes: int = 512_000) -> list[str]:
        return read_tail_lines(self.expand(path), max_bytes)

    def tail_records(self, path: str, max_bytes: int = 512_000) -> list[dict]:
        return read_tail_records(self.expand(path), max_bytes)


class Adapter(ABC):
    """One adapter per vendor. Pure translation: vendor files in, SessionState out."""

    vendor: str = "unknown"

    @abstractmethod
    def discover(self, source: Source) -> list[SessionRef]:
        """Find candidate sessions for this vendor. Must be cheap — it runs every
        refresh. Return ``[]`` when the vendor is not installed."""
        ...

    @abstractmethod
    def read(
        self, source: Source, ref: SessionRef, prev: Optional[SessionState]
    ) -> Optional[SessionState]:
        """Produce the current SessionState for one ref.

        ``prev`` is the last state held for this session (or ``None`` on first
        sight). Adapters may use it to carry context forward but must stay
        correct when it is ``None``. Return ``None`` to skip a ref that is not
        really a session. This method must not raise: on trouble, return a
        SessionState with ``state=State.ERROR`` and a message in ``error``.
        """
        ...
