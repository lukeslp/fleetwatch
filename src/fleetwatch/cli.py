"""fleetwatch command-line entry point."""

from __future__ import annotations

import argparse
import json
import os
import sys


def main(argv: "list[str] | None" = None) -> int:
    parser = argparse.ArgumentParser(
        prog="fleetwatch",
        description="Live plain-language status of every terminal coding session you have running.",
    )
    parser.add_argument(
        "--once", action="store_true",
        help="print one text snapshot and exit (no live screen)",
    )
    parser.add_argument(
        "--export-json", action="store_true",
        help="print current sessions as JSON and exit (for scripting / remote export)",
    )
    parser.add_argument(
        "--no-model", action="store_true",
        help="disable model summaries; heuristics only",
    )
    parser.add_argument(
        "--vendors",
        help="comma-separated subset of vendors to watch (default: claude,codex,grok)",
    )
    args = parser.parse_args(argv)

    # CLI flags are translated to env so config.py stays the single source of truth.
    if args.no_model:
        os.environ["FLEETWATCH_NO_MODEL"] = "1"
    if args.vendors:
        os.environ["FLEETWATCH_VENDORS"] = args.vendors

    from .core import Aggregator

    agg = Aggregator()
    agg.refresh()

    if args.export_json or args.once:
        # One-shot modes have no refresh loop, so give background summaries a
        # moment to land before we print.
        agg.summarizer.drain()

    if args.export_json:
        print(json.dumps([s.to_dict() for s in agg.sessions()], indent=2))
        return 0

    if args.once:
        from .render import render_snapshot
        print(render_snapshot(agg.sessions(), counts=agg.counts()))
        return 0

    from .tui import run_tui
    run_tui(agg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
