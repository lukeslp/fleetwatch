"""Vendor adapters. ``all_adapters()`` returns the enabled, importable set."""

from __future__ import annotations

import importlib
import sys

from ..config import ENABLED_VENDORS
from .base import Adapter, LocalSource, SessionRef, Source

_REGISTRY = {
    "claude": ("claude", "ClaudeAdapter"),
    "codex": ("codex", "CodexAdapter"),
    "grok": ("grok", "GrokAdapter"),
    "gemini": ("gemini", "GeminiAdapter"),
}


def all_adapters() -> list[Adapter]:
    """Instantiate every enabled adapter. An adapter that fails to import is
    skipped with a warning rather than taking the whole tool down."""
    out: list[Adapter] = []
    for vendor in ENABLED_VENDORS:
        spec = _REGISTRY.get(vendor)
        if not spec:
            continue
        module_name, class_name = spec
        try:
            mod = importlib.import_module(f".{module_name}", __package__)
            out.append(getattr(mod, class_name)())
        except Exception as exc:  # never let one adapter break the others
            print(f"fleetwatcher: skipping {vendor} adapter ({exc})", file=sys.stderr)
    return out


__all__ = ["Adapter", "SessionRef", "Source", "LocalSource", "all_adapters"]
