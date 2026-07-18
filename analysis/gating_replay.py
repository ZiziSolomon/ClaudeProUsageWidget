"""gating_replay.py — test the sawtooth-pairing derivation against real turn-state.

Derivation (memory: sawtooth-pairing-derivation): display error just before read k is
    e_k = disp_before_k - pct_k = (L(t_{k-1}) - L(t_k)) / B
where L(t) = billed-but-not-yet-ingested lead, L>0 while a turn is in flight.
Predictions, using spinner state (title_probe.py log) as the busy proxy:
  prev read BUSY, this read idle  ->  e_k > 0   (overshoot; the sawtooth snap-down)
  this read BUSY, prev read idle  ->  e_k < 0   (lag; benign direction)
  both idle                       ->  e_k ~ 0

Busy proxy caveat: the probe records the ACTIVE Windows Terminal tab's title only.
A Claude busy in a background tab looks idle -> misclassification can only WEAKEN
the association, not manufacture it (braille prefix never appears while idle).

Usage:  python analysis/gating_replay.py [--title-log analysis/title_log.jsonl]
"""
import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import sawtooth_metric as sm

BRAILLE = lambda p: bool(p) and any(0x2800 <= ord(c) <= 0x28FF for c in p)


def load_busy_intervals(path: Path):
    """Merge the per-window title log into global [start, end) UTC busy intervals.
    Busy = ANY tracked window's current prefix contains a Braille (spinner) glyph.
    Returns (intervals, coverage_lo, coverage_hi)."""
    tz = datetime.now().astimezone().tzinfo  # log timestamps are naive local
    state = {}      # hwnd -> currently busy?
    events = []     # (ts_utc, hwnd, busy)
    for line in path.read_text(encoding="utf-8").splitlines():
        r = json.loads(line)
        ts = datetime.fromisoformat(r["ts"]).replace(tzinfo=tz).astimezone(timezone.utc)
        busy = False if r.get("gone") else BRAILLE(r.get("pre"))
        events.append((ts, r["hwnd"], busy))
    events.sort(key=lambda e: e[0])
    coverage = (events[0][0], events[-1][0])

    intervals = []
    open_start = None
    for ts, hwnd, busy in events:
        state[hwnd] = busy
        any_busy = any(state.values())
        if any_busy and open_start is None:
            open_start = ts
        elif not any_busy and open_start is not None:
            intervals.append((open_start, ts))
            open_start = None
    if open_start is not None:
        intervals.append((open_start, coverage[1]))
    return intervals, coverage


def busy_at(intervals, t):
    """(is_busy, seconds_into_current_busy_run) at UTC time t."""
    for lo, hi in intervals:
        if lo <= t < hi:
            return True, (t - lo).total_seconds()
    return False, None


def busy_frac(intervals, lo, hi):
    """Fraction of [lo, hi] covered by busy intervals."""
    total = (hi - lo).total_seconds()
    if total <= 0:
        return 0.0
    cov = sum(max(0.0, (min(b, hi) - max(a, lo)).total_seconds())
              for a, b in intervals)
    return cov / total


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--title-log", default=str(Path(__file__).parent / "title_log.jsonl"))
    args = ap.parse_args()

    intervals, (cov_lo, cov_hi) = load_busy_intervals(Path(args.title_log))
    total_busy = sum((b - a).total_seconds() for a, b in intervals)
    print(f"probe coverage {cov_lo:%Y-%m-%d %H:%M}Z .. {cov_hi:%H:%M}Z, "
          f"{len(intervals)} busy runs, {total_busy/60:.1f} min busy total")

    ticks = sm._load_ticks()
    sessions = sm._load_reads_by_session()

    rows = []
    for ss, reads in sorted(sessions.items()):
        prev = None  # (ts, busy, runlen) of previous IN-COVERAGE read, same session
        for ts, pct in reads:
            if not (cov_lo <= ts <= cov_hi):
                prev = None
                continue
            b_now, run_now = busy_at(intervals, ts)
            db = sm._disp_before(ticks, ts)
            e = None if (db is None or pct < sm.FLOOR) else db - pct
            frac = busy_frac(intervals, prev[0], ts) if prev else None
            rows.append({
                "session": ss, "ts": ts, "pct": pct, "db": db, "e": e,
                "busy_now": b_now, "run_now": run_now,
                "busy_prev": prev[1] if prev else None,
                "run_prev": prev[2] if prev else None,
                "frac_between": frac,
            })
            prev = (ts, b_now, run_now)

    print(f"\n{'read (UTC)':19} {'pct':>5} {'disp':>6} {'e':>6}  "
          f"{'now':>9} {'prev':>9} {'busy%':>6}")
    for r in rows:
        def st(b, run):
            if b is None:
                return "?"
            return f"BUSY {run:.0f}s" if b else "idle"
        e_s = f"{r['e']:+6.1f}" if r["e"] is not None else "     -"
        db_s = f"{r['db']:6.1f}" if r["db"] is not None else "     -"
        fr = f"{100*r['frac_between']:5.0f}%" if r["frac_between"] is not None else "     -"
        print(f"{r['ts']:%Y-%m-%d %H:%M:%S} {r['pct']:5.1f} {db_s} {e_s}  "
              f"{st(r['busy_now'], r['run_now']):>9} "
              f"{st(r['busy_prev'], r['run_prev']):>9} {fr:>6}")

    # Contingency summary over scorable reads with a known previous state
    groups = {}
    for r in rows:
        if r["e"] is None or r["busy_prev"] is None:
            continue
        key = (r["busy_prev"], r["busy_now"])
        groups.setdefault(key, []).append(r["e"])
    label = {True: "BUSY", False: "idle"}
    print("\nprediction check (prev-state, now-state -> mean e [derivation predicts]):")
    pred = {(True, False): "e > 0 overshoot", (False, True): "e < 0 lag",
            (False, False): "e ~ 0", (True, True): "sign = run-length diff"}
    for key in [(True, False), (False, True), (False, False), (True, True)]:
        if key not in groups:
            continue
        es = groups[key]
        mean = sum(es) / len(es)
        print(f"  prev {label[key[0]]:4}, now {label[key[1]]:4}:  n={len(es):2d}  "
              f"mean e {mean:+.2f}  (values: {', '.join(f'{x:+.1f}' for x in es)})"
              f"   [{pred[key]}]")


if __name__ == "__main__":
    main()
