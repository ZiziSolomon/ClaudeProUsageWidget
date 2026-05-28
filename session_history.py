#!/usr/bin/env python3
"""
Session history — offline analysis of token usage and (where known) the
token budget available in each 5-hour session.

This is a standalone, read-only tool. It does NOT touch the live widget,
call the API, or need browser cookies / curl_cffi. It combines two sources:

  1. Transcripts  (~/.claude/projects/**/*.jsonl)
     Reconstructs historical 5-hour session windows from assistant-message
     timestamps and tallies input/output tokens per session. This gives a
     *usage* back-log reaching as far back as the transcripts do.

  2. calibration.jsonl  (usage_data/calibration.jsonl)
     The live widget already records, per calibration, the API utilisation %
     alongside our transcript token count, and derives
         implied_session_budget = transcript_tokens / (pct / 100)
     i.e. an estimate of how many tokens the session *could* hold. We pick
     the most reliable estimate per session (the highest-utilisation
     calibration, where relative error is smallest).

The two are joined on time: a calibration belongs to the reconstructed
session whose window contains the moment it was scraped.

Caveat baked into the output: implied_session_budget assumes all of the
session's usage was logged on THIS machine. If tokens were spent on another
device, the API's pct reflects them but our transcript tokens don't, so the
estimate is biased LOW. The contamination flagger marks sessions whose
implied budget is a statistical outlier (usually the low ones) so they can
be discounted.

Usage:
    python session_history.py              # print a table
    python session_history.py --json       # also write usage_data/session_history.jsonl
"""

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import median

PROJECTS_DIR     = Path.home() / ".claude" / "projects"


def _data_dir() -> Path:
    """Mirror of widget_updater._data_dir (kept standalone so this analysis
    tool doesn't import the widget's API/cookie stack). Read-only here - the
    widget owns migration. Override with CLAUDE_USAGE_DATA_DIR."""
    import os
    env = os.environ.get("CLAUDE_USAGE_DATA_DIR")
    if env:
        return Path(env)
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(base) / "ClaudeUsage" / "usage_data"


DATA_DIR         = _data_dir()
CALIBRATION_FILE = DATA_DIR / "calibration.jsonl"
OUTPUT_FILE      = DATA_DIR / "session_history.jsonl"
SESSION_HOURS    = 5
# Robust outlier threshold: flag budgets more than this many MADs from the
# median. 3.5 is the conventional modified-z cutoff.
OUTLIER_MADS = 3.5


# ---------------------------------------------------------------------------
# 1. Transcript reconstruction
# ---------------------------------------------------------------------------

def _iter_assistant_events():
    """Yield (timestamp, msg_id, input_tokens, output_tokens, model) for every
    assistant message with usage across all transcripts. Mirrors the live
    widget's counting rules so totals line up."""
    for f in PROJECTS_DIR.rglob("*.jsonl"):
        try:
            text = f.read_text(encoding="utf-8", errors="ignore")
        except Exception as e:
            print(f"[X] _iter_assistant_events: could not read {f}: {type(e).__name__}: {e}")
            continue
        for line in text.splitlines():
            try:
                obj = json.loads(line)
            except Exception as e:
                print(f"[X] _iter_assistant_events: JSON decode failed in {f.name}: {type(e).__name__}: {e}")
                continue
            if obj.get("type") != "assistant":
                continue
            msg = obj.get("message", {})
            usage = msg.get("usage")
            mid = msg.get("id")
            ts_raw = obj.get("timestamp")
            if not usage or not mid or not ts_raw:
                continue
            try:
                ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
            except Exception as e:
                print(f"[X] _iter_assistant_events: bad timestamp {ts_raw!r} in {f.name}: {type(e).__name__}: {e}")
                continue
            yield (ts, mid,
                   usage.get("input_tokens", 0),
                   usage.get("output_tokens", 0),
                   msg.get("model", "unknown"))


def reconstruct_sessions() -> list[dict]:
    """Group assistant events into fixed 5-hour windows.

    A session opens at the first message after any prior window has closed,
    and spans exactly SESSION_HOURS — the same fixed-length window the
    claude.ai API reports. Messages are deduped by id (a message can appear
    in more than one transcript after compaction)."""
    seen: set[str] = set()
    events = []
    for ts, mid, tin, tout, model in _iter_assistant_events():
        if mid in seen:
            continue
        seen.add(mid)
        events.append((ts, tin, tout, model))
    events.sort(key=lambda e: e[0])

    sessions: list[dict] = []
    cur: dict | None = None
    for ts, tin, tout, model in events:
        if cur is None or ts > cur["end"]:
            cur = {
                "start": ts,
                "end": ts + timedelta(hours=SESSION_HOURS),
                "input_tokens": 0,
                "output_tokens": 0,
                "messages": 0,
                "by_model": {},
            }
            sessions.append(cur)
        cur["input_tokens"] += tin
        cur["output_tokens"] += tout
        cur["messages"] += 1
        bm = cur["by_model"].setdefault(model, {"input": 0, "output": 0})
        bm["input"] += tin
        bm["output"] += tout
    return sessions


# ---------------------------------------------------------------------------
# 2. Calibration budgets
# ---------------------------------------------------------------------------

def load_calibration_budgets() -> list[dict]:
    """Best (highest-pct) implied budget per calibrated session_start.

    Returns one entry per session_start seen in calibration.jsonl, carrying
    the scraped_at of the chosen calibration so we can place it on the
    reconstructed timeline."""
    if not CALIBRATION_FILE.exists():
        return []
    best: dict[str, dict] = {}
    for line in CALIBRATION_FILE.read_text(encoding="utf-8").splitlines():
        try:
            rec = json.loads(line)
        except Exception as e:
            print(f"[X] load_calibration_budgets: JSON decode failed: {type(e).__name__}: {e}")
            continue
        pct = rec.get("session_pct")
        start = rec.get("session_start")
        if pct is None or pct <= 0 or not start:
            continue
        # Recompute the budget rather than trusting the stored field, so old
        # records written before any formula tweak stay consistent.
        total_io = rec.get("transcript_io_total", 0)
        budget = round(total_io / (pct / 100)) if total_io else None
        prior = best.get(start)
        if prior is None or pct > prior["pct"]:
            best[start] = {
                "session_start": start,
                "pct": pct,
                "implied_budget": budget,
                "scraped_at": rec.get("scraped_at"),
                "calibrations": 0,
            }
    # Count how many calibrations each session got (data-density signal).
    for line in CALIBRATION_FILE.read_text(encoding="utf-8").splitlines():
        try:
            rec = json.loads(line)
        except Exception as e:
            print(f"[X] load_calibration_budgets (count pass): JSON decode failed: {type(e).__name__}: {e}")
            continue
        start = rec.get("session_start")
        if start in best:
            best[start]["calibrations"] += 1
    return list(best.values())


# ---------------------------------------------------------------------------
# 3. Join + outlier flagging
# ---------------------------------------------------------------------------

def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts)
    except Exception as e:
        print(f"[X] _parse: bad timestamp {ts!r}: {type(e).__name__}: {e}")
        return None


def build_history() -> list[dict]:
    sessions = reconstruct_sessions()
    budgets = load_calibration_budgets()

    # Place each calibration budget on the reconstructed session whose window
    # contains its scrape time; fall back to nearest start if none contains it.
    for b in budgets:
        when = _parse(b["scraped_at"]) or _parse(b["session_start"])
        if when is None:
            continue
        match = None
        for s in sessions:
            if s["start"] <= when <= s["end"]:
                match = s
                break
        if match is None and sessions:
            match = min(sessions, key=lambda s: abs((s["start"] - when).total_seconds()))
        if match is not None:
            # Keep the highest-pct budget if two calibrations map to one window.
            if "pct" not in match or b["pct"] > match.get("pct", -1):
                match["pct"] = b["pct"]
                match["implied_budget"] = b["implied_budget"]
                match["calibrations"] = b["calibrations"]

    # Outlier flagging over sessions that actually have a budget estimate.
    known = [s["implied_budget"] for s in sessions
             if s.get("implied_budget") is not None]
    med = median(known) if known else None
    mad = median([abs(b - med) for b in known]) if known else None
    for s in sessions:
        b = s.get("implied_budget")
        if b is None or med is None or not mad:
            s["outlier"] = False
            s["outlier_note"] = None
            continue
        score = 0.6745 * (b - med) / mad  # modified z-score
        s["outlier"] = abs(score) > OUTLIER_MADS
        if s["outlier"]:
            s["outlier_note"] = ("budget far below others — likely off-device "
                                 "usage inflated the API %"
                                 if score < 0 else
                                 "budget far above others — verify")
        else:
            s["outlier_note"] = None
    return sessions


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def _fmt_int(n) -> str:
    return f"{n:,}" if isinstance(n, (int, float)) else "--"


def print_table(sessions: list[dict]) -> None:
    if not sessions:
        print("No sessions found in transcripts.")
        return
    print(f"{'session start (UTC)':<20} {'msgs':>5} {'in':>11} {'out':>11} "
          f"{'pct':>5} {'budget~':>12}  flag")
    print("-" * 82)
    for s in sorted(sessions, key=lambda x: x["start"]):
        pct = s.get("pct")
        flag = "  [!] " + s["outlier_note"] if s.get("outlier") else ""
        if s.get("implied_budget") is None and pct is None:
            flag = flag or "  (usage only - no API % for this session)"
        print(f"{s['start'].strftime('%Y-%m-%d %H:%M'):<20} "
              f"{s['messages']:>5} "
              f"{_fmt_int(s['input_tokens']):>11} "
              f"{_fmt_int(s['output_tokens']):>11} "
              f"{(str(round(pct)) + '%') if pct is not None else '--':>5} "
              f"{_fmt_int(s.get('implied_budget')):>12}"
              f"{flag}")

    budgeted = [s for s in sessions if s.get("implied_budget") is not None]
    print("-" * 82)
    print(f"{len(sessions)} sessions, {len(budgeted)} with a budget estimate.")
    clean = [s["implied_budget"] for s in budgeted if not s.get("outlier")]
    if clean:
        print(f"Median implied session budget (excluding flagged outliers): "
              f"~{median(clean):,.0f} tokens.")


def write_jsonl(sessions: list[dict]) -> None:
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT_FILE.open("w", encoding="utf-8") as f:
        for s in sorted(sessions, key=lambda x: x["start"]):
            f.write(json.dumps({
                "session_start": s["start"].isoformat(),
                "session_end":   s["end"].isoformat(),
                "messages":      s["messages"],
                "input_tokens":  s["input_tokens"],
                "output_tokens": s["output_tokens"],
                "io_total":      s["input_tokens"] + s["output_tokens"],
                "session_pct":   s.get("pct"),
                "implied_session_budget": s.get("implied_budget"),
                "calibrations":  s.get("calibrations", 0),
                "outlier":       s.get("outlier", False),
                "outlier_note":  s.get("outlier_note"),
                "by_model":      s["by_model"],
            }) + "\n")
    print(f"\nWrote {OUTPUT_FILE}")


def main() -> None:
    sessions = build_history()
    print_table(sessions)
    if "--json" in sys.argv:
        write_jsonl(sessions)


if __name__ == "__main__":
    main()
