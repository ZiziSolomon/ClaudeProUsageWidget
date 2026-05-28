"""Generate an accuracy chart: continuous local estimate line + API-truth dots.

Three ways to pick which session to plot:

    python save_accuracy_chart.py --last
        Pick the most recent session that appears in calibration.jsonl.

    python save_accuracy_chart.py --at "2026-05-27 09:30"
        Pick the session that was in flight at the given local datetime.
        Rejects with a non-zero exit if no recorded session covered it.
        Accepts any ISO-ish format datetime.fromisoformat() understands;
        bare dates are treated as 00:00 local on that day.

    python save_accuracy_chart.py
        Default = --last (most useful for debugging today's session).

The session window is taken from calibration.jsonl's `session_start` field
(the same window the widget itself uses), extended by SESSION_HOURS. Output
is written to docs/accuracy_sample.png unless --out points elsewhere.
"""
import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import matplotlib.pyplot as plt
import matplotlib.dates as mdates

DATA = Path(os.environ["LOCALAPPDATA"]) / "ClaudeUsage" / "usage_data"
JSONL = DATA / "calibration.jsonl"
LOG   = DATA / "widget_run.log"

# Must match widget_updater.SESSION_HOURS. Duplicated rather than imported so
# this script stays runnable without the widget's heavy import surface.
SESSION_HOURS = 5


def to_local_naive(dt: datetime) -> datetime:
    """Convert a UTC-aware datetime to a naive local-time datetime for plotting."""
    return datetime.fromtimestamp(dt.timestamp())


def _session_key(dt: datetime) -> datetime:
    """Group session_starts that belong to the same real 5h window.

    Every widget restart recomputes session_start = session_end - 5h from
    the API's resets_at, which carries microseconds; this drifts the
    stored session_start by milliseconds across restarts even within the
    same real window. Rounding down to the minute collapses those drifts
    without losing the ability to distinguish adjacent real sessions
    (which are 5h apart by construction)."""
    return dt.replace(second=0, microsecond=0)


def _load_all_records() -> list[dict]:
    """All calibration records with a parseable session_start. The list is
    naturally in time order because calibration.jsonl is append-only."""
    if not JSONL.exists():
        sys.exit(f"calibration.jsonl not found at {JSONL}")
    records = []
    for line in JSONL.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not r.get("session_start"):
            continue
        try:
            ss = datetime.fromisoformat(r["session_start"])
        except ValueError:
            continue
        r["_session_start_dt"] = ss
        r["_session_key"] = _session_key(ss)
        records.append(r)
    return records


def _resolve_session(args) -> datetime:
    """Pick the session-start (UTC, aware) to plot, per the CLI flags.

    --at picks the session whose [start, start+SESSION_HOURS) window covers
    the given local datetime. --last picks the most recent session_start
    that appears in the calibration log. Default is --last."""
    records = _load_all_records()
    if not records:
        sys.exit("calibration.jsonl is empty - nothing to plot.")

    if args.at:
        # Parse the user's datetime as local-naive, convert to UTC-aware.
        try:
            target_local = datetime.fromisoformat(args.at)
        except ValueError as e:
            sys.exit(f"Could not parse --at {args.at!r}: {e}")
        target_utc = target_local.astimezone(timezone.utc) if target_local.tzinfo \
                     else target_local.astimezone().astimezone(timezone.utc)
        window = timedelta(hours=SESSION_HOURS)
        # Distinct session keys only (minute-rounded so restart-drift
        # doesn't fragment one real session into many).
        seen = set()
        for r in records:
            key = r["_session_key"]
            if key in seen:
                continue
            seen.add(key)
            if key <= target_utc < key + window:
                return key
        sys.exit(f"No recorded session covered {args.at} "
                 f"(local). Try --last to see what's on file.")

    # --last (default): pick the session key belonging to the record with
    # the most recent scraped_at. Using scraped_at (not session_start)
    # because some tests have historically polluted the log with synthetic
    # future-dated session_start values (e.g. 2099-01-01); scraped_at is
    # always the real wall-clock time and reflects actual activity.
    latest = max(records, key=lambda r: r["scraped_at"])
    return latest["_session_key"]


def load_api_points(session_start: datetime) -> list[dict]:
    """Calibration records that belong to the chosen session AND have a
    resolved budget (i.e. were above the floor at fetch time, OR were
    sub-floor-blended after the 2026-05-28 calibration changes)."""
    points = []
    for r in _load_all_records():
        if r["_session_key"] != session_start:
            continue
        if r.get("implied_session_budget") and r.get("session_pct") is not None:
            points.append({
                "ts":  to_local_naive(datetime.fromisoformat(r["scraped_at"])),
                "pct": r["session_pct"],
            })
    # Deduplicate by minute (multiple rapid calls at the same % are noise).
    seen, deduped = set(), []
    for p in points:
        key = p["ts"].strftime("%H:%M")
        if key not in seen:
            seen.add(key)
            deduped.append(p)
    return deduped


def load_local_estimates(session_start: datetime) -> list[dict]:
    """Parse [HH:MM:SS] pct lines that fall in the session window.

    The log only carries HH:MM:SS, not dates. The trick: walk the log
    BACKWARDS from EOF (which is roughly 'now'), anchoring the most
    recent line to the log file's mtime date. As we walk up, time goes
    monotonically backwards in clock terms; whenever we see HMS jump
    UPWARD (e.g. 23:55 above 00:05) that's a midnight crossing and the
    inferred date steps back a day.

    This treats every line uniformly across Widget HTTP restart blocks
    and needs no calibration-log anchoring. Lines older than the session
    window are dropped."""
    if not LOG.exists():
        return []
    all_lines = LOG.read_text(encoding="utf-8", errors="ignore").splitlines()

    sess_local_start = to_local_naive(session_start)
    sess_local_end   = sess_local_start + timedelta(hours=SESSION_HOURS)

    line_pat = re.compile(r"^\[(\d{2}):(\d{2}):(\d{2})\].*pct=(\d+(?:\.\d+)?)")
    matches = []
    for line in all_lines:
        m = line_pat.match(line)
        if m:
            matches.append((int(m.group(1)), int(m.group(2)),
                            int(m.group(3)), float(m.group(4))))
    if not matches:
        return []

    # Anchor the LAST line to the log file's mtime. Most-recent line in
    # an actively-written log is by definition very recent, so the log's
    # mtime date is the right date for it.
    log_mtime = datetime.fromtimestamp(LOG.stat().st_mtime)
    current_date = log_mtime.date()

    # Build (datetime, pct) pairs by walking backwards.
    dated = []
    prev_hms = None
    for h, mi, sec, pct in reversed(matches):
        hms = (h, mi, sec)
        if prev_hms is not None and hms > prev_hms:
            # Walking backwards: an UPWARD HMS jump means we crossed
            # midnight going back into the previous day.
            current_date -= timedelta(days=1)
        prev_hms = hms
        ts = datetime(current_date.year, current_date.month, current_date.day,
                      h, mi, sec)
        dated.append((ts, pct))

    # Restore chronological order and keep only in-window points.
    dated.reverse()
    return [{"ts": ts, "pct": pct}
            for ts, pct in dated
            if sess_local_start <= ts < sess_local_end]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--last", action="store_true",
                   help="Plot the most recent session (default).")
    g.add_argument("--at", metavar="DATETIME",
                   help='Plot the session in flight at this local datetime, '
                        'e.g. "2026-05-28 09:30". ISO-ish formats accepted.')
    ap.add_argument("--out", type=Path,
                    help="Output PNG path. Default: docs/accuracy_<session-start>.png "
                         "(timestamped so repeat runs don't overwrite each other - "
                         "useful for debugging across multiple sessions). Pass "
                         "docs/accuracy_sample.png explicitly when regenerating "
                         "the README chart.")
    args = ap.parse_args()

    session_start = _resolve_session(args)
    if args.out is None:
        stamp = to_local_naive(session_start).strftime("%Y-%m-%d_%H%M")
        args.out = Path(__file__).parent / "docs" / f"accuracy_{stamp}.png"
    api_pts   = load_api_points(session_start)
    local_pts = load_local_estimates(session_start)

    if not local_pts:
        sys.exit(f"No local estimate data found in the log for session "
                 f"starting {session_start.isoformat()}.")

    local_ts  = [p["ts"] for p in local_pts]
    local_pct = [p["pct"] for p in local_pts]
    api_ts    = [p["ts"] for p in api_pts]
    api_pct   = [p["pct"] for p in api_pts]

    fig, ax = plt.subplots(figsize=(9, 4))
    ax.plot(local_ts, local_pct, "-", color="#4C9BE8", lw=1.8,
            label="Local estimate (live)")
    ax.scatter(api_ts, api_pct, color="#E84C4C", s=80, zorder=5,
               label="API truth (calibration call)")
    for ts in api_ts:
        ax.axvline(ts, color="#E84C4C", lw=0.6, ls=":", alpha=0.5)

    sess_label = to_local_naive(session_start).strftime("%Y-%m-%d %H:%M")
    ax.set_title(f"Session {sess_label} - local estimate vs API calibration points",
                 fontsize=10)
    ax.set_ylabel("Usage %")
    ymax = max(max(local_pct), max(api_pct) if api_pct else 0) + 5
    ax.set_ylim(0, ymax)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
    ax.tick_params(axis="x", labelsize=9)
    ax.legend(fontsize=9)
    ax.grid(True, alpha=0.3)
    plt.tight_layout()

    args.out.parent.mkdir(parents=True, exist_ok=True)
    plt.savefig(args.out, dpi=140, bbox_inches="tight")
    print(f"Saved -> {args.out}")


if __name__ == "__main__":
    main()
