# Turn-state read gating — implementation plan

Written 2026-07-18 late. Context: the sawtooth is a proven anchor mis-pairing bug
(memory: sawtooth-pairing-derivation; validated by analysis/gating_replay.py,
commit b699210). Fix = when an endpoint read lands mid-turn, defer pairing its
level with local io until the turn settles. Never touch the level itself.

Agreed layering (2026-07-18 discussion):
1. **Hooks** as the primary busy signal (harness declares turn state — no inference).
2. **Transcript watch** as the dangler-resolver (catch turn boundaries hooks missed).
3. **Title watch** stays a research oracle only; we suspect it adds nothing as a
   production layer — test that claim in Phase 3 rather than assume it.

---

## Phase 0 — Explain hooks (read this over coffee)

### What a hook is

Claude Code can run **your** shell command at fixed points in its lifecycle. You
already own one: every prompt you submit runs
`~/.claude/scripts/session-title-hook.ps1`, which is why your terminal tabs get
titles like `✳ !CUW-Om - sawtooth gating proof`. That's a `UserPromptSubmit`
hook. So the mechanism is already load-bearing on your machine; we're adding a
second script on two more events.

### How they're configured

In `~/.claude/settings.json` (user-global; project `.claude/settings.json` also
works), under a `"hooks"` key. Shape:

```json
{
  "hooks": {
    "UserPromptSubmit": [
      { "hooks": [ { "type": "command",
                     "command": "powershell -File C:\\...\\turn-state-hook.ps1",
                     "timeout": 10 } ] }
    ],
    "Stop": [
      { "hooks": [ { "type": "command", "command": "..." } ] }
    ]
  }
}
```

(Tool-related events also take a `"matcher"` to filter which tool; the events we
use fire unconditionally.) We'll wire this with the update-config skill, not by
hand-editing.

### The lifecycle events that matter to us

| Event | Fires when | We use it for |
|---|---|---|
| `UserPromptSubmit` | you submit a prompt, before Claude processes it | **turn start** → write `busy` |
| `Stop` | the main agent finishes responding | **turn end** → write `idle`. Crucially it does NOT fire between tool calls inside an agentic turn — one turn, one Stop. |
| `SubagentStop` | a spawned subagent finishes | ignore for v1 (subagent work happens inside a main turn that's already busy) |
| `SessionStart` | session starts/resumes | mark session alive; clear any stale busy for that session id |
| `SessionEnd` | session exits (`/clear`, logout, quit) | write `idle` — cleans up sessions that die politely |

### How the hook learns context

Claude Code passes a JSON object on **stdin**: `session_id`, `transcript_path`,
`cwd`, `hook_event_name`, plus event-specific fields. `transcript_path` is a
gift — the ledger can record which transcript file each session maps to, so the
widget doesn't have to guess.

### Rules of engagement

- Exit code 0 = fine. **Exit code 2 is special** — on `Stop` it *blocks Claude
  from stopping* (forces it to continue!), on `UserPromptSubmit` it blocks the
  prompt. Our scripts must swallow every error and always exit 0: a broken
  logging hook must never be able to break Claude itself.
- Stdout from a `UserPromptSubmit` hook gets injected into Claude's context.
  Ours prints nothing.
- Default timeout 60 s per command; we'll set ~10 s. PowerShell cold-start is
  ~200-500 ms per invocation — same cost the title hook already pays per prompt.

### ⚠ Verify in the morning (things the plan asserts but should be checked)

- [ ] **`Stop` does not fire on Esc-interrupt** (docs have said this; confirm
      empirically: temp hook, interrupt a turn, check ledger). This is the main
      dangler source and the reason layer 2 exists.
- [ ] Exact stdin field names per event (dump raw stdin to a scratch file once).
- [ ] Whether settings hook changes are picked up by already-running sessions or
      only new ones (affects rollout: may need to restart sessions).
- [ ] `SessionEnd` fires on window-close / process-kill? (Suspect no → dangler.)
- Fast route for all of these: ask the claude-code-guide agent + one empirical
  session with a debug hook. Budget ~30 min.

---

## Phase 1 — Hook scripts + ledger

**New file** `~/.claude/scripts/turn-state-hook.ps1` (one script, branches on
`hook_event_name`):

- Read stdin JSON. Append one line to the ledger:
  `{"ts": "<iso local+offset>", "sid": "...", "ev": "start|stop|session_start|session_end", "transcript": "..."}`
- Ledger: `%LOCALAPPDATA%\ClaudeUsage\turn_state.jsonl` (runtime state lives
  there, never in the repo — memory: runtime-state-location).
- Append with a 3-try retry on IO errors (two sessions can fire concurrently;
  sub-1KB appends are effectively atomic on NTFS, retry covers the rest).
- Entire script in try/catch, always `exit 0`.
- Wire `UserPromptSubmit` + `Stop` + `SessionStart` + `SessionEnd` in
  `~/.claude/settings.json` via the **update-config skill**.

**Smoke test:** two parallel sessions, interleaved prompts; ledger shows paired
start/stop per sid; Esc-interrupt one turn and observe the dangler (Phase 0
verification doubles as this).

## Phase 2 — Widget: TurnStateTracker + SHADOW mode

New class in `widget_updater.py` (+ fakes for tests, per test-architecture):

- Incremental tail-read of the ledger (same seek-to-offset pattern as the
  transcript reader). State: `sid -> busy_since` for sessions whose last event
  is `start`.
- `busy_now() -> (bool, run_seconds)`; any session busy ⇒ busy.
- **Dangler resolution** (the layer-2 transcript watch — design decision, keep
  this order):
  1. A later hook event for the sid (next `start`, or `stop`) supersedes — hooks
     self-heal at the next turn boundary.
  2. Transcript check: if the sid's transcript (we know its path from the
     ledger) shows a *completed* end-of-turn assistant row newer than
     `busy_since` — or a new **genuine user text row** (filter tool_results per
     memory: transcript-user-vs-toolresult) — the dangling turn is over; clear
     or restart the state accordingly.
  3. Staleness cap: busy > CAP (default 30 min) **and** that transcript hasn't
     grown for QUIET_S (default 120 s) ⇒ treat settled. Both conditions, so
     genuinely long agentic turns (which do write rows as they go) stay busy.
  All three fail **conservative** (worst case: we defer a pairing needlessly).
- **Shadow behaviour** (this phase changes no estimates): stamp every
  calibration.jsonl read row with `busy_at_read`, `busy_run_s`,
  `busy_frac_since_prev`. That's ground-truth labels for the analysis the title
  probe could only approximate (its active-tab blind spot poisoned idle labels).
- Config: `turn_gating: "off" | "shadow" | "on"`, default `"shadow"`.
- Tests: fake ledger + fake clock; dangler cases (missed Stop then new prompt;
  missed Stop then silence past cap; long agentic turn stays busy); concurrent
  sessions; ledger absent (feature inert).

**Then let it run for several days of normal use before Phase 3.**

## Phase 3 — Analyze shadow data; judge the title watcher

- Extend gating_replay.py to score reads against ledger-derived busy state
  (replacing spinner labels). Re-run the prediction check from the derivation:
  prev-busy ⇒ e>0, now-busy ⇒ e<0, settled-settled ⇒ e≈0 — now with labels that
  can actually falsify it. Decide gating parameters (grace period, cap) from
  this data, not from taste.
- Run title_probe.py alongside for one day; compare oracle vs ledger. **Claim
  under test: titles add nothing once hooks + transcript watch exist** (our
  suspicion is they don't; let the disagreement rows decide).

## Phase 4 — Enforce

Only after Phase 3 confirms with clean labels:

- In `_adopt_api_pct` / `_set_anchor`: if busy at read time, adopt the level for
  display (endpoint stays ground truth — memory: endpoint-is-trusted-anchor)
  but mark the anchor **provisional**: no sf refit, no frozen (pct, io) pair.
- On settle (tracker idle + grace ≈ 2 poll cycles for ingest catch-up): trigger
  one fresh endpoint read and anchor fully on it. Rate-limit: max one
  settle-read per turn (don't hammer the endpoint).
- Fallback if the settle-read fails (auth, offline): promote the provisional
  anchor paired with io-at-settle. That pairing errs low (io grew past what the
  level covered) — the conservative direction — and the next normal read fixes it.
- Tests around the provisional-anchor lifecycle; then flip default shadow→on.

---

## Open questions for the morning

1. Phase 0 checklist above (Stop-on-interrupt is the big one).
2. Ledger rotation: widget compacts turn_state.jsonl past ~1 MB (it grows ~2
   lines/turn, so this is years away — decide whether to bother in v1).
3. Should `SubagentStop`/background tasks ever matter? (v1 says no — they live
   inside a busy main turn. Revisit if shadow data shows busy gaps during
   agentic work.)
4. Does shadow mode ship in v0.2.0-beta or after? (Release is still one
   repackage+tag away and shouldn't wait on this — memory: release-v020-beta.)
