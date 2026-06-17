"""One-off diagnostic: plot the local estimate as if it ran on a SINGLE fixed
budget (the one implied by the LAST endpoint read), with NO mid-session budget
re-derivations, and overlay the endpoint reads as dots that don't affect the line.

This isolates the estimator's raw token-tracking from the budget-swinging caused
by the lower-bound ratchet (see widget_run.log "budget lb clamp" lines). If the
smooth line tracks the dots, the token tracking is sound and the corrections were
the problem.

  line(t)  = 100 * weighted_io(t) / B_fixed
  B_fixed  = weighted_io(last_read) / (last_read_pct / 100)

weighted_io(t) is reconstructed from the fine-grained raw-io ticks in
widget_run.log ("[ts] in=.. out=.. pct=..") scaled by the weighted:raw ratio
measured at the nearest calibration read (transcript_weighted_io / transcript_io_total).

Usage:  python analysis/fixed_budget_chart.py [--session "2026-06-17T06:50..."] [--out PATH]
        defaults to the most recent session in the log, opens the PNG.
"""
import argparse
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

DATA = Path(os.environ["LOCALAPPDATA"]) / "ClaudeUsage" / "usage_data"
LOG = DATA / "widget_run.log"
CALIB = DATA / "calibration.jsonl"

TICK = re.compile(r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\] in=(\d+) out=(\d+) pct=([\d.]+)")


def load_calib(session_start: str) -> list[dict]:
    """Endpoint reads for the session: ts, pct, raw_io, weighted_io."""
    out = []
    for line in CALIB.read_text(encoding="utf-8", errors="ignore").splitlines():
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if r.get("session_start") != session_start:
            continue
        if r.get("session_pct") is None:
            continue
        out.append({
            "ts":  datetime.fromisoformat(r["scraped_at"]),
            "pct": float(r["session_pct"]),
            "raw": r.get("transcript_io_total") or 0,
            "wtd": r.get("transcript_weighted_io") or 0,
        })
    out.sort(key=lambda d: d["ts"])
    return out


def load_ticks(session_start: str, lo: datetime, hi: datetime) -> list[dict]:
    """Fine raw-io ticks from the run log within [lo, hi]."""
    ticks = []
    for line in LOG.read_text(encoding="utf-8", errors="ignore").splitlines():
        m = TICK.match(line)
        if not m:
            continue
        # Log timestamps are LOCAL wall-clock (BST/GMT), calibration scraped_at is
        # UTC. Treat the naive log time as system-local and convert to UTC so both
        # series share one time base. .astimezone() on a naive dt assumes local.
        ts = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").astimezone(timezone.utc)
        if not (lo <= ts <= hi):
            continue
        ticks.append({"ts": ts, "raw": int(m.group(2)) + int(m.group(3))})
    ticks.sort(key=lambda d: d["ts"])
    return ticks


def nearest_ratio(t: datetime, calib: list[dict]) -> float:
    """weighted:raw ratio at the calibration read nearest in time to t."""
    best, bestdt = None, None
    for c in calib:
        if c["raw"] <= 0 or c["wtd"] <= 0:
            continue
        dt = abs((c["ts"] - t).total_seconds())
        if bestdt is None or dt < bestdt:
            best, bestdt = c["wtd"] / c["raw"], dt
    return best if best is not None else 1.0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--session", help="session_start ISO string; default = latest in calibration log")
    ap.add_argument("--out", type=Path, default=Path(os.environ.get("TEMP", ".")) / "fixed_budget.png")
    args = ap.parse_args()

    # Resolve session.
    sessions = []
    for line in CALIB.read_text(encoding="utf-8", errors="ignore").splitlines():
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        ss = r.get("session_start")
        if ss and ss not in sessions:
            sessions.append(ss)
    session = args.session or (sessions[-1] if sessions else None)
    if not session:
        raise SystemExit("No sessions found in calibration log.")

    calib = load_calib(session)
    reads = [c for c in calib if c["wtd"] > 0]   # reads with a usable weighted-io
    if not reads:
        raise SystemExit(f"No endpoint reads with weighted io for session {session}.")

    last = reads[-1]
    b_fixed = last["wtd"] / (last["pct"] / 100.0)

    sess_start = datetime.fromisoformat(session)
    ticks = load_ticks(session, sess_start, last["ts"])
    if not ticks:
        raise SystemExit("No raw-io ticks in the log window.")

    # Reconstruct weighted-io(t) from raw ticks scaled by the local weighted:raw
    # ratio, then the no-correction line = 100 * wtd_io / B_fixed.
    xs, ys = [], []
    for tk in ticks:
        wtd = tk["raw"] * nearest_ratio(tk["ts"], reads)
        xs.append(tk["ts"])
        ys.append(100.0 * wtd / b_fixed)

    fig, ax = plt.subplots(figsize=(10, 4.5))
    ax.plot(xs, ys, "-", color="#4C9BE8", lw=1.8,
            label=f"Estimate on FIXED budget (B={b_fixed/1e6:.2f}M, from last read)")
    ax.scatter([c["ts"] for c in calib], [c["pct"] for c in calib],
               color="#E84C4C", s=70, zorder=5, label="claude.ai endpoint reads")
    for c in calib:
        ax.annotate(f"{c['pct']:.0f}", (c["ts"], c["pct"]),
                    textcoords="offset points", xytext=(0, 8),
                    ha="center", fontsize=8, color="#E84C4C")

    ax.set_title("No-correction estimate vs endpoint reads "
                 f"(session {sess_start.astimezone().strftime('%Y-%m-%d %H:%M')})",
                 fontsize=10)
    ax.set_ylabel("Usage %")
    ax.set_ylim(0, max(max(ys), max(c["pct"] for c in calib)) + 5)
    # Both series are UTC-aware; label the axis in LOCAL wall-clock so the times
    # match what you'd read off the clock (and the log).
    local_tz = datetime.now().astimezone().tzinfo
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M", tz=local_tz))
    ax.grid(True, alpha=0.25)
    ax.legend(fontsize=8, loc="upper left")
    fig.tight_layout()
    fig.savefig(args.out, dpi=110)
    print(f"Saved -> {args.out}")
    print(f"B_fixed = {b_fixed:,.0f} (from last read: {last['pct']:.0f}% @ wtd={last['wtd']:,})")


if __name__ == "__main__":
    main()
