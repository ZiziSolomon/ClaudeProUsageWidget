# TODO / feature ideas

## Today (2026-06-14)

- **Dry-run test for the Sonnet w_cw1h re-burn.** Write a test that walks the
  full burn *sequence* for measuring `w_cw1h_sonnet` (the 06-09 value was VOID —
  silent `/model` failure meant those legs ran on Opus) **without executing any
  actual burn step** (no sends, no real token spend). Goal: validate the
  orchestration/sequencing — model verification per leg, phase ordering,
  reset-guards, output-file wiring — so the real burn can be set off confidently
  before going AFK. Stub/fake the send + transcript-read layer; assert the leg
  plan, per-leg model assertions, and artifact paths are correct.

- **README cleanup.** README still lists "improved early-session calibration"
  as a *coming* feature — it's DONE (2026-05-31). Remove it from the
  coming-features section. (Per memory `improved-early-session-calibration.md`.)

- **Stale-convention sweep (agent).** Sweep code AND memory for stale calibration
  conventions vs. the current one. Current: `B := 1` + per-session SessionFactor;
  per-model weights `w_Mt` on the `cw1h_Om = 1` basis; cost = `count × w`
  (multiply, NOT divide). Stale to flag: `input = 1` basis, `cw1h := 1` with
  `o = 9` weight dicts, absolute-B orun fitter, `solve_cw1h` hardcoded 6.34,
  any `count / w` cost direction.

## Usage graph
- **Configurable gridlines.** Add a toggle/setting for how many horizontal and
  vertical gridlines the usage graph draws. Right now you have to squint to
  estimate percentages off the curve — gridlines (e.g. every 10/20/25%) would
  make it readable at a glance. Ideally separate counts for horizontal (% axis)
  and vertical (time axis), exposed in the dashboard settings.
