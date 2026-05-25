# Handoff — resume here (written 2026-05-25)

## Start-of-session prompt
> Resume ClaudeUsageWidget. Read HANDOFF.md. This session finished the 0.1.0
> session-% accuracy work (all unit-tested, 46 green) but it is NOT live-
> verified. First task: live-verify the new recalibration control flow against
> the real API, then do the 0.1.0 release/packaging. Check usage with
> `python usage_check.py --live` and pace accordingly.

## What this session did (all on `main`, committed)
Fixed the session-% accuracy bugs. The >100% overshoot had **three drivers**
(see `CALIBRATION-PLAN.md` and the `accuracy-bugs` memory):

1. **Integer-rounding instability** — API reports utilisation as an integer %
   (floor convention, confirmed 83% vs 68% on real data). Back-deriving
   `budget = io/(pct/100)` at pct=1–2 swings ±25–50%.
2. **Session rollover** — old window's budget divided into new window's tokens.
3. **Off-laptop usage contamination** — API pct is account-wide, local token
   tally is local-only, so the back-derived budget is biased down per session.

Commit trail (newest first):
- `dc820c8` stuck-watcher: 1-min silence window + 5s deferred re-check
- `8e8a352` README: per-model weighting added to Coming features
- `9bc63ab` stuck-watcher detector via disk re-scan on recalibration
- `36f3c84` recalibrate budget on >5pp API discrepancy + suppress false stuck toast
- `7184fef` emergency re-anchor gap → 5pp (was 3, was 25)
- `18c8e98` self-heal: force recal when estimate is suspect (clamp/gap), cooldown-gated
- `523ffba` pct-floor on calibration + clamp display (closes n/0)
- `7881c9a` CALIBRATION-PLAN.md (the 0.1.1 design) + README roadmap
- `49805ae` session rollover fix (`_roll_over_if_expired`)

## How the accuracy logic now works (control flow in widget_updater.py)
- **Display = local extrapolation between API calls.** `_estimate_session_pct`
  = `100*io/budget`, **clamped to 100**. Budget = last back-derived
  `implied_session_budget`. No budget yet ⇒ shows pending `--`.
- **Calibration floor.** `_append_calibration` only sets a budget at
  `pct >= CALIBRATION_PCT_FLOOR (5)`; below that it's too rounding-unstable.
  It gained `update_budget` so a sample can be logged for history without
  churning the budget.
- **Adopt + maybe-recalibrate.** `_adopt_api_pct` (used by calibration +
  liveness; `force_refresh` inlines the same gate because its session-reset
  must run first) always adopts the fresh API pct for display, but only
  re-derives the budget when it disagrees with the prior display by
  `> RECAL_DISCREPANCY_PP (5)` (or no budget yet). Gating on >5pp avoids the
  rounding thrash (the historical 200k→123k→141k swing).
- **Self-heal trigger.** In `on_modified`, after the local estimate updates,
  `_estimate_is_suspect` fires a forced API re-anchor when the estimate pegs at
  the 100% clamp OR runs `FORCE_RECAL_GAP_PP (5)` past the last API-confirmed
  pct. Cooldown-gated by `FORCE_RECAL_COOLDOWN_SECS (300)` so a stuck-at-100
  session can't hammer the endpoint. (`FORCE_RECAL_GAP_PP == RECAL_DISCREPANCY_PP`
  by design — same "disagree enough to act" threshold.)
- **Stuck-watcher detection.** On every recalibration, `_rescan_and_check_watcher`
  does an active `full_scan` of the transcript folder. `seen_ids` dedup means it
  recovers exactly the tokens the watchdog Observer missed → heals the count and
  derives the budget against truth. If it recovers tokens AND live events have
  been silent for `WATCHER_STUCK_SILENCE_SECS (60)`, it arms a
  `WATCHER_STUCK_RECHECK_SECS (5)` one-shot timer and only toasts "restart" if no
  ping lands in that grace window (`last_event_at` unchanged). **Off-laptop usage
  can never trip this** (no local-disk tokens to recover) — that's the whole
  point of the disk-rescan approach.
- **Old pct-based "stuck" toast is dormant:** when we recalibrate off a
  discrepancy we pass `suppress_toast=True`, so the `LARGE_DISCREPANCY_PP (10)`
  toast no longer false-fires. Stuck detection now comes from the disk re-scan.

## Tunable constants (top of widget_updater.py)
`CALIBRATION_PCT_FLOOR=5`, `RECAL_DISCREPANCY_PP=5`, `FORCE_RECAL_GAP_PP=5`,
`FORCE_RECAL_COOLDOWN_SECS=300`, `WATCHER_STUCK_SILENCE_SECS=60`,
`WATCHER_STUCK_RECHECK_SECS=5`, `LIVENESS_INTERVAL_SECS=1200`.

## NEXT: live verification (required before declaring 0.1.0 done)
Tests cover the logic but NOTHING here has run against the real API in the live
widget. Things to actually watch:
1. A fresh session: confirm it shows `--` below 5%, then a sane number, and
   never displays >100%.
2. Force a >5pp disagreement (or wait for natural drift) and confirm the budget
   re-derives (watch the `calibration: …` log line) — and that it does NOT
   re-derive on small (<5pp) moves.
3. The forced re-anchor: confirm extra API calls are bounded by the 5-min
   cooldown and don't spam discrepancy toasts.
4. Stuck detector: hard to provoke deliberately; mainly confirm it does NOT
   false-fire during normal use or when you use claude.ai web on the side.
Run the widget with `pythonw tray_widget.py` (or the built exe). `usage_check.py
--live` is the authoritative cross-check.

## THEN: 0.1.0 release
Windows-only first (resist scope creep — see `shipping-plan-v010` memory).
Build exe + zip + GitHub Release. `build.ps1` is the safe rebuild (won't drop
config.json or fight the file lock). Branch discipline starts at 0.1.1 (user's
call this session): commit straight to main for 0.1.0, branch after.

## 0.1.1 and beyond (deferred, designed)
- **0.1.1 — improved early-session calibration:** recency-weighted prior +
  delta-calibration on increments + asymmetric blip classifier (off-laptop usage
  is always ≥0, so a negative delta-residual unambiguously = budget-too-small =
  self-healing). Front-loaded poll schedule. Full design + validation tasks in
  `CALIBRATION-PLAN.md`. Today's clamp/floor/re-anchor are the stopgap for this.
- **Per-model weighting** (new roadmap item): usage is currently raw unweighted
  input+output; weighting Opus heavier is planned. (NB: model mix did NOT
  correlate with budget on our data, r=−0.05 — so this is correctness, not the
  fix for the budget variance, which is off-laptop usage.)
- `v020-configurable-widgets` memory: separate deferred spec (settings GUI + SVG).

## Gotchas (still current)
- **Live API auth works via FIREFOX cookies only** — Chrome/Edge fail (memory
  `cookie-browser-support`). Verify in Firefox.
- Runtime state lives in `%LOCALAPPDATA%\ClaudeUsage`, NOT the repo `usage_data/`
  (memory `runtime-state-location`). Calibration history: `…\usage_data\
  calibration.jsonl`, discrepancies: `discrepancies.jsonl`.
- Edit the **root** `config.json`, never the `dist` copy (memory
  `config-json-rebuild-hazard`). `config.json` is gitignored (holds org_id);
  fresh checkouts need `$env:CLAUDE_ORG_ID` to import widget_updater.
- PowerShell: commit with `git commit -F <file>`, not `@'…'@` here-strings
  (leaks a stray `@`). Tests in this session used a temp file via Bash heredoc.
- Tests: `python -m pytest tests/test_widget_updater.py -q` (46 green). Note
  `TestCalibrationRecordsBudget` calls the real `_append_calibration`, which
  writes to the real calibration.jsonl — minor pre-existing test pollution.
- Untracked in the tree (intentionally not committed): build/, dist/, *.bak,
  *.log, .claude/, DECISION.md, alert_preview.png, install_start_menu.ps1.
