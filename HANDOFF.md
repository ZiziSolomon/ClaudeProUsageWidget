# Handoff — resume here (written 2026-05-24, ~14:35 BST)

## Start-of-session prompt
> Resume the ClaudeUsageWidget "usable by strangers" work. Read HANDOFF.md.
> Three feature lanes were built by parallel agents but on a STALE git base
> (see "CRITICAL" below) — they need rebasing onto current `main`, not
> fast-merging. First: fix the base mismatch, then rebase/merge each branch
> resolving conflicts in tray_widget.py and widget_updater.py, test, commit.
> Then do A1 (build exe + zip + GitHub Release). Check usage with
> `python usage_check.py --live` and pace accordingly.

## Why we paused
Session usage hit **77%** (user's ceiling was 75%). Wrapped up rather than
pushing further. Session resets ~16:50 BST; weekly was 60%. Next session has
fresh budget.

## CRITICAL — worktree stale-base bug (fix this FIRST)
- The `isolation: worktree` agents branched from **`origin/main` = bf972a6**,
  but **local `main` = 2c5e75f**, which is 3 commits ahead:
  464f6b9, 7a08ced, and 2c5e75f (usage_check.py).
- `7a08ced` ("Fix ghost fill scaling and move runtime state out of the
  bundle") modified **tray_widget.py AND widget_updater.py** — the files the
  lanes edit. So lane branches will conflict and a plain `git merge` would
  also try to drop files added after bf972a6.
- **Fix going forward:** push local `main` to `origin` so future worktrees
  branch from the right tip — BUT this repo's history may go public, so ASK
  the user before pushing (and confirm the noreply identity on all commits).
- **To integrate the existing branches:** `git rebase --onto main bf972a6
  <branch>` (or cherry-pick the single feature commit) and resolve conflicts
  in the two shared files. Re-test after each.

## Branches produced (all based on bf972a6 — need rebase)
- **lane-b-tray** (`5a8a462`) — DONE. tray_widget.py: time-left number now
  bold, rounds to nearest hour, shows plain minutes under 1h; "Start at login"
  checkable menu item (shortcut in shell:startup); disconnected/error icon +
  tooltip driven by a `status` field. Sample renders in
  %TEMP%\claude_icon_test\.
- **lane-a-auth** (`c2e0c78`) — DONE, and the agent fast-forwarded `main` into
  its worktree first, so its merge-base is `7a08ced` (NOT the stale bf972a6).
  It touches only widget_updater.py / ClaudeUsage.spec / config.example.json,
  none of which main changed after 7a08ced → **merges CLEAN, no conflict**.
  MERGE THIS ONE FIRST. Delivered: cross-browser cookies auto-try Chrome→Edge→
  Firefox (C1), `_fetch_usage_status()` + `startup_sanity_check()` emitting the
  status contract + loud logs (C2a/G1), `LIVENESS_INTERVAL_SECS=600` heartbeat
  separate from calibration budget, lazy org_id/USAGE_URL resolution so import
  no longer hard-fails, `_discover_org_id()` via /api/organizations preferring
  Pro/Max (D1), tkinter/console first-run prompt (D2), config resolves from
  %LOCALAPPDATA%\ClaudeUsage\config.json first then repo (G2). Back-compat
  verified: usage_check.py + tray import names intact.
  NOTE: lane-a-auth's base predates main's usage_check.py + HANDOFF.md commits,
  but it doesn't touch them, so a 3-way merge keeps them (don't be alarmed by
  `git diff main lane-a-auth` showing them as "deleted").
- **docs-packaging** — DONE. Single root requirements.txt + requirements-dev.txt
  (B1), README rewrite (top warning, usage_check headline, "device"
  limitations, autostart/unhide tray, platform table), .github/workflows/ci.yml
  (win+mac+linux smoke + pytest), tests/ (22 passing).
  ⚠️ CONFLICT TO EXPECT: because it was on the stale base it RECREATED its own
  `usage_check.py` and forward-ported `session_history.py` — both already on
  main. Drop its versions and keep main's during rebase (or diff to confirm
  identical). Take only its README/requirements/CI/tests changes.

## The status-field contract (Lane A produces, Lane B consumes)
`status` in widget_state.json + on_state_change payload, values:
`"ok" | "no_cookie" | "no_login" | "fetch_error" | "config_missing"`.
Lane B reads it defensively (absent ⇒ "ok"). Verify Lane A emits exactly these.

## Done & committed on main (2c5e75f)
- `usage_check.py` — CLI: default = widget estimate, `--live` = authoritative
  claude.ai web check (== Claude Code session usage), `--json` for agents.
  **Highlight this in the README** (it was the original point of the project).
- onboarding.md updated (how Claude checks usage); memory files added
  (usage-check-tool, powershell-commit-message). These live in ~/.claude, not
  this repo.

## Cadence decision (for Lane A review)
Web check moved hourly → ~10 min, BUT as a separate **liveness heartbeat**, not
by lowering the per-session calibration budget (CALIBRATION_CALLS_PER_SESSION).
Confirm Lane A implemented it that way.

## Gotchas
- PowerShell: use `git commit -F <file>` for multi-line messages; `@'...'@`
  here-strings leak a stray `@` into the subject (bit us twice — see memory).
- Never commit `config.json` (gitignored, holds org_id). It is NOT present in
  fresh worktrees, so testing there needs `$env:CLAUDE_ORG_ID`.
- Cross-platform scope decided: core + CI only; tray/packaging stay Windows v1.
