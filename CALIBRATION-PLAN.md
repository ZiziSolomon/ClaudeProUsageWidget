# Improved early-session calibration — design for 0.1.1

Target: **0.1.1** (after the 0.1.0 release). Active plan, not speculative.

This replaces the fragile per-poll budget back-derivation that causes the
session-% estimate to be unstable early in a window and to overshoot 100%.

---

## Background: the bug we're fixing

The displayed session % is `100 * io_total / implied_session_budget`,
**unclamped**. The budget is back-derived from a live API reading as
`budget = io_total / (pct/100)`. Anything that makes the budget too small
overshoots 100%.

### The three drivers of the >100% overshoot

1. **Integer-rounding / small-sample instability (dominant early).** The live
   endpoint reports utilization at **integer (1%) resolution** — verified
   empirically, 60/60 distinct values whole, and the convention is **floor**
   (truncation): `pct = floor(100*io/B)` matched 83% of samples vs 68% for
   round, with the residual attributable to budget-estimate noise. At `pct=1`
   the true value sits in a ±0.5pp band = ±50% relative error; back-deriving a
   budget by dividing by that coarse number is wildly unstable. Observed thrash
   from real data, three calibrations 34s apart: budget 200,800 → 123,550 →
   141,950 purely from `pct` ticking 1→2.

2. **Session rollover (FIXED in 0.1.0, commit 49805ae).** `implied_session_budget`
   used to persist across a session boundary, so an old (possibly small) budget
   got divided into the new window's tokens. `_roll_over_if_expired` now resets
   the window the instant `session_end` passes.

3. **Off-laptop usage contamination (NEW finding, 2026-05-25).** The API `pct`
   reflects **all account usage** (claude.ai web, other machines, every Claude
   Code project), but `transcript_io_total` counts only the transcripts this
   widget watches. So `budget = local_io/(pct/100)` is biased **down** by
   however much usage happened off-widget — and that varies per session.
   Decisive evidence, one real session:

   ```
   pct= 5.0  io=1,938  budget=38,760
   pct=10.0  io=2,346  budget=23,460
   pct=10.0  io=2,753  budget=27,530
   ```

   `pct` doubled 5→10% while local io rose only ~400 tokens — impossible from
   local tokens alone. A contaminated small budget (23k) gets locked in, local
   tokens keep accruing, estimate sails past 100%.

### Empirical findings from `%LOCALAPPDATA%\ClaudeUsage\usage_data\calibration.jsonl`

- **Aggregate budget is NOT stable**: across 56 sessions reaching ≥10%, median
  ~186k, range 4,783–242,775, CV 34%, only 39% within ±15% of median.
- **NOT explained by model mix**: Pearson r(opus_fraction, budget) = −0.05.
  Same 100%-Opus mix produced budgets of 21k, 67k, 214k, 233k. (Killed the
  "Opus tokens weighted heavier" hypothesis.)
- **Real per-session variation exists** beyond contamination: there was a period
  where peak sessions ran genuinely lower token budgets — likely dynamic
  capacity tightening under load. So budget can differ session-to-session and
  possibly **shift mid-session**.
- **io step between observations**: median 0.25pp of budget, p90 1.74pp, max
  8.86pp — a single step can blow past a 1% crossing, so lazy (on-file-event)
  polling has a dangerous fat tail.

---

## Design

### 1. Seed with a recency-weighted prior
Session budgets cluster (~186k clean) but drift across regimes, so seed each new
session's budget from a **recency-weighted estimate of recent clean sessions**
(EMA or last-similar-session), NOT an all-time median (which lags regime shifts).
The prior only seeds — it never overrides in-session evidence. This alone removes
most of Driver 1 and the `n/0` case: we never divide by a coarse low `pct`.

### 2. Delta-calibration (calibrate on increments, not absolutes)
On **every** poll, compare the increment since the previous poll:

```
Δpct_expected = 100 * Δio_local / B_est
residual r     = Δpct_api - Δpct_expected
```

Clean intervals recalibrate via `B = 100 * Δio_local / Δpct_api`. This beats
absolute `io/(pct/100)` because it never assumes the session started at the
origin and is immune to any *constant* baseline of unseen usage — only fresh
contamination shows up. **Prefer the widest clean span available**: a clean
`Δpct=20` carries ±2.5% quantization error vs ±25% at `Δpct=2`. Accumulate
clean `Δio` rather than recalibrating off every 1pp tick (that tick noise is
exactly what caused the 200k→123k thrash).

### 3. Asymmetric blip classifier (the key insight)
Off-laptop usage can only **add** to account pct, never subtract:
`exogenous_pct ≥ 0`, always. So:

```
r = 100 * Δio_local * (1/B_true - 1/B_est) + exogenous_pct      (exogenous ≥ 0)
```

- **r < −tol** → exogenous can't be negative, so the only cause is
  `B_true > B_est`: **budget too small. Grow it, with confidence.** This is
  exactly the overshoot condition (small budget → inflated pct → expected > api
  → r negative), so **Bug A becomes self-healing on the next clean poll.**
- **r > +tol** → ambiguous: off-laptop usage OR budget too big. Resolve over
  time — contamination is **sporadic** (spike, then local-only intervals return
  to r≈0), budget-too-big is **persistent** (holds across many active
  intervals). Transient positive r → treat as contamination, **adopt the new
  pct for display but do NOT shrink the budget.** Persistent positive r →
  slowly shrink.

`tol` ≈ the quantization band, `~max(1.5pp, ...)`, since `Δpct_api` is integer.

The risk posture is deliberately asymmetric: fast/confident correcting the
unsafe direction (too-small → overshoot), slow/cautious correcting the safe one
(too-big → harmless under-estimate, never alarms).

### 4. Smooth, don't replace
Update `B` as a robust/EMA blend of recent clean delta-calibrations so one odd
interval nudges rather than overwrites. Prefer recent clean intervals over a
session-wide fit so it can track a budget that moves mid-session.

### 5. Clamp display to 100
Belt-and-braces for any residual.

### 6. Value-driven (front-loaded) poll schedule
Calls aren't equally valuable across the 300-min window. Two value curves:
- **Calibration value is front-loaded** — a wrong budget compounds over the
  whole remaining window; with the prior seed, early polls *verify* the prior
  and catch contamination before it compounds.
- **Decision value is back-loaded** — accuracy matters most near the cap
  ("am I about to be cut off at 90%?").

So the ideal is a **U-shape / event-driven** schedule, not monotonic decay:
- First ~30 min: dense (~5 min, or eager at predicted pct crossings) to verify
  the prior and reject early contamination.
- Stable middle: slow heartbeat (~30–40 min) to catch disconnect/drift.
- Approaching cap (est > ~70–80%): re-densify (~10 min) for decision-grade
  accuracy.

Roughly the same total call count as today's flat 20-min (~15 polls),
redistributed — also respects the fragile Firefox-only auth (no extra hammering).
Cleaner implementation: **event-triggered, not clock-triggered** — poll on
(a) predicted crossings while budget is unverified, (b) crossing user-actionable
thresholds (80/90/95%), (c) a slow background heartbeat. Time density falls out
naturally.

**Caveat:** front-loading assumes the prior is usually right. If a user's budget
genuinely shifts session-to-session, keep polling dense until early polls
*agree* with the prior, rather than relaxing after a fixed time.

---

## Honest ceiling
No scheme recovers usage the widget can't see. Between polls a contaminated
session will under-count locally; the prior + contamination rejection keep that
from poisoning the budget, and each poll's authoritative `pct` re-anchors the
display. We lean on the API `pct` as truth and use local tokens to interpolate.

---

## Validation tasks (do before/while implementing)
1. Confirm the chain **negative residual ⟺ budget-too-small ⟺ historical >100%
   episode** against `calibration.jsonl` + `discrepancies.jsonl`: were the
   recorded overshoots preceded by negative residuals? If yes, the classifier is
   validated end-to-end.
2. Confirm delta-calibration would have been more stable than absolute on the
   historical thrash sessions (replay the 200k→123k→141k window).
3. Re-confirm the floor convention on a larger sample with a better-fit budget
   (current 83% vs 68% used a single-sample budget estimate).
4. Characterise the recency structure of the budget regime shifts (peak vs
   off-peak) to choose the prior's EMA half-life.

---

## What 0.1.0 ships instead (the simple stopgap)
- Session-rollover fix (Driver 2) — **done**, commit 49805ae, 28 tests green.
- pct-floor + clamp: don't back-derive a budget below a pct floor (≥5%); show
  pending `--` until then; clamp the displayed estimate to 100. Small, removes
  the `n/0` crash and the worst of the low-pct overshoot without the full
  delta-calibration machinery.
