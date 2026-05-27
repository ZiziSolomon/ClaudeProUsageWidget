"""Plot local-estimate vs API-truth drift from calibration.jsonl.

For each pair of consecutive calibration points A → B in the same session,
we ask: what would the widget have shown at B if it hadn't recalibrated since A?
  local_stale = min(100, round(100 * io_total_B / budget_A))
  drift = local_stale - api_pct_B

That gap is what the user actually sees when the poll interval is long.
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from collections import defaultdict

import matplotlib.pyplot as plt
import matplotlib.dates as mdates

JSONL = Path(os.environ["LOCALAPPDATA"]) / "ClaudeUsage" / "usage_data" / "calibration.jsonl"


def load_records():
    records = []
    for line in JSONL.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        r = json.loads(line)
        r["_ts"] = datetime.fromisoformat(r["scraped_at"])
        r["_session_start"] = datetime.fromisoformat(r["session_start"])
        records.append(r)
    return sorted(records, key=lambda r: r["_ts"])


def _session_key(r):
    # session_start has sub-second jitter across restarts; truncate to the minute
    dt = r["_session_start"].replace(second=0, microsecond=0)
    return dt.isoformat()


def group_by_session(records):
    sessions = defaultdict(list)
    for r in records:
        sessions[_session_key(r)].append(r)
    return sessions


def compute_drift(session_records):
    """Return list of (ts, api_pct, stale_local_pct, drift) tuples.

    For each record after the first in the session we project forward from the
    previous record's budget, giving the stale estimate the widget would show."""
    points = []
    prev = None
    for r in session_records:
        budget = r.get("implied_session_budget")
        api_pct = r.get("session_pct")
        io_total = r.get("transcript_io_total", 0)
        if api_pct is None:
            prev = r
            continue
        if prev is not None and prev.get("implied_session_budget"):
            stale_local = min(100, round(100 * io_total / prev["implied_session_budget"]))
            drift = stale_local - api_pct
            points.append({
                "ts": r["_ts"],
                "api_pct": api_pct,
                "stale_local": stale_local,
                "drift": drift,
                "gap_mins": (r["_ts"] - prev["_ts"]).total_seconds() / 60,
            })
        prev = r
    return points


def main():
    records = load_records()
    sessions = group_by_session(records)

    # Only sessions with at least 3 calibration points and a resolved budget
    usable = {k: v for k, v in sessions.items()
              if len(v) >= 3 and any(r.get("implied_session_budget") for r in v)}

    if not usable:
        print("Not enough multi-point sessions in calibration.jsonl yet.")
        return

    n = len(usable)
    fig, axes = plt.subplots(n, 2, figsize=(14, 4 * n), squeeze=False)
    fig.suptitle("Widget local estimate vs API truth (drift analysis)", fontsize=13, y=1.01)

    for row, (session_start_str, sess_recs) in enumerate(
            sorted(usable.items(), key=lambda kv: kv[0])):
        points = compute_drift(sess_recs)
        if not points:
            continue

        session_dt = datetime.fromisoformat(session_start_str)
        label = session_dt.strftime("%Y-%m-%d %H:%M UTC")

        timestamps  = [p["ts"] for p in points]
        api_vals    = [p["api_pct"] for p in points]
        stale_vals  = [p["stale_local"] for p in points]
        drift_vals  = [p["drift"] for p in points]
        gap_mins    = [p["gap_mins"] for p in points]

        # Left: pct over time
        ax_pct = axes[row][0]
        ax_pct.plot(timestamps, api_vals,   "o-", color="#4C9BE8", label="API truth", lw=1.5)
        ax_pct.plot(timestamps, stale_vals, "s--", color="#E88A4C", label="Stale local est.", lw=1.5, alpha=0.8)
        ax_pct.fill_between(timestamps, api_vals, stale_vals, alpha=0.15, color="#E88A4C")
        ax_pct.set_title(f"Session {label}", fontsize=9)
        ax_pct.set_ylabel("Usage %")
        ax_pct.set_ylim(0, 105)
        ax_pct.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
        ax_pct.tick_params(axis="x", labelsize=7)
        ax_pct.legend(fontsize=8)
        ax_pct.grid(True, alpha=0.3)

        # Right: drift (stale - truth) vs time gap since last calibration
        ax_drift = axes[row][1]
        scatter = ax_drift.scatter(gap_mins, drift_vals,
                                   c=drift_vals, cmap="RdYlGn_r",
                                   vmin=-10, vmax=10, s=60, zorder=3)
        ax_drift.axhline(0, color="gray", lw=0.8, ls="--")
        ax_drift.axhline(5,  color="orange", lw=0.6, ls=":", label="±5 pp band")
        ax_drift.axhline(-5, color="orange", lw=0.6, ls=":")
        ax_drift.set_xlabel("Gap since last calibration (min)")
        ax_drift.set_ylabel("Drift (stale − truth, pp)")
        ax_drift.set_title("Drift vs calibration gap", fontsize=9)
        ax_drift.legend(fontsize=8)
        ax_drift.grid(True, alpha=0.3)
        fig.colorbar(scatter, ax=ax_drift, label="drift pp")

    plt.tight_layout()
    out = Path(__file__).parent / "drift_analysis.png"
    plt.savefig(out, dpi=140, bbox_inches="tight")
    print(f"Saved -> {out}")
    import subprocess
    subprocess.Popen(["explorer", str(out)])


if __name__ == "__main__":
    main()
