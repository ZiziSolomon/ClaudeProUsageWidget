# Claude Usage Widget

**v0.2.0-beta** · Windows · Python · MIT

A lightweight tray widget for Claude Pro/Max users — uses live Claude Code transcripts to estimate your usage continuously between polls of claude.ai for calibration.

> **Unofficial — not affiliated with Anthropic.** Reads your usage by polling an undocumented claude.ai endpoint using your Firefox session cookie. See [Risks](#risks) before using.

---

## Why this one

- **Live between polls** — token counts from your local Claude Code transcripts drive a live estimate that updates as each response arrives, not just when the widget polls claude.ai.
- **CLI access** — `usage_check.py` gives you the current reading from any terminal or script, useful for Claude Code hooks and mid-task checks.
- **Customisable widgets** — per-widget colours, fill modes, colour thresholds, and custom shapes.
- **HTML dashboard** — a local browser UI for settings, icon customisation, and a usage graph.

---

## Screenshots

### System tray

![Up to five tray icons: session ghost, weekly ghost, session %, weekly %, session clock](docs/readmeImages/tray_icons.png)

Up to five icons, all configurable from the dashboard: session usage (ghost fill), weekly usage (ghost fill), session % (text), weekly % (text), and time left in session (arc). All update live.

<!-- TODO: add right-click menu screenshot -->

---

### HTML dashboard

Open by right-clicking any tray icon → **Open dashboard**, or navigate to `http://localhost:7433` while the widget is running.

![Dashboard — all sections collapsed](docs/readmeImages/dashboard_collapsed.png)

<details>
<summary>Show in taskbar</summary>

![Show in taskbar section](docs/readmeImages/dashboard_taskbar.png)

Toggle which of the five icons appear in your tray.
</details>

<details>
<summary>Settings</summary>

![Settings section](docs/readmeImages/dashboard_settings.png)

- **Start at login** — adds a Startup shortcut so the widget launches with Windows.
- **Start menu shortcut** — optional shortcut in the Windows Start menu.
- **Check usage every N min** — how often to poll claude.ai. Counts from the last poll against claude.ai, whatever triggered it.
- **Re-check at these %** — poll claude.ai the first time you cross each threshold in a session (e.g. `5,10,95`).
- **…and after every N% moved** — poll claude.ai whenever the live estimate moves this many points since the last poll against claude.ai, whatever triggered it.
</details>

<details>
<summary>Actions</summary>

![Actions section](docs/readmeImages/dashboard_actions.png)

- **Confirm usage %** — force an immediate poll of claude.ai and re-anchor the estimate.
- **Open config folder** — opens the folder containing `config.json` and the usage logs in Explorer.
- **Restart widget** — restart in place.
- **Quit** — exit cleanly.
- **Uninstall** — removes Startup and Start menu shortcuts, then quits.
</details>

<details>
<summary>Appearance</summary>

![Appearance section](docs/readmeImages/dashboard_appearance.png)

Per-widget controls for all five icons:

- **Base colour** — the colour when usage is low.
- **Colour stops** — threshold overrides, e.g. `50:#FFCC00,90:#D64E2A` turns yellow at 50%, red at 90%.
- **Fill mode** — *Level* (fills bottom-to-top) or *Angular* (sweeps clockwise like a gauge).
- **Text** — toggle the text overlay on each widget.
- **Custom shape** — upload your own SVG or raster image to replace the ghost silhouette.
</details>

<details>
<summary>Widget log</summary>

![Widget log section](docs/readmeImages/dashboard_widgetlog.png)

Select a past session and hit **Generate** to plot your usage over time alongside the claude.ai endpoint readings — useful for checking what your usage looked like over time and verifying the accuracy of the live estimate. Gridline spacing (every N% / N minutes), the marker lines dropped at each endpoint reading (vertical and/or horizontal), and colouring those readings by *why* the call fired are all configurable here.

![Widget log chart — local estimate vs claude.ai endpoint readings](docs/readmeImages/dashboard_widgetlog_chart.png)
</details>

---

## Setup

### Option A — prebuilt (Windows, no Python needed)

1. Download `ClaudeUsage-win64.zip` from the [Releases page](../../releases).
2. Unzip anywhere.
3. Log in to [claude.ai](https://claude.ai) in **Firefox** (see [Browser support](#browser-support)).
4. Double-click `ClaudeUsage.exe`.

The widget auto-detects your org ID and session cookie from Firefox — no configuration needed.

### Option B — from source

```
git clone <this-repo>
cd ClaudeUsageWidget
pip install -r requirements.txt
python tray_widget.py
```

Still requires Firefox logged in to [claude.ai](https://claude.ai).

---

## CLI

For scripting, Claude Code hooks, or checking usage mid-task without opening anything:

```
python usage_check.py            # current widget estimate (no network call)
python usage_check.py --live     # poll claude.ai for authoritative numbers
python usage_check.py --json     # machine-readable JSON (combine with either)
```

`--live` polls the same endpoint Claude Code uses for its own "X% of session used" banner. Works on any OS with Python and Firefox — no tray required.

---

## Making the icon always visible

Windows hides new tray icons under the `^` overflow. To pin them:

1. **Settings → Personalization → Taskbar**
2. Scroll to **Other system tray icons**
3. Toggle **Claude Usage** on.

The dashboard's **Settings** section has a **Start at login** toggle for auto-launch.

---

## How it works

The widget combines two sources:

**Live estimate (continuous):** Watches your Claude Code transcript files (`.jsonl`) for new responses. Each time Claude responds, that exchange's tokens are added to a running total — **weighted per model and per token type** (output and 1-hour cache-writes cost far more against the session limit than plain input, and an Opus token costs more than a Sonnet or Haiku one), using weights calibrated against claude.ai. Between polls the widget estimates the change in your usage since the last poll as `weighted_tokens_since_last_poll / session_budget`, where `session_budget` is derived from the most recent claude.ai reading.

**Calibration (periodic):** Polls claude.ai for your actual Pro/Max utilisation % using your Firefox session cookie. Polls are triggered by:
- Widget startup
- The configured polling interval (default every 20 minutes)
- The first time you cross a configured % threshold in a session
- Whenever the live estimate moves more than a configured number of points since the last poll
- Clicking **Confirm usage %** in the dashboard

**What the estimate misses** until the next poll:
- Usage on other devices
- Claude.ai web chat usage

---

## Accuracy

A typical single-device session looks like this:

![Accuracy chart — local estimate vs calibration points](docs/readmeImages/accuracy_sample.png)

The dots are claude.ai-confirmed readings; the line is the live estimate between them. On a single device, the estimate usually tracks within a few percentage points.

---

## Browser support

The widget reads your session cookie from Firefox's local cookie store using NSS (the same mechanism Firefox uses internally).

**Why only Firefox:** Since Chrome 127 (July 2024), Chromium-based browsers protect cookies with App-Bound Encryption tied to a SYSTEM-privilege helper. External processes can't access them without invasive techniques we're not willing to ship. Firefox remains accessible. You don't need to use Firefox as your default browser — just being logged in to [claude.ai](https://claude.ai) there is enough.

---

## Platform support

| Platform | Tray widget | `usage_check.py` |
|---|---|---|
| Windows | ✅ prebuilt + source | ✅ |
| macOS | source only (no packaging yet) | ✅ |
| Linux | source only (no packaging yet) | ✅ |

The tray icons are Windows-only today; macOS/Linux packaging is planned.

---

## Coming features

- **macOS & Linux packaging** — the core is cross-platform; only the tray packaging is Windows-only today.

---

## Risks

**Unofficial — not affiliated with Anthropic.** This tool works by replaying your logged-in Firefox session cookie against an **undocumented** claude.ai endpoint. Anthropic may not welcome a tool that surfaces per-session token limits, and they could change or block the endpoint at any time. **Use at your own risk, including the risk of account action.**

Polling claude.ai is kept to a minimum by design — once at startup, then on the configured schedule and triggers described above.

---

## For Claude

*If you're a user wanting Claude to help you use or debug this widget, copy this section into your Claude conversation — it gives Claude the context to be useful without having to explore the codebase first.*

**Architecture in one paragraph:** `tray_widget.py` owns the UI (pystray tray icons, a background HTTP server on port 7433 that serves `widget.html` as the dashboard). `widget_updater.py` owns all data logic: a `watchdog` filesystem watcher on `~/.claude/projects/**/*.jsonl` drives `TranscriptHandler`, which counts tokens incrementally and periodically polls claude.ai for the authoritative utilisation %. The rendered tray icons (PNG) are generated by Pillow in `tray_widget.py`. Widget shapes (custom silhouettes) are processed by `widget_shapes.py`.

**Key files:**
- `tray_widget.py` — tray icons, menus, HTTP server, tick loop, icon rendering
- `widget_updater.py` — `TranscriptHandler` (token counting + calibration), HTTP request handler
- `widget.html` — dashboard (single-file; served from the HTTP server, no framework)
- `widget_shapes.py` — custom shape upload/processing (SVG → raster mask)
- `usage_check.py` — standalone CLI; reads live state or polls claude.ai directly
- `usage_scraper/scrape.py` — Firefox cookie extraction + claude.ai endpoint call

**Live state** lives in `%LOCALAPPDATA%\ClaudeUsage\usage_data\` (not in the repo). `state.json` is the running widget's current session state. `calibration.jsonl` is an append-only log of every claude.ai-confirmed (pct, token_count) pair.

**Config** lives in `%LOCALAPPDATA%\ClaudeUsage\config.json`. Edit it directly or use the dashboard. The repo's root `config.json` is the build-time default baked into the exe — don't edit the `dist/` copy.

**Calibration logic:** Local tokens are counted as a *weighted* total — each token is weighted by model and by type (output and 1-hour cache-writes cost far more against the session limit than plain input; an Opus token more than a Sonnet/Haiku one), using ratios calibrated against the claude.ai endpoint. Between endpoint readings the widget extrapolates from the last reading: `level + SessionFactor · (weighted_io − anchor_io)`, where `SessionFactor` is the cleanest observed slope (min of `(pct / io)` across above-floor readings, robust to off-laptop inflation) and `level` is the anchor from the last reading. The endpoint reports an integer %, which we presume is round-to-nearest (so a reading of N means true ∈ [N−0.5, N+0.5)); the anchor reconciles that band with our prior estimate rather than blindly snapping to N (see `_snap_anchor_level`). A forced re-read fires if the estimate sprints ≥5pp past the last reading or pegs at the 100% clamp (cooldown-gated). Scheduled reads also fire on an interval, on a configurable %-moved delta, and the first time you cross configured fixed thresholds — the trigger reason is recorded per reading and can be surfaced on the chart. Session start is `resets_at − 5h` with a 30-second jitter tolerance to avoid phantom resets.

**Tests:** `pytest tests/` — 200+ tests, no network calls. All writers monkeypatch `STATE_FILE` and `CALIBRATION_FILE` to avoid touching real user data.

---

## License

MIT — see [LICENSE](LICENSE).
