"""Watching other machines, the cheap and robust way.

Instead of tailing a remote host's session files over a long-lived connection,
fleetwatcher asks the remote to do its own normalization and just hand back the
answer: one ``ssh <host> fleetwatcher --export-json`` per refresh returns that
host's whole fleet as JSON. One command per host (not N file tails), the work
happens where the files are, and the conversation content never leaves the
channel you already trust.

A failed fetch does NOT drop the host's sessions — the aggregator keeps the last
snapshot so an unreachable host reads as "stale", never as "all gone".
"""

from __future__ import annotations

import json
import subprocess
from typing import Optional

from .models import SessionState


class RemoteHost:
    """One machine reachable over ssh, watched by pulling its JSON export."""

    def __init__(
        self,
        name: str,
        ssh_target: Optional[str] = None,
        command: str = "fleetwatcher --export-json",
        timeout: float = 15.0,
    ):
        self.name = name
        self.ssh_target = ssh_target or name
        self.command = command
        self.timeout = timeout

    def fetch(self) -> "tuple[list[SessionState], bool]":
        """Return ``(sessions, reachable)``. Never raises.

        ``reachable`` is False on any ssh/parse trouble; sessions is then empty
        and the caller should keep whatever it already had for this host.
        """
        # Force heuristics-only on the remote so it never spends tokens; the
        # local summarizer fills in plain-language summaries for the whole fleet
        # from one API key.
        remote_cmd = f"FLEETWATCHER_NO_MODEL=1 {self.command}"
        argv = [
            "ssh",
            "-o", "BatchMode=yes",
            "-o", f"ConnectTimeout={int(self.timeout)}",
            self.ssh_target,
            remote_cmd,
        ]
        try:
            proc = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=self.timeout + 5,
            )
        except (OSError, subprocess.TimeoutExpired):
            return [], False
        if proc.returncode != 0 or not proc.stdout.strip():
            return [], False
        try:
            data = json.loads(proc.stdout)
        except (ValueError, TypeError):
            return [], False
        if not isinstance(data, list):
            return [], False

        out: list[SessionState] = []
        for d in data:
            try:
                st = SessionState.from_dict(d)
            except Exception:
                continue
            st.source = self.name  # relabel: these sessions live on this host
            out.append(st)
        return out, True


def parse_hosts(spec: str) -> list[RemoteHost]:
    """Parse a ``--hosts`` / ``FLEETWATCHER_HOSTS`` string into RemoteHosts.

    Comma-separated. Each entry is ``name`` or ``name=ssh_target``. The pseudo
    host ``local`` is ignored here (the aggregator always watches local).

        "dreamer"                       -> ssh dreamer
        "dreamer=luke@dr.eamer.dev"     -> ssh luke@dr.eamer.dev (labelled dreamer)
        "local,dreamer,box=user@host"   -> dreamer + box
    """
    hosts: list[RemoteHost] = []
    for raw in (spec or "").split(","):
        entry = raw.strip()
        if not entry or entry.lower() == "local":
            continue
        if "=" in entry:
            name, target = entry.split("=", 1)
            hosts.append(RemoteHost(name.strip(), target.strip()))
        else:
            hosts.append(RemoteHost(entry))
    return hosts
