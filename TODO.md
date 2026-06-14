# TODO / feature ideas

## Done 2026-06-14
- [x] Sonnet w_cw1h re-burn: it's a BRIDGE (cw1h_sonnet:cw1h_Om is confounded
      with SessionFactor in a single-model run — see memory). Driver
      `analysis/burn/burn_sonnet_bridge.py` (Opus gauge leg + Sonnet leg, same
      session, send_model VERIFY gate) + dry-run test `test_burn_sonnet_bridge.py`
      (10 cases, all the orchestration + abort paths). Both in gitignored
      analysis/burn/. Run the real burn: `python burn_sonnet_bridge.py --go`
      from a PLAIN console, then solve_cw1h.py.
- [x] README cleanup: removed shipped features from "coming" (per-model
      weighting + early-session calib), fixed live-estimate desc (was claiming
      raw unweighted counting), test count 173->200+.
- [x] Stale-convention sweep (agent): one live issue found + fixed (commit
      29366df — widget_updater weight-basis header still described the old
      input=1 / output 5.4x basis). Everything else correctly marked superseded.
- [x] round-not-floor presumption (API_PCT_FLOOR_BIAS_PP 0.5->0.0) + full
      sub-floor uncertainty rederivation; backtest_fit.py built; gridlines.

## Still open
- Run the actual Sonnet bridge burn (the driver is ready + test-validated).
- A single clean tiny-dose run from pct 0 would CONFIRM round-vs-floor
  (currently a presumption from 2 thin runs, 0.4-0.5pp flip).
- Dashboard settings UI wiring for --hgrid/--vgrid (small follow-up; chart
  already defaults them on).

## Usage graph
- **Configurable gridlines.** Add a toggle/setting for how many horizontal and
  vertical gridlines the usage graph draws. Right now you have to squint to
  estimate percentages off the curve — gridlines (e.g. every 10/20/25%) would
  make it readable at a glance. Ideally separate counts for horizontal (% axis)
  and vertical (time axis), exposed in the dashboard settings.
