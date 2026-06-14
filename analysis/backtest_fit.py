#!/usr/bin/env python3
"""Backtest the CURRENT shipped token weights against historic data.

Question this answers: "Take today's per-model weights (widget_updater.MODEL_WEIGHTS,
weighted_v5) and replay every recorded session — how well do they fit the API truth?"

This is an OFFLINE diagnostic for us, NOT part of the widget's live self-calibration.
It does not write learned_weights.json or touch runtime state. Its purpose is to tell
us whether a re-burn is worth it before we spend one.

Pipeline (reuses the audited pieces rather than reimplementing):
  1. historic_validate.load_messages()  -> raw transcript messages, deduped by id,
     with per-model token components. (Same dedup/segmentation as the widget.)
  2. For each calibration.jsonl grab: historic_validate.reconstruct() gives the
     cumulative per-model token vector at that grab's (session_start, scraped_at).
  3. widget_updater._weighted_io() collapses that vector to weighted tokens using
     the CURRENT MODEL_WEIGHTS. (We build a minimal state dict shaped like the one
     _weighted_io expects, so we exercise the exact shipped weighting code.)
  4. Per session we then ask two things of the (weighted_io, api_pct) point cloud:

     A. SessionFactor consistency (the core weight test). The widget models
        pct = s * weighted_io for a single per-session slope s. If the weights are
        right, every above-floor grab in a session implies nearly the SAME s. We
        report, per session, the spread of per-grab s = (pct+0.5)/io: max/min ratio.
        Tight (~1.0) across MODELS in a mixed session = the cross-model weights are
        consistent. A model that systematically pulls s away = that model's weight
        is off. This is weight-only and budget-free (s absorbs the unknown B).

     B. Stale-projection drift (what the user would have seen). Like plot_drift.py
        but recomputed with current weights: anchor on the previous grab's implied
        budget-per-pp, project forward, compare to truth. Surfaces absolute pp error.

Run:  python analysis/backtest_fit.py            # text report
      python analysis/backtest_fit.py --json      # machine-readable
      python analysis/backtest_fit.py --min-grabs 3
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Make the repo root importable whether run from root or analysis/.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import widget_updater as wu
from analysis.burn import historic_validate as hv
from analysis.burn import historic_step3 as h3

FLOOR_BIAS = wu.API_PCT_FLOOR_BIAS_PP
# The widget only derives its SessionFactor from above-floor grabs (claude.ai
# reports floored ints; below this, the rounding noise dwarfs the signal). We
# mirror that exactly so the s_ratio metric is comparable to the live estimator
# and isn't blown up by a near-zero-io, pct=0/1 early grab.
PCT_FLOOR = wu.CALIBRATION_PCT_FLOOR
COMPONENTS = hv.COMPONENTS  # input, output, cw1h, cw5m, cread

# historic_validate component keys -> the per-model state keys _weighted_io reads.
_HV_TO_STATE = {
    "input":  "input",
    "output": "output",
    "cw1h":   "cache_write_1h",
    "cw5m":   "cache_write_5m",
    "cread":  "cache_read",
}


def _state_from_reconstruction(rec: dict) -> dict:
    """Shape a reconstruct() result into the minimal state dict _weighted_io wants:
    a `by_model` map carrying the full cache vector, plus matching global counters
    (so _weighted_io takes its per-model path rather than the fallback)."""
    by_model = {}
    g_input = g_output = g_cw1h = g_cw5m = g_cread = 0
    for model, comp in rec["by_model"].items():
        by_model[model] = {
            "input":          comp["input"],
            "output":         comp["output"],
            "cache_write_1h": comp["cw1h"],
            "cache_write_5m": comp["cw5m"],
            "cache_read":     comp["cread"],
        }
        g_input  += comp["input"]
        g_output += comp["output"]
        g_cw1h   += comp["cw1h"]
        g_cw5m   += comp["cw5m"]
        g_cread  += comp["cread"]
    return {
        "by_model":       by_model,
        "input_tokens":   g_input,
        "output_tokens":  g_output,
        "cache_write_1h": g_cw1h,
        "cache_write_5m": g_cw5m,
        "cache_read":     g_cread,
    }


def _dominant_model(rec: dict) -> str:
    """The model class accounting for the most weighted output this grab — used to
    label which model is driving a session's SessionFactor."""
    best, best_w = "opus", -1.0
    ew = wu._effective_weights()
    for model, comp in rec["by_model"].items():
        mc = wu._model_class(model)
        w = ew.get(mc, wu.DEFAULT_WEIGHTS)
        score = comp["output"] * w["output"] + comp["cw1h"] * w["cache_write_1h"]
        if score > best_w:
            best, best_w = mc, score
    return best


def load_purity():
    """Return (classify(session_start_dt) -> 'clean'|'dirty'|'unknown', available).

    Reuses historic_step3's off-laptop export so the backtest agrees with the
    weight-validation work on which sessions are contaminated by web/mobile use.
    A session is CLEAN only if no off-laptop message overlaps its window AND the
    window ends before the export's last-covered instant; windows extending past
    that are UNKNOWN (post-export off-laptop use is invisible). Degrades to
    all-unknown if the export zip is missing, so the backtest still runs."""
    try:
        off, export_end = h3.load_offlaptop()
    except (FileNotFoundError, OSError, KeyError) as e:
        print(f"[purity] off-laptop export unavailable ({e}); "
              f"sessions left unclassified")
        return (lambda dt: "unknown"), False

    def classify(start_dt):
        cls = h3.classify_session(start_dt, off, export_end)
        if cls == "clean" and (start_dt + hv.timedelta(hours=hv.SESSION_HOURS)) > export_end:
            return "unknown"   # window runs past export coverage -> can't confirm clean
        return cls

    return classify, True


def build_grabs(min_grabs: int) -> dict[str, list[dict]]:
    """Reconstruct weighted tokens at every above-floor calibration grab, grouped
    by session_start. Only sessions with >= min_grabs usable grabs are returned."""
    msgs = hv.load_messages()
    rows = hv.load_calibration()

    sessions: dict[str, list[dict]] = {}
    for r in rows:
        ss = r.get("session_start")
        api_pct = r.get("session_pct")
        if not ss or api_pct is None:
            continue
        if api_pct < PCT_FLOOR:   # mirror the widget: SessionFactor is above-floor only
            continue
        session_start = hv._parse_ts(ss)
        scraped_at = hv._parse_ts(r["scraped_at"])
        rec = hv.reconstruct(msgs, session_start, scraped_at)
        state = _state_from_reconstruction(rec)
        io = wu._weighted_io(state)
        if io <= 0:
            continue
        sessions.setdefault(ss, []).append({
            "scraped_at": r["scraped_at"],
            "api_pct":    api_pct,
            "io":         io,
            "s_grab":     (api_pct + FLOOR_BIAS) / io,  # per-grab SessionFactor
            "dom_model":  _dominant_model(rec),
            "n_messages": rec["n_messages"],
        })

    # keep sessions ordered by grab time; filter by count
    out = {}
    for ss, grabs in sessions.items():
        grabs.sort(key=lambda g: g["scraped_at"])
        if len(grabs) >= min_grabs:
            out[ss] = grabs
    return out


def analyze(sessions: dict[str, list[dict]], classify) -> dict:
    """Per-session SessionFactor-consistency (A) + stale-projection drift (B).

    `classify` maps a session-start datetime to 'clean'/'dirty'/'unknown' so a
    high s_ratio caused by off-laptop use (DIRTY) isn't misread as a weight
    misfit — only CLEAN sessions are a fair test of the weights."""
    per_session = []
    all_abs_drift = []
    for ss, grabs in sorted(sessions.items()):
        purity = classify(hv._parse_ts(ss))
        s_vals = [g["s_grab"] for g in grabs]
        s_min, s_max = min(s_vals), max(s_vals)
        ratio = s_max / s_min if s_min > 0 else float("inf")

        # B: stale projection. Anchor on each grab, project to the next using that
        # anchor's slope (pct/io), compare to truth. Mirrors plot_drift but with
        # current weights and the SessionFactor LEVEL+SLOPE form.
        drifts = []
        for prev, cur in zip(grabs, grabs[1:]):
            s_anchor = prev["s_grab"]
            proj = prev["api_pct"] + FLOOR_BIAS + s_anchor * (cur["io"] - prev["io"])
            drift = round(proj) - cur["api_pct"]  # widget floors to int for display
            drifts.append(drift)
            all_abs_drift.append(abs(drift))

        models = sorted({g["dom_model"] for g in grabs})
        per_session.append({
            "session_start": ss,
            "purity":        purity,
            "n_grabs":       len(grabs),
            "models":        models,
            "mixed":         len(models) > 1,
            "s_ratio":       round(ratio, 3),
            "max_abs_drift": max((abs(d) for d in drifts), default=0),
            "drifts":        drifts,
        })

    n = len(all_abs_drift)
    # Clean sessions are the fair weight test; report their s_ratio band separately.
    clean_ratios = [r["s_ratio"] for r in per_session if r["purity"] == "clean"]
    summary = {
        "n_sessions":        len(per_session),
        "n_clean":           sum(1 for r in per_session if r["purity"] == "clean"),
        "n_dirty":           sum(1 for r in per_session if r["purity"] == "dirty"),
        "n_unknown":         sum(1 for r in per_session if r["purity"] == "unknown"),
        "n_projection_pts":  n,
        "mean_abs_drift_pp": round(sum(all_abs_drift) / n, 2) if n else 0,
        "max_abs_drift_pp":  max(all_abs_drift, default=0),
        "within_2pp_frac":   round(sum(1 for d in all_abs_drift if d <= 2) / n, 3) if n else 0,
        "clean_s_ratio_max": round(max(clean_ratios), 3) if clean_ratios else None,
        "io_unit":           wu.IO_UNIT,
    }
    return {"summary": summary, "sessions": per_session}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--min-grabs", type=int, default=3,
                    help="Minimum usable grabs per session (default 3).")
    ap.add_argument("--json", action="store_true", help="Emit JSON, not a table.")
    args = ap.parse_args()

    # Data loading (hv.load_messages, load_purity) prints progress to stdout.
    # In --json mode that would corrupt the JSON, so divert those to stderr.
    import contextlib
    load_sink = sys.stderr if args.json else sys.stdout
    with contextlib.redirect_stdout(load_sink):
        sessions = build_grabs(args.min_grabs)
        if not sessions:
            sys.exit("No sessions with enough above-floor grabs in calibration.jsonl.")
        classify, _ = load_purity()
    result = analyze(sessions, classify)

    if args.json:
        print(json.dumps(result, indent=2))
        return

    s = result["summary"]
    print(f"\n=== Backtest of CURRENT weights ({s['io_unit']}) against historic data ===")
    print(f"sessions: {s['n_sessions']}  "
          f"(clean {s['n_clean']} / dirty {s['n_dirty']} / unknown {s['n_unknown']})   "
          f"projection points: {s['n_projection_pts']}")
    print(f"stale-projection drift: mean abs {s['mean_abs_drift_pp']} pp   "
          f"max abs {s['max_abs_drift_pp']} pp   "
          f"within 2pp: {s['within_2pp_frac']:.0%}")
    if s["clean_s_ratio_max"] is not None:
        print(f"worst s_ratio among CLEAN sessions (the fair weight test): "
              f"{s['clean_s_ratio_max']}")
    print("\n--- per session: SessionFactor consistency (s_ratio ~1.0 = weights fit) ---")
    print(f"{'session_start':25} {'pure':>7} {'grabs':>5} {'s_ratio':>8} {'maxdrift':>8}  models")
    for row in sorted(result["sessions"], key=lambda r: r["s_ratio"], reverse=True):
        # Only flag CLEAN sessions: a high s_ratio on a dirty/unknown session is
        # expected off-laptop contamination, not a weight problem.
        flag = "  <-- CLEAN misfit" if (row["s_ratio"] > 2.0 and row["purity"] == "clean") else ""
        mtag = ("MIX:" if row["mixed"] else "") + ",".join(row["models"])
        print(f"{row['session_start']:25} {row['purity']:>7} {row['n_grabs']:>5} "
              f"{row['s_ratio']:>8.3f} {row['max_abs_drift']:>8}  {mtag}{flag}")
    print("\nNote: a high s_ratio is only a weight signal on a CLEAN session. On "
          "DIRTY/UNKNOWN sessions it's (likely) off-laptop use the meter saw but "
          "our transcripts didn't. In a MIXED clean session it points at a "
          "cross-model weight being off.")


if __name__ == "__main__":
    main()
