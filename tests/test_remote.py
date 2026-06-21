"""Tests for the remote (ssh-pull) source and its merge into the aggregator."""

from __future__ import annotations

import json
import subprocess
import time
import types

from fleetwatcher.core import Aggregator
from fleetwatcher.models import SessionState, State
from fleetwatcher.remote import RemoteHost, parse_hosts
from fleetwatcher.summarize import Summarizer


# --------------------------------------------------------------------------- #
# parse_hosts                                                                  #
# --------------------------------------------------------------------------- #
def test_parse_hosts_variants():
    hosts = parse_hosts("local,dreamer,box=user@host")
    names = [(h.name, h.ssh_target) for h in hosts]
    assert ("dreamer", "dreamer") in names
    assert ("box", "user@host") in names
    # 'local' is implicit, never a remote
    assert all(h.name != "local" for h in hosts)
    assert len(hosts) == 2


def test_parse_hosts_empty():
    assert parse_hosts("") == []
    assert parse_hosts("local") == []


# --------------------------------------------------------------------------- #
# RemoteHost.fetch                                                             #
# --------------------------------------------------------------------------- #
def _completed(stdout: str, returncode: int = 0):
    return subprocess.CompletedProcess(
        args=["ssh"], returncode=returncode, stdout=stdout, stderr=""
    )


def test_fetch_success_relabels_source(monkeypatch):
    payload = [
        SessionState(
            vendor="claude", session_id="abc", project="proj", state=State.WAITING
        ).to_dict()
    ]
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: _completed(json.dumps(payload))
    )
    host = RemoteHost("dreamer", "luke@dr.eamer.dev")
    states, ok = host.fetch()
    assert ok is True
    assert len(states) == 1
    assert states[0].session_id == "abc"
    assert states[0].source == "dreamer"  # relabeled away from "local"


def test_fetch_nonzero_returncode_is_unreachable(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _completed("", 255))
    states, ok = RemoteHost("dreamer").fetch()
    assert states == [] and ok is False


def test_fetch_exception_is_unreachable(monkeypatch):
    def boom(*a, **k):
        raise OSError("ssh not found")

    monkeypatch.setattr(subprocess, "run", boom)
    states, ok = RemoteHost("dreamer").fetch()
    assert states == [] and ok is False


def test_fetch_garbage_output_is_unreachable(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _completed("not json"))
    states, ok = RemoteHost("dreamer").fetch()
    assert states == [] and ok is False


# --------------------------------------------------------------------------- #
# Aggregator merge + unreachable resilience                                    #
# --------------------------------------------------------------------------- #
class FakeHost:
    """Duck-typed host: name + fetch(). Reachability is toggleable."""

    def __init__(self, name, states, reachable=True):
        self.name = name
        self._states = states
        self.reachable = reachable

    def fetch(self):
        if not self.reachable:
            return [], False
        # hand back fresh copies so the aggregator owns its objects
        return [SessionState.from_dict(s.to_dict()) for s in self._states], True


def _agg(hosts):
    # disabled summarizer (no network), no local adapters (isolate remote path)
    agg = Aggregator(hosts=hosts, summarizer=Summarizer(use_model=False))
    agg.adapters = []
    return agg


def test_aggregator_merges_remote_sessions():
    st = SessionState(
        vendor="claude", session_id="r1", project="proj",
        last_activity=time.time(), state=State.WAITING, needs="approve a command",
    )
    agg = _agg([FakeHost("dreamer", [st])])
    agg.refresh()
    sessions = agg.sessions()
    assert any(s.source == "dreamer" and s.session_id == "r1" for s in sessions)
    assert agg.counts()["waiting"] == 1


def test_unreachable_host_keeps_last_sessions():
    st = SessionState(
        vendor="claude", session_id="r1", project="proj",
        last_activity=time.time(), state=State.IDLE,
    )
    host = FakeHost("dreamer", [st])
    agg = _agg([host])
    agg.refresh()
    assert any(s.session_id == "r1" for s in agg.sessions())

    # host goes dark — its sessions must NOT be evicted, just kept (stale)
    host.reachable = False
    agg.refresh()
    assert any(s.session_id == "r1" for s in agg.sessions())
