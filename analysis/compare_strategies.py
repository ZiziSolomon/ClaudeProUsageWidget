#!/usr/bin/env python3
"""Compare two estimation STRATEGIES on historic data by prediction error.

Question: when a reading disagrees with our estimate, is it better to brute-force
re-derive the scalar budget B (Model A, the old big_diff / budget path) or to
anchor the LEVEL to truth and take a conservative clean-min SLOPE (Model B, the
SessionFactor path)? The worry (Ezekiel): B is a single scalar, so re-deriving it
to fit a reading only works while the token MIX stays colinear early->late. If a
weight is wrong (e.g. Fable) or a token type is unaccounted, B fits-then-drifts;
and on the under-predict direction it bakes off-laptop contamination into B.

We can't add historic endpoint calls, but we CAN replay each strategy forward
through the readings we already logged and measure, at each reading, what the
strategy WOULD have displayed vs the fresh truth. We report the error
distributions per strategy, split CLEAN vs DIRTY (dirty = off-laptop overlap;
that's where contaminating B should hurt Model A most).

Both strategies mirror widget_updater:
  A (re-derive B): predict 100*io/B; on |pred-truth|>RECAL_DISCREPANCY_PP
     re-derive B = io/(truth/100). (No SessionFactor; the fallback/old path.)
  B (SessionFactor): predict anchor_pct + s*(io-anchor_io), where s =
     min((pct+bias)/io) over readings so far (the clean SLOPE) and the anchor is
     the last reading's (pct, io). Level always snaps to truth; slope is the
     contamination-robust min. (The current primary path.)
  C (asymmetric / off-laptop-aware): like B but makes the off-laptop asymmetry
     explicit. Off-laptop usage only inflates truth, so on under-predict
     (truth>pred) jump the LEVEL but leave the slope; on over-predict
     (truth<pred, can't be off-laptop) correct the slope. (Ezekiel's "treat
     off-laptop differently".)

Error at reading i is measured BEFORE that reading updates the strategy (i.e.
what the user would have seen just before the poll landed). The first reading of
a session seeds each strategy and is not scored (nothing to predict from yet).

Run:  python analysis/compare_strategies.py
      python analysis/compare_strategies.py --json
      python analysis/compare_strategies.py --recal-pp 2   # try a 2pp A threshold
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import widget_updater as wu
import analysis.backtest_fit as bt

FLOOR_BIAS = wu.API_PCT_FLOOR_BIAS_PP


def replay_strategy_A(readings, recal_pp):
    """Re-derive-B strategy. Yields prediction error (pp) at each reading after
    the first. B seeded from reading 0; re-derived whenever a reading disagrees
    with the prediction by > recal_pp (mirrors _adopt_api_pct's big_diff)."""
    errs = []
    r0 = readings[0]
    B = r0["io"] / ((r0["api_pct"] + FLOOR_BIAS) / 100.0)   # seed from first reading
    for r in readings[1:]:
        pred = 100.0 * r["io"] / B
        truth = r["api_pct"] + FLOOR_BIAS
        errs.append(abs(pred - truth))
        if abs(pred - truth) > recal_pp:          # disagreement -> re-derive B
            B = r["io"] / (truth / 100.0)
    return errs


def replay_strategy_B(readings):
    """SessionFactor strategy. Level anchors to the last reading; slope is the
    running min of (pct+bias)/io (clean burn rate, off-laptop-robust)."""
    errs = []
    s_min = (readings[0]["api_pct"] + FLOOR_BIAS) / readings[0]["io"]
    anchor_pct = readings[0]["api_pct"] + FLOOR_BIAS
    anchor_io  = readings[0]["io"]
    for r in readings[1:]:
        pred = anchor_pct + s_min * (r["io"] - anchor_io)
        truth = r["api_pct"] + FLOOR_BIAS
        errs.append(abs(pred - truth))
        # update: fold this reading's slope into the min, re-anchor level to truth
        s_min = min(s_min, truth / r["io"])
        anchor_pct, anchor_io = truth, r["io"]
    return errs


def replay_strategy_C(readings):
    """Asymmetric / off-laptop-aware strategy. Off-laptop usage only ever makes
    truth HIGHER than we predicted (it adds usage we can't see), so the sign of
    the miss is diagnostic:
      truth > pred  (under-predict): likely off-laptop -> jump the LEVEL to truth
        but leave the slope alone (don't let contamination corrupt B).
      truth < pred  (over-predict):  off-laptop can't cause this, so our slope was
        too steep -> correct it (re-derive slope from this clean point).
    Level always anchors to truth either way."""
    errs = []
    s = (readings[0]["api_pct"] + FLOOR_BIAS) / readings[0]["io"]
    anchor_pct = readings[0]["api_pct"] + FLOOR_BIAS
    anchor_io  = readings[0]["io"]
    for r in readings[1:]:
        pred = anchor_pct + s * (r["io"] - anchor_io)
        truth = r["api_pct"] + FLOOR_BIAS
        errs.append(abs(pred - truth))
        if truth < pred and r["io"] > 0:        # over-predict -> slope too steep, fix it
            s = truth / r["io"]
        # under-predict -> leave slope (off-laptop, not our error)
        anchor_pct, anchor_io = truth, r["io"]  # level always re-anchors to truth
    return errs


def _summary(errs):
    if not errs:
        return None
    errs = sorted(errs)
    n = len(errs)
    return {
        "n":        n,
        "mean":     round(statistics.mean(errs), 2),
        "median":   round(errs[n // 2], 2),
        "p90":      round(errs[int(n * 0.9)], 2),
        "max":      round(errs[-1], 2),
        "within2":  round(sum(1 for e in errs if e <= 2) / n, 3),
    }


def analyze(sessions, classify, recal_pp):
    buckets = {p: {"A": [], "B": [], "C": []}
               for p in ("clean", "dirty", "unknown")}
    for ss, readings in sessions.items():
        if len(readings) < 2:
            continue
        purity = classify(bt.hv._parse_ts(ss))
        buckets[purity]["A"].extend(replay_strategy_A(readings, recal_pp))
        buckets[purity]["B"].extend(replay_strategy_B(readings))
        buckets[purity]["C"].extend(replay_strategy_C(readings))
    out = {}
    for purity, d in buckets.items():
        out[purity] = {k: _summary(v) for k, v in d.items()}
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--min-grabs", type=int, default=2)
    ap.add_argument("--recal-pp", type=float, default=wu.RECAL_DISCREPANCY_PP,
                    help=f"Model A re-derive threshold (default {wu.RECAL_DISCREPANCY_PP}).")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    import contextlib, io
    sink = sys.stderr if args.json else sys.stdout
    with contextlib.redirect_stdout(sink):
        sessions = bt.build_grabs(args.min_grabs)
        classify, _ = bt.load_purity()
    result = analyze(sessions, classify, args.recal_pp)

    if args.json:
        print(json.dumps(result, indent=2))
        return

    print(f"\n=== Strategy comparison (A=re-derive B @ {args.recal_pp}pp, "
          f"B=SessionFactor) ===")
    print("prediction error in pp, per reading (lower=better). A_re-derive vs B_SessionFactor.\n")
    hdr = f"{'purity':8} {'strat':14} {'n':>4} {'mean':>6} {'med':>6} {'p90':>6} {'max':>7} {'<=2pp':>7}"
    print(hdr)
    print("-" * len(hdr))
    for purity in ("clean", "dirty", "unknown"):
        for strat, key in (("A re-derive B", "A"), ("B SessionFactor", "B"),
                           ("C asymmetric", "C")):
            s = result[purity][key]
            if s is None:
                continue
            print(f"{purity:8} {strat:14} {s['n']:>4} {s['mean']:>6} {s['median']:>6} "
                  f"{s['p90']:>6} {s['max']:>7} {s['within2']:>7.0%}")
        print()
    print("Read: A re-derives B on every disagreement (contamination-prone). B takes "
          "the clean-min slope. C is sign-aware: on under-predict (likely off-laptop) "
          "it jumps the LEVEL but leaves the slope; on over-predict it fixes the slope. "
          "Watch DIRTY: C and B should beat A by not baking off-laptop use into the slope.")


if __name__ == "__main__":
    main()
