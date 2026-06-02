"""Per-token-type lower-bound analysis.

Reads the two CSVs produced by build_token_csvs.py.

Step 1 - collapse session-window jitter: cluster endpoint checks whose
session_start values fall within 5 minutes of each other into one session group
(real sessions are hours apart, so single-linkage at 5 min is safe).

Step 2 - lower bound per token type. Within a group, pct is monotonic, so for
any pair of checks i<j we have a clean delta. The same logic the widget already
uses for the budget lower bound,

        budget_lb = 100 * delta_tokens / (delta_pct + 1)

generalises per token type. For a single type t:

    delta_pct_from_t = f_t * delta_pct,   0 <= f_t <= 1   (t is one of several
                                                            contributors)
    delta_t tokens caused delta_pct_from_t = 100 * delta_t / budget_t
    => budget_t = 100*delta_t / delta_pct_from_t  >=  100*delta_t / delta_pct

so 100*delta_t/(delta_pct) is a LOWER bound on budget_t (the number of type-t
tokens that would fill a whole session if t alone drove usage). The "+1" guards
floor-rounding of pct (true delta_pct <= observed+1), making the bound
conservative. max() over every in-group pair keeps the tightest evidence.

Crucially this is robust to under-counting: missing some type-t tokens, or
off-machine usage inflating delta_pct, only shrinks the bound - it stays valid.

budget_t (tokens per 100%) inverts to a per-token cost a_t = 100/budget_t. A
LOWER bound on budget_t is an UPPER bound on a_t. Normalising a_t to input gives
an upper bound on each type's weight relative to input - directly testable
against the published ratios (input 1, cc5m 1.25, cc1h 2, cr 0.1, out 5).
"""
import csv
import os
from datetime import datetime, timedelta

HERE   = os.path.dirname(os.path.abspath(__file__))
TX_CSV = os.path.join(HERE, "transcript_entries.csv")
EP_CSV = os.path.join(HERE, "endpoint_checks.csv")

TYPES = ["input", "cache_write_5m", "cache_write_1h", "cache_read", "output"]
PUBLISHED = {"input": 1.0, "cache_write_5m": 1.25, "cache_write_1h": 2.0,
             "cache_read": 0.1, "output": 5.0}

CLUSTER_GAP = timedelta(minutes=5)


def _dt(s):
    return datetime.fromisoformat(s) if s else None


def load():
    tx = []
    for r in csv.DictReader(open(TX_CSV, encoding="utf-8")):
        tx.append((_dt(r["timestamp"]),
                   {t: int(r[t]) for t in TYPES}))
    tx.sort(key=lambda x: x[0])
    ep = []
    for r in csv.DictReader(open(EP_CSV, encoding="utf-8")):
        ss = _dt(r["session_start"])
        ct = _dt(r["check_time"])
        pct = r["session_pct"]
        if ss is None or ct is None or not pct:
            continue
        ep.append({"ss": ss, "ct": ct, "pct": float(pct)})
    ep.sort(key=lambda x: x["ss"])
    return tx, ep


def cluster_groups(ep):
    """Group checks whose session_start is within 5 min of the running group
    anchor. Returns list of groups (each a list of check dicts)."""
    groups = []
    cur, anchor = [], None
    for e in ep:
        if anchor is None or e["ss"] - anchor <= CLUSTER_GAP:
            if anchor is None:
                anchor = e["ss"]
            cur.append(e)
        else:
            groups.append(cur)
            cur, anchor = [e], e["ss"]
    if cur:
        groups.append(cur)
    return groups


def delta_tokens(tx, t0, t1):
    """Account-wide sum of each token type for entries in (t0, t1]."""
    acc = {t: 0 for t in TYPES}
    for ts, d in tx:
        if t0 < ts <= t1:
            for t in TYPES:
                acc[t] += d[t]
    return acc


def main():
    tx, ep = load()
    groups = cluster_groups(ep)
    print(f"endpoint checks: {len(ep)}   collapsed session groups: {len(groups)}")

    # budget_t lower bound = max over all in-group check pairs.
    best = {t: 0.0 for t in TYPES}
    best_ev = {t: None for t in TYPES}     # provenance of the winning bound
    pairs_used = 0

    for g in groups:
        # de-dup checks at identical times; keep monotone-pct order
        g = sorted(g, key=lambda x: x["ct"])
        for i in range(len(g)):
            for j in range(i + 1, len(g)):
                dpct = g[j]["pct"] - g[i]["pct"]
                if dpct <= 0:
                    continue
                dt = delta_tokens(tx, g[i]["ct"], g[j]["ct"])
                pairs_used += 1
                for t in TYPES:
                    if dt[t] <= 0:
                        continue
                    lb = 100.0 * dt[t] / (dpct + 1.0)
                    if lb > best[t]:
                        best[t] = lb
                        best_ev[t] = (g[i]["ss"].strftime("%m-%d %H:%M"),
                                      dt[t], dpct)

    print(f"in-group check pairs evaluated: {pairs_used}\n")
    # a_t = per-token cost in %/token. LOWER bound on budget_t => UPPER bound
    # (ceiling) on a_t. These ceilings are valid ABSOLUTE per-token bounds and
    # are comparable to each other AS CEILINGS (not as point weights).
    a = {t: (100.0 / best[t] if best[t] else float('inf')) for t in TYPES}
    print(f"{'token type':16} {'budget_t LB':>14} {'cost CEILING %/tok':>20} "
          f"{'ceiling rel. output':>20}")
    print("-" * 74)
    a_ref = a["output"]   # output is the well-populated, tight reference
    for t in TYPES:
        rel = a[t] / a_ref if a_ref else float('nan')
        print(f"{t:16} {best[t]:>14,.0f} {a[t]:>20.3e} {rel:>20.2f}")

    print("\nbound provenance (session / delta_tokens / delta_pct):")
    for t in TYPES:
        print(f"  {t:16} {best_ev[t]}")

    print("""
Reading:
 * budget_t LB  = at least this many type-t tokens fit in a 100% session if t
                  alone drove usage. Robust to under-counting (only loosens it).
 * cost CEILING = the most a single token of that type can cost (%/token); the
                  true cost is at or below this. A tight (low) ceiling that came
                  from a high-volume interval is strong evidence the type is cheap.
 * These are ONE-SIDED, per-type bounds from different intervals, so their RATIO
   is not a valid weight ratio - it cannot pin relative weights, only cap them.
   Pinning weights needs the complementary upper bound per type (two-sided) or
   an NNLS fit. 'input' here is loose: no interval was ever input-dominated.""")


if __name__ == "__main__":
    main()
