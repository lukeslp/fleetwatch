"""Tests for the background summarizer.

The Summarizer is best-effort and threaded, so these tests inject a fake
``anthropic`` client (no network, no key) and drive the real enable path,
ThreadPoolExecutor, cache, and dedup logic. Threaded tests block the fake
model call on an :class:`threading.Event` so the in-flight window is
deterministic, then ``drain`` to a bounded timeout.
"""

from __future__ import annotations

import sys
import threading
import types

import pytest

from fleetwatcher.models import SessionState, State, TodoItem
from fleetwatcher.summarize import Summarizer


# --------------------------------------------------------------------------- #
# Fakes                                                                        #
# --------------------------------------------------------------------------- #
def _text_block(text: str):
    return types.SimpleNamespace(type="text", text=text)


class FakeClient:
    """Stands in for ``anthropic.Anthropic()``; records calls, returns text."""

    def __init__(self, response="a clean summary sentence", blocks=None, exc=None):
        self.response = response          # read at call time (mutable for force test)
        self._blocks = blocks             # override the content blocks entirely
        self._exc = exc                   # raise this instead of returning
        self.calls: list[dict] = []
        self.gate: threading.Event | None = None  # if set, create() blocks on it
        self.messages = self              # so client.messages.create == self.create

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.gate is not None:
            self.gate.wait(2.0)
        if self._exc is not None:
            raise self._exc
        blocks = self._blocks
        if blocks is None:
            blocks = [_text_block(self.response)]
        return types.SimpleNamespace(content=blocks)


def _enable(monkeypatch, client: FakeClient) -> Summarizer:
    """Build a Summarizer wired to ``client`` through the real enable path."""
    mod = types.ModuleType("anthropic")
    mod.Anthropic = lambda *a, **k: client
    monkeypatch.setitem(sys.modules, "anthropic", mod)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    s = Summarizer(use_model=True)
    assert s.enabled
    assert s._pool is not None
    return s


def _state(last_activity: float = 100.0, **kw) -> SessionState:
    base = dict(
        vendor="claude",
        session_id="s1",
        project="fleetwatcher",
        state=State.WAITING,
        last_activity=last_activity,
        doing="ran the test suite",
        needs="approve running pytest?",
    )
    base.update(kw)
    return SessionState(**base)


# --------------------------------------------------------------------------- #
# enable / disable                                                            #
# --------------------------------------------------------------------------- #
def test_not_enabled_without_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    s = Summarizer(use_model=True)
    assert not s.enabled
    assert s._pool is None
    st = _state()
    s.request(st)              # no-op, never touches the network
    assert st.summary is None
    assert s.cached(st) is None
    s.drain(0.1)               # returns immediately when disabled


def test_not_enabled_when_use_model_false(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    s = Summarizer(use_model=False)
    assert not s.enabled
    assert s._pool is None


def test_request_without_pool_is_safe():
    """Regression: ``enabled`` with no pool must return, not assert.

    The internal invariant is ``enabled`` implies a pool, but a guarded return
    (formerly an ``assert``) keeps a broken invariant from raising inside a
    background thread or vanishing under ``python -O``.
    """
    s = Summarizer(use_model=False)
    s._client = object()       # force enabled True while _pool stays None
    assert s.enabled and s._pool is None
    st = _state()
    s.request(st)              # must not raise
    assert s.cached(st) is None


# --------------------------------------------------------------------------- #
# request / cache / drain                                                     #
# --------------------------------------------------------------------------- #
def test_request_calls_model_and_caches(monkeypatch):
    client = FakeClient(response="Claude is waiting for you to approve pytest.")
    s = _enable(monkeypatch, client)
    st = _state()

    s.request(st)
    s.drain(2.0)

    assert st.summary == "Claude is waiting for you to approve pytest."
    assert s.cached(st) == "Claude is waiting for you to approve pytest."
    assert len(client.calls) == 1
    s._pool.shutdown(wait=False)


def test_cache_hit_skips_second_call(monkeypatch):
    client = FakeClient(response="cached sentence")
    s = _enable(monkeypatch, client)
    st = _state()

    s.request(st)
    s.drain(2.0)
    assert len(client.calls) == 1

    # Same (key, last_activity): the cached sentence is applied with no new call.
    fresh = _state()
    s.request(fresh)
    assert fresh.summary == "cached sentence"
    assert len(client.calls) == 1
    s._pool.shutdown(wait=False)


def test_cached_keyed_by_activity(monkeypatch):
    client = FakeClient(response="first")
    s = _enable(monkeypatch, client)

    s.request(_state(last_activity=100.0))
    s.drain(2.0)
    assert len(client.calls) == 1

    # New activity timestamp on the same session is a cache miss → a new call.
    client.response = "second"
    later = _state(last_activity=200.0)
    s.request(later)
    s.drain(2.0)
    assert len(client.calls) == 2
    assert later.summary == "second"
    s._pool.shutdown(wait=False)


def test_force_recomputes(monkeypatch):
    client = FakeClient(response="first")
    s = _enable(monkeypatch, client)
    st = _state()

    s.request(st)
    s.drain(2.0)
    assert s.cached(st) == "first"
    assert len(client.calls) == 1

    client.response = "second"
    s.request(st, force=True)
    s.drain(2.0)
    assert len(client.calls) == 2
    assert s.cached(st) == "second"
    s._pool.shutdown(wait=False)


def test_inflight_dedup(monkeypatch):
    client = FakeClient(response="only once")
    client.gate = threading.Event()       # hold the first call in-flight
    s = _enable(monkeypatch, client)
    st = _state()

    s.request(st)                          # schedules one call, blocks on the gate
    s.request(st)                          # deduped: same activity already in flight
    assert len(s._inflight) == 1

    client.gate.set()
    s.drain(2.0)
    assert len(client.calls) == 1
    assert st.summary == "only once"
    s._pool.shutdown(wait=False)


def test_model_failure_leaves_summary_none(monkeypatch):
    client = FakeClient(exc=RuntimeError("network down"))
    s = _enable(monkeypatch, client)
    st = _state()

    s.request(st)
    s.drain(2.0)

    assert st.summary is None              # heuristic line stays in place
    assert s.cached(st) is None            # a failure is not cached
    assert s._inflight == set()            # but the slot is released, so drain ends
    s._pool.shutdown(wait=False)


# --------------------------------------------------------------------------- #
# _call_model: line collapsing, truncation, emptiness                         #
# --------------------------------------------------------------------------- #
def _disabled_with_client(client: FakeClient) -> Summarizer:
    s = Summarizer(use_model=False)
    s._client = client                     # call _call_model directly, no pool
    return s


def test_call_model_collapses_multiblock_and_whitespace():
    client = FakeClient(blocks=[_text_block("first line\n\nsecond"), _text_block("third")])
    s = _disabled_with_client(client)
    assert s._call_model(_state()) == "first line second third"


def test_call_model_ignores_nontext_blocks():
    client = FakeClient(blocks=[
        types.SimpleNamespace(type="tool_use"),   # no .text, must be skipped
        _text_block("the sentence"),
    ])
    s = _disabled_with_client(client)
    assert s._call_model(_state()) == "the sentence"


def test_call_model_truncates_long():
    client = FakeClient(response="x" * 300)
    s = _disabled_with_client(client)
    out = s._call_model(_state())
    assert len(out) == 240
    assert out.endswith("…")


def test_call_model_empty_returns_none():
    client = FakeClient(blocks=[_text_block("   ")])
    s = _disabled_with_client(client)
    assert s._call_model(_state()) is None


# --------------------------------------------------------------------------- #
# prompt construction                                                         #
# --------------------------------------------------------------------------- #
def test_build_prompt_includes_context():
    s = Summarizer(use_model=False)
    st = _state(
        last_user="please add the tests",
        last_agent="May I run pytest?",
        todos=[TodoItem("write the tests", "in_progress")],
    )
    prompt = s._build_prompt(st)
    for token in ("claude", "fleetwatcher", "waiting", "approve running pytest?",
                  "please add the tests", "May I run pytest?", "write the tests"):
        assert token in prompt
