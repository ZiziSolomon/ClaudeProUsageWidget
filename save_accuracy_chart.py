"""Save a standalone accuracy chart for the README (best single session)."""
import json, os
from datetime import datetime
from pathlib import Path
from collections import defaultdict
import matplotlib.pyplot as plt
import matplotlib.dates as mdates

JSONL = Path(os.environ["LOCALAPPDATA"]) / "ClaudeUsage" / "usage_data" / "calibration.jsonl"

def _session_key(r):
    dt = datetime.fromisoformat(r["session_start"]).replace(second=0, microsecond=0)
    return dt.isoformat()

records = []
for line in JSONL.read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if not line: continue
    r = json.loads(line)
    r["_ts"] = datetime.fromisoformat(r["scraped_at"])
    records.append(r)
records.sort(key=lambda r: r["_ts"])

sessions = defaultdict(list)
for r in records:
    sessions[_session_key(r)].append(r)

# Pick the May 25 16:00 session (cleanest data); fall back to most data points
target = "2026-05-25T16:00"
best_key = next((k for k in sessions if k.startswith(target)), None)
if best_key is None:
    best_key, _ = max(
        ((k, v) for k, v in sessions.items()
         if len(v) >= 5 and any(r.get("implied_session_budget") for r in v)),
        key=lambda kv: len(kv[1])
    )
best_recs = sessions[best_key]

# Build (timestamp, api_pct, stale_local_pct) triples
timestamps, api_vals, stale_vals = [], [], []
prev = None
for r in best_recs:
    api_pct = r.get("session_pct")
    io_total = r.get("transcript_io_total", 0)
    if api_pct is None:
        prev = r; continue
    timestamps.append(r["_ts"])
    api_vals.append(api_pct)
    if prev and prev.get("implied_session_budget"):
        stale_vals.append(min(100, round(100 * io_total / prev["implied_session_budget"])))
    else:
        stale_vals.append(api_pct)
    prev = r

session_dt = datetime.fromisoformat(best_key)
title = f"Session {session_dt.strftime('%Y-%m-%d %H:%M UTC')}"

fig, ax = plt.subplots(figsize=(9, 4))
ax.plot(timestamps, api_vals,   "o-",  color="#4C9BE8", label="API truth",       lw=2)
ax.plot(timestamps, stale_vals, "s--", color="#E88A4C", label="Stale local est.", lw=2, alpha=0.85)
ax.fill_between(timestamps, api_vals, stale_vals, alpha=0.12, color="#E88A4C")
ax.set_title(title, fontsize=11)
ax.set_ylabel("Usage %")
ax.set_ylim(0, 105)
ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
ax.tick_params(axis="x", labelsize=9)
ax.legend(fontsize=9)
ax.grid(True, alpha=0.3)
plt.tight_layout()

out = Path(__file__).parent / "docs" / "accuracy_sample.png"
out.parent.mkdir(exist_ok=True)
plt.savefig(out, dpi=140, bbox_inches="tight")
print(f"Saved -> {out}")
