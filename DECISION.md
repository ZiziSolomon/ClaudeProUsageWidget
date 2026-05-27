# Why a system-tray icon, not a "real" Start Menu widget

You asked for a Start Menu widget. Windows 11 doesn't actually have those in
the sense Windows 7/8/8.1 did. The three real options on current Windows:

| Option | Verdict |
| --- | --- |
| **Win+W Widgets Board** (the panel that slides in from the left) | Closest to a "real" widget, but requires shipping the app as an MSIX package with a signed certificate, Adaptive Cards JSON, and a WidgetProvider COM server. Weeks of yak-shaving for a 60-line web view. Rejected. |
| **Start Menu Live Tile** | Removed in Windows 11. Start tiles are static icons now. Can't show live %. Rejected. |
| **System tray icon** | Lives in the taskbar next to Start, always visible, supports live icon + tooltip updates, no packaging or signing. Picked this. |
| Pinned shortcut to widget.html | Static icon, no live data unless you open the browser. Worse than what you already had. Rejected. |

## What I built

- **`tray_widget.py`** - new entry point. Embeds the existing
  `TranscriptHandler` watcher and `_WidgetHandler` HTTP server from
  `widget_updater.py`, then adds a `pystray` tray icon. The icon is rendered
  with Pillow from the same ghost-shape coordinates as `widget.html`, so the
  tray icon and the web widget look identical: dark unfilled ghost,
  blue fill rising from the bottom in proportion to `session_pct`. Goes red
  at >=90%.
- **Tooltip** - `Claude: 19% - resets in 2h 50m`, refreshed every 30s plus
  on every JSONL event.
- **Menu** - status label (disabled), Open dashboard (opens the existing
  HTML widget at `http://127.0.0.1:7433/`), Quit.
- **`claude_usage.ico`** - multi-resolution icon (16/24/32/48/64/128/256)
  rendered at 50% fill so it looks reasonable in the Start Menu where the
  app isn't running yet.
- **`install_start_menu.ps1`** - drops a `.lnk` in the current user's Start
  Menu Programs folder pointing at `pythonw.exe tray_widget.py`. Already
  run; the shortcut is installed at
  `%APPDATA%\Microsoft\Windows\Start Menu\Programs\Claude Usage.lnk`.
  Right-click it in Start to Pin to Start if you want it on the front page.

## Small edits to `widget_updater.py`

`TranscriptHandler` now accepts an `on_state_change=` callback and fires it
on startup, on calibration, and on any token-count delta. The HTML widget's
HTTP path is unchanged - the old `main()` still works if you ever want to
run the watcher headless.

## Things I deliberately didn't do

- **Did not add to autostart.** You can do this yourself by copying the
  shortcut into `shell:startup`, or pinning to Start, or adding a Task
  Scheduler entry. Felt presumptuous to make it auto-launch without asking.
- **Did not delete `widget.html` or `widget_updater.py`.** The dashboard is
  reachable via the tray menu and still useful for the detail view.
- **Did not package as an MSIX/Widgets-Board widget.** As above, the
  payoff is too small for the work involved unless you specifically want
  it in the Win+W panel.

## Things to know

- Dependencies added: `pystray 0.19.5`, `pyinstaller 6.20.0`. `pillow` was
  already installed.
- The Start Menu shortcut points at the PyInstaller-built
  `dist\ClaudeUsage\ClaudeUsage.exe`, not at `pythonw.exe`. Reason: the
  Windows 11 "Other system tray icons" list (Settings -> Personalization
  -> Taskbar) shows each app's `FileDescription` PE resource. pythonw.exe
  has FileDescription = "Python", so the tray icon was listed as "python".
  ClaudeUsage.exe has FileDescription = "Claude Usage" (see
  `version_info.txt`), so it now lists correctly. If you ever rebuild,
  re-run `install_start_menu.ps1` to refresh the shortcut.
- Rebuild command: `.\build.ps1` (add `-Run` to relaunch). This stops any
  running instance first (the running exe locks `dist\`), builds from
  `ClaudeUsage.spec`, then verifies the bundle.
  DO NOT rebuild with `pyinstaller ... tray_widget.py` — passing the .py
  regenerates the spec from scratch and drops `datas=[('config.json','.')]`,
  so config.json stops being bundled (the "config.json rebuild hazard").
  Always build from the spec.
- For development you can still run `python tray_widget.py` directly --
  it'll show up as "python" in tray settings, but you get console logs.
- Calibration still depends on Firefox cookies for `claude.ai`, same as
  before - this is inherited from `widget_updater.py`, not something the
  tray introduced.
