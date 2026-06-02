"""Sanity-check scaffold for weighted-token calibration.

Produces two CSVs and a per-session comparison:

  transcript_entries.csv - one row per assistant transcript entry, every token
                           type split out, plus the published-weight total.
  endpoint_checks.csv    - one row per API endpoint check (from calibration.jsonl),
                           with sessionly %, weekly % (best-effort from run log),
                           check time, and time remaining in the 5h session.

Then for each endpoint check it sums the account-wide weighted transcript tokens
in [session_start, check_time] and prints the implied session budget. If the
published weights are right, that budget should be roughly constant within a
session (and across sessions if the underlying token limit is fixed).

Published weights are Anthropic's relative token costs (base input = 1x):
    input            1.00
    cache_write_5m   1.25
    cache_write_1h   2.00
    cache_read       0.10
    output           5.00
"""
import csv
import glob
import json
import os
from datetime import datetime, timedelta, timezone

# --- weights -----------------------------------------------------------------
W_INPUT   = 1.00
W_CC_5M   = 1.25
W_CC_1H   = 2.00
W_CR      = 0.10
W_OUTPUT  = 5.00

SESSION_HOURS = 5

HERE       = os.path.dirname(os.path.abspath(__file__))
DATA       = os.path.expandvars(r"%LOCALAPPDATA%\ClaudeUsage\usage_data")
CALIB      = os.path.join(DATA, "calibration.jsonl")
RUNLOG     = os.path.join(DATA, "widget_run.log")
PROJECTS   = os.path.expanduser("~/.claude/projects")
OUT_TX     = os.path.join(HERE, "transcript_entries.csv")
OUT_EP     = os.path.join(HERE, "endpoint_checks.csv")


def _ts(s):
    """Parse an ISO timestamp (handles trailing Z) to an aware UTC datetime."""
    if not s:
        return None
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)


def weighted(i, c5, c1, cr, o):
    return i * W_INPUT + c5 * W_CC_5M + c1 * W_CC_1H + cr * W_CR + o * W_OUTPUT


# --- 1. transcript entries ---------------------------------------------------
def load_transcript_entries():
    rows = []
    for f in glob.glob(os.path.join(PROJECTS, "**", "*.jsonl"), recursive=True):
        for line in open(f, encoding="utf-8"):
            if '"usage"' not in line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get("type") != "assistant":
                continue
            u = (r.get("message") or {}).get("usage")
            if not u:
                continue
            t = _ts(r.get("timestamp"))
            if t is None:
                continue
            cc = u.get("cache_creation") or {}
            i  = u.get("input_tokens", 0)
            c5 = cc.get("ephemeral_5m_input_tokens", 0)
            c1 = cc.get("ephemeral_1h_input_tokens", 0)
            cr = u.get("cache_read_input_tokens", 0)
            o  = u.get("output_tokens", 0)
            rows.append({
                "timestamp":       t.isoformat(),
                "project":         os.path.basename(os.path.dirname(f)),
                "session_id":      r.get("sessionId", ""),
                "model":           (r.get("message") or {}).get("model", ""),
                "input":           i,
                "cache_write_5m":  c5,
                "cache_write_1h":  c1,
                "cache_read":      cr,
                "output":          o,
                "in_plus_out":     i + o,
                "weighted":        round(weighted(i, c5, c1, cr, o), 1),
            })
    rows.sort(key=lambda x: x["timestamp"])
    return rows


# --- 2. weekly timeline from the run log (best-effort) -----------------------
def load_weekly_timeline():
    """Pair each 'Session X% | weekly Y%' print with the nearest preceding
    '[YYYY-MM-DD HH:MM:SS]' timestamp line. Returns sorted [(dt, weekly_pct)]."""
    out = []
    last_dt = None
    if not os.path.exists(RUNLOG):
        return out
    for line in open(RUNLOG, encoding="utf-8", errors="replace"):
        s = line.strip()
        if s.startswith("[") and len(s) > 20 and s[1:5].isdigit():
            try:
                last_dt = datetime.strptime(s[1:20], "%Y-%m-%d %H:%M:%S").replace(
                    tzinfo=timezone.utc)
            except Exception:
                pass
        elif "| weekly" in s and last_dt is not None:
            try:
                wk = float(s.split("| weekly")[1].split("%")[0].strip())
                out.append((last_dt, wk))
            except Exception:
                pass
    out.sort()
    return out


def nearest_weekly(timeline, dt, tol_secs=900):
    best, bestd = None, None
    for t, wk in timeline:
        d = abs((t - dt).total_seconds())
        if bestd is None or d < bestd:
            best, bestd = wk, d
    return best if (bestd is not None and bestd <= tol_secs) else None


# --- 3. endpoint checks ------------------------------------------------------
def load_endpoint_checks(weekly_tl):
    rows = []
    for line in open(CALIB, encoding="utf-8"):
        r = json.loads(line)
        check = _ts(r.get("scraped_at"))
        ss    = _ts(r.get("session_start"))
        if check is None:
            continue
        se = ss + timedelta(hours=SESSION_HOURS) if ss else None
        secs_rem = (se - check).total_seconds() if se else None
        rows.append({
            "check_time":        check.isoformat(),
            "session_pct":       r.get("session_pct"),
            "weekly_pct":        nearest_weekly(weekly_tl, check),
            "session_start":     ss.isoformat() if ss else "",
            "session_end":       se.isoformat() if se else "",
            "secs_remaining":    round(secs_rem) if secs_rem is not None else "",
            "widget_io_total":   r.get("transcript_io_total"),
            "trigger":           r.get("trigger"),
        })
    rows.sort(key=lambda x: x["check_time"])
    return rows


def write_csv(path, rows):
    if not rows:
        print(f"  (no rows for {path})")
        return
    with open(path, "w", newline="", encoding="utf-8") as fh:
        wtr = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        wtr.writeheader()
        wtr.writerows(rows)
    print(f"  wrote {len(rows):>5} rows -> {path}")


# --- 4. sanity comparison ----------------------------------------------------
def sanity(tx_rows, ep_rows):
    """For each endpoint check with a real session_pct>0, sum account-wide
    weighted tokens in [session_start, check_time] and back out the implied
    session budget = weighted / (pct/100)."""
    tx = [(_ts(r["timestamp"]), r["weighted"]) for r in tx_rows]
    tx.sort()
    print("\nSANITY CHECK - implied session budget per endpoint check")
    print("(weighted tokens in window / session_pct; should be ~constant if weights are right)\n")
    print(f"{'check_time':20} {'sess%':>6} {'weighted_in_window':>18} {'implied_budget':>15}")
    cur_start = None
    for e in ep_rows:
        pct = e["session_pct"]
        ss  = _ts(e["session_start"])
        ct  = _ts(e["check_time"])
        if not pct or pct <= 0 or ss is None:
            continue
        wsum = sum(w for t, w in tx if ss <= t <= ct)
        if e["session_start"] != cur_start:
            cur_start = e["session_start"]
            print(f"--- session {ss.isoformat()} ---")
        budget = wsum / (pct / 100.0) if pct else 0
        print(f"{ct.strftime('%m-%d %H:%M:%S'):20} {pct:>6} "
              f"{wsum:>18,.0f} {budget:>15,.0f}")


def main():
    print("Loading transcript entries ...")
    tx = load_transcript_entries()
    write_csv(OUT_TX, tx)
    print("Loading weekly timeline from run log ...")
    wtl = load_weekly_timeline()
    print(f"  {len(wtl)} weekly prints found")
    print("Loading endpoint checks ...")
    ep = load_endpoint_checks(wtl)
    write_csv(OUT_EP, ep)
    sanity(tx, ep)


if __name__ == "__main__":
    main()
