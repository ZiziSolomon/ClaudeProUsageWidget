"""Generate accuracy chart for README: continuous local estimate line + API-truth dots."""
import json, os, re
from datetime import datetime, date, timedelta, timezone
from pathlib import Path
from collections import defaultdict
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

DATA = Path(os.environ["LOCALAPPDATA"]) / "ClaudeUsage" / "usage_data"
JSONL = DATA / "calibration.jsonl"
LOG   = DATA / "widget_run.log"

SESSION_DATE = date(2026, 5, 27)
SESSION_START_UTC = datetime(2026, 5, 27, 8, 50, tzinfo=timezone.utc)


def load_api_points():
    """Calibration records for today's session that have a resolved budget."""
    points = []
    for line in JSONL.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        r = json.loads(line)
        if not r["session_start"].startswith("2026-05-27"):
            continue
        if r.get("implied_session_budget") and r.get("session_pct") is not None:
            points.append({
                "ts":  datetime.fromisoformat(r["scraped_at"]),
                "pct": r["session_pct"],
            })
    # Deduplicate by minute (multiple rapid calls at same % are noise)
    seen, deduped = set(), []
    for p in points:
        key = p["ts"].strftime("%H:%M")
        if key not in seen:
            seen.add(key)
            deduped.append(p)
    return deduped


def load_local_estimates():
    """Parse [HH:MM:SS] lines from the last widget-startup block in the log.

    The log is appended across restarts; splitting on 'Widget HTTP' gives us
    one block per run. The last block is the current session."""
    all_lines = LOG.read_text(encoding="utf-8", errors="ignore").splitlines()
    # Find the last "Widget HTTP" restart marker
    last_start = 0
    for i, line in enumerate(all_lines):
        if line.startswith("Widget HTTP"):
            last_start = i
    block = all_lines[last_start:]

    pattern = re.compile(r"^\[(\d{2}:\d{2}:\d{2})\].*pct=(\d+(?:\.\d+)?)")
    points = []
    for line in block:
        m = pattern.match(line)
        if not m:
            continue
        h, mi, s = map(int, m.group(1).split(":"))
        ts = datetime(SESSION_DATE.year, SESSION_DATE.month, SESSION_DATE.day,
                      h, mi, s, tzinfo=timezone.utc)
        points.append({"ts": ts, "pct": float(m.group(2))})
    return points


api_pts   = load_api_points()
local_pts = load_local_estimates()

if not local_pts:
    print("No local estimate data found for today's session.")
    raise SystemExit(1)

local_ts  = [p["ts"] for p in local_pts]
local_pct = [p["pct"] for p in local_pts]
api_ts    = [p["ts"] for p in api_pts]
api_pct   = [p["pct"] for p in api_pts]

fig, ax = plt.subplots(figsize=(9, 4))

ax.plot(local_ts, local_pct, "-", color="#4C9BE8", lw=1.8, label="Local estimate (live)")
ax.scatter(api_ts, api_pct, color="#E84C4C", s=80, zorder=5, label="API truth (calibration call)")

# Vertical dotted lines at each API call to make the cadence obvious
for ts in api_ts:
    ax.axvline(ts, color="#E84C4C", lw=0.6, ls=":", alpha=0.5)

ax.set_title("Session 2026-05-27 — local estimate vs API calibration points", fontsize=10)
ax.set_ylabel("Usage %")
ax.set_ylim(0, max(max(local_pct), max(api_pct) if api_pct else 0) + 5)
ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
ax.tick_params(axis="x", labelsize=9)
ax.legend(fontsize=9)
ax.grid(True, alpha=0.3)
plt.tight_layout()

out = Path(__file__).parent / "docs" / "accuracy_sample.png"
out.parent.mkdir(exist_ok=True)
plt.savefig(out, dpi=140, bbox_inches="tight")
print(f"Saved -> {out}")
