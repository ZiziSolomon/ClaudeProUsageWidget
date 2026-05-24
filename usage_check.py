#!/usr/bin/env python3
"""
usage_check — report current Claude.ai usage from the command line.

Two sources:
  * widget estimate (default): reads the running widget's
    %LOCALAPPDATA%\\ClaudeUsage\\usage_data\\widget_state.json. Instant, no
    network. This is what the tray icon is showing right now.
  * live web check (--live): replays your browser session cookie against the
    claude.ai usage API for the authoritative number (the same figure the
    website shows). Slower, hits the network.

The live number matches what Claude Code reports as your session usage, so
this doubles as a way to ask Claude "how much of my session/week is left?".

Usage:
  python usage_check.py            # widget estimate, human-readable
  python usage_check.py --live     # fresh number straight from claude.ai
  python usage_check.py --json     # machine-readable (for agents/scripts)
  python usage_check.py --live --json
"""

import argparse
import json
import sys
from datetime import datetime, timezone

# Reuse the widget's own paths, fetch and parse helpers so this tool tracks
# any improvement to cookie handling / org-id resolution made there.
import widget_updater as wu


def _fmt_eta(end: datetime | None) -> str:
    """Human delta to a reset time, e.g. '2h 41m' or '34m' or 'now'."""
    if end is None:
        return "?"
    secs = (end - datetime.now(timezone.utc)).total_seconds()
    if secs <= 0:
        return "now"
    mins = int(secs // 60)
    h, m = divmod(mins, 60)
    return f"{h}h {m}m" if h else f"{m}m"


def _local(end: datetime | None) -> str:
    return end.astimezone().strftime("%H:%M") if end else "?"


def get_usage(live: bool = False) -> dict:
    """Return current usage as a dict. Raises on a failed live fetch so the
    caller can distinguish 'no number' from '0%'."""
    if live:
        raw = wu._fetch_usage()
        if raw is None:
            raise RuntimeError(
                "live web check failed — not logged in to claude.ai in the "
                "browser this tool reads, or the endpoint is unavailable."
            )
        _, session_end, session_pct = wu._parse_session(raw)
        weekly_pct, weekly_end = wu._parse_weekly(raw)
        source, updated = "claude.ai live", datetime.now(timezone.utc)
    else:
        state = wu._load_state()
        if state.get("session_pct") is None:
            raise RuntimeError(
                f"no widget estimate yet ({wu.STATE_FILE}). Is the widget "
                "running? Try --live for a fresh web check."
            )
        session_pct = state.get("session_pct")
        weekly_pct = state.get("weekly_pct")
        session_end = (datetime.fromisoformat(state["session_end"])
                       if state.get("session_end") else None)
        weekly_end = (datetime.fromisoformat(state["weekly_end"])
                      if state.get("weekly_end") else None)
        updated = (datetime.fromisoformat(state["updated_at"])
                   if state.get("updated_at") else None)
        source = "widget estimate"

    return {
        "source": source,
        "session_pct": session_pct,
        "session_end": session_end.isoformat() if session_end else None,
        "session_eta": _fmt_eta(session_end),
        "weekly_pct": weekly_pct,
        "weekly_end": weekly_end.isoformat() if weekly_end else None,
        "weekly_eta": _fmt_eta(weekly_end),
        "updated_at": updated.isoformat() if updated else None,
        "_session_end_obj": session_end,
        "_weekly_end_obj": weekly_end,
        "_updated_obj": updated,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Report current Claude.ai usage.")
    ap.add_argument("--live", action="store_true",
                    help="fetch a fresh number from claude.ai instead of "
                         "reading the running widget's estimate")
    ap.add_argument("--json", action="store_true",
                    help="emit machine-readable JSON")
    args = ap.parse_args()

    try:
        u = get_usage(live=args.live)
    except RuntimeError as e:
        if args.json:
            print(json.dumps({"error": str(e)}))
        else:
            print(f"error: {e}", file=sys.stderr)
        return 1

    if args.json:
        out = {k: v for k, v in u.items() if not k.startswith("_")}
        print(json.dumps(out))
        return 0

    sp = "--" if u["session_pct"] is None else f"{u['session_pct']:.0f}%"
    wp = "--" if u["weekly_pct"] is None else f"{u['weekly_pct']:.0f}%"
    print(f"Session (5h):  {sp:>4}   resets in {u['session_eta']:<7} "
          f"({_local(u['_session_end_obj'])})")
    print(f"Weekly  (7d):  {wp:>4}   resets in {u['weekly_eta']:<7} "
          f"({_local(u['_weekly_end_obj'])})")
    age = ""
    if not args.live and u["_updated_obj"]:
        mins = int((datetime.now(timezone.utc) - u["_updated_obj"]).total_seconds() // 60)
        age = f", updated {mins}m ago" if mins else ", updated just now"
    print(f"source: {u['source']}{age}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
