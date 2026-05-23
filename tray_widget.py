#!/usr/bin/env python3
"""
Claude Usage - system-tray widget.

Up to four tray icons, each independently toggleable via any icon's
right-click menu:
  - session ghost: shape from widget.html, filled bottom-up by session %,
    blue fill (red at >=90%). On by default.
  - weekly ghost:  same shape, orange fill (red at >=90%), driven by weekly %.
  - session %:     text-only icon in white, taskbar font.
  - weekly  %:     text-only icon in orange, taskbar font.

The toggle logic refuses to hide the last visible icon, so the menu (which
includes Quit) is always reachable.

Why not put text *next* to the ghost in the taskbar's slot: the taskbar slot
is just one bitmap, so each value gets its own slot. See DECISION.md.
"""

import ctypes
import json
import sys
import threading
import time
import webbrowser
from ctypes import wintypes
from datetime import datetime, timezone
from http.server import HTTPServer
from pathlib import Path

import pystray
from PIL import Image, ImageDraw, ImageFont
from watchdog.observers import Observer

from widget_updater import (
    PROJECTS_DIR,
    SERVER_PORT,
    STATE_FILE,
    TranscriptHandler,
    _WidgetHandler,
)

PREFS_FILE = STATE_FILE.parent / "tray_prefs.json"

# ---------------------------------------------------------------------------
# Taskbar font detection.
#
# Windows exposes the "canonical" UI fonts via SystemParametersInfo +
# SPI_GETNONCLIENTMETRICS -> NONCLIENTMETRICS struct. lfStatusFont is what
# status-bar-like UI uses (closest legacy match to taskbar text).
#
# Caveat: the Win11 taskbar is rendered via XAML, not GDI, and actually uses
# "Segoe UI Variable Small" - which the legacy API doesn't report. So we
# layer a Win11 override on top of the API answer.
# ---------------------------------------------------------------------------

LF_FACESIZE = 32


class _LOGFONT(ctypes.Structure):
    _fields_ = [
        ("lfHeight",         wintypes.LONG),
        ("lfWidth",          wintypes.LONG),
        ("lfEscapement",     wintypes.LONG),
        ("lfOrientation",    wintypes.LONG),
        ("lfWeight",         wintypes.LONG),
        ("lfItalic",         wintypes.BYTE),
        ("lfUnderline",      wintypes.BYTE),
        ("lfStrikeOut",      wintypes.BYTE),
        ("lfCharSet",        wintypes.BYTE),
        ("lfOutPrecision",   wintypes.BYTE),
        ("lfClipPrecision",  wintypes.BYTE),
        ("lfQuality",        wintypes.BYTE),
        ("lfPitchAndFamily", wintypes.BYTE),
        ("lfFaceName",       wintypes.WCHAR * LF_FACESIZE),
    ]


class _NONCLIENTMETRICS(ctypes.Structure):
    _fields_ = [
        ("cbSize",             wintypes.UINT),
        ("iBorderWidth",       ctypes.c_int),
        ("iScrollWidth",       ctypes.c_int),
        ("iScrollHeight",      ctypes.c_int),
        ("iCaptionWidth",      ctypes.c_int),
        ("iCaptionHeight",     ctypes.c_int),
        ("lfCaptionFont",      _LOGFONT),
        ("iSmCaptionWidth",    ctypes.c_int),
        ("iSmCaptionHeight",   ctypes.c_int),
        ("lfSmCaptionFont",    _LOGFONT),
        ("iMenuWidth",         ctypes.c_int),
        ("iMenuHeight",        ctypes.c_int),
        ("lfMenuFont",         _LOGFONT),
        ("lfStatusFont",       _LOGFONT),
        ("lfMessageFont",      _LOGFONT),
        ("iPaddedBorderWidth", ctypes.c_int),
    ]


SPI_GETNONCLIENTMETRICS = 0x0029

# Mapping from face-name to a TTF file that PIL can load. Windows registers
# fonts with friendly names, but PIL needs a path. Order matters - we try
# from most-specific to most-general.
_FONT_FILE_CANDIDATES = {
    "Segoe UI Variable": [
        "SegUIVar.ttf",          # 22000+ ships this single VF file
        "SegoeUIVF.ttf",
    ],
    "Segoe UI": ["segoeui.ttf"],
}


def _query_system_status_font() -> str:
    """Ask Win32 what font it considers canonical for status-bar UI."""
    try:
        ncm = _NONCLIENTMETRICS()
        ncm.cbSize = ctypes.sizeof(_NONCLIENTMETRICS)
        ok = ctypes.windll.user32.SystemParametersInfoW(
            SPI_GETNONCLIENTMETRICS, ncm.cbSize, ctypes.byref(ncm), 0
        )
        if ok:
            return ncm.lfStatusFont.lfFaceName or "Segoe UI"
    except Exception:
        pass
    return "Segoe UI"


def _is_windows_11() -> bool:
    try:
        return sys.platform == "win32" and sys.getwindowsversion().build >= 22000
    except Exception:
        return False


def _resolve_font_path(face: str) -> str | None:
    """Map a face name to a TTF path under C:\\Windows\\Fonts."""
    fonts_dir = Path(r"C:\Windows\Fonts")
    for candidate in _FONT_FILE_CANDIDATES.get(face, []):
        p = fonts_dir / candidate
        if p.exists():
            return str(p)
    # Fallback: try a generic filename built from the face name.
    fallback = fonts_dir / (face.replace(" ", "").lower() + ".ttf")
    if fallback.exists():
        return str(fallback)
    return None


def pick_taskbar_font(size: int) -> ImageFont.ImageFont:
    """Best-effort match for the Win11 taskbar typeface, sized for the tray."""
    # Win11 taskbar specifically uses Segoe UI Variable Small; the legacy
    # SystemParametersInfo API can't see that, so override on Win11.
    if _is_windows_11():
        path = _resolve_font_path("Segoe UI Variable")
        if path:
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                pass

    # Whatever the OS says is the status-bar font (usually Segoe UI).
    face = _query_system_status_font()
    path = _resolve_font_path(face)
    if path:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            pass

    # Last-resort fallback.
    return ImageFont.load_default()


# ---------------------------------------------------------------------------
# Ghost icon (matches widget.html).
# ---------------------------------------------------------------------------

VIEW_W, VIEW_H = 121, 76

GHOST_OUTLINE = [
    (17, 36), (17, 48), (29, 48), (29, 59), (35, 59), (35, 71), (41, 71),
    (41, 59), (47, 59), (47, 71), (53, 71), (53, 59), (77, 59), (77, 71),
    (83, 71), (83, 59), (89, 59), (89, 71), (95, 71), (95, 59), (101, 59),
    (101, 48), (113, 48), (113, 36), (101, 36), (101, 13), (29, 13),
    (29, 36),
]
EYE_LEFT  = [(41, 25), (41, 36), (47, 36), (47, 25)]
EYE_RIGHT = [(83, 25), (83, 36), (89, 36), (89, 25)]

GHOST_COLOR  = (54, 54, 53, 255)
FILL_COLOR   = (42, 120, 214, 255)
ALERT_COLOR  = (214, 78, 42, 255)
WEEKLY_COLOR = (245, 166, 35, 255)   # orange


def _scale(points, size):
    s = min(size / VIEW_W, size / VIEW_H)
    ox = (size - VIEW_W * s) / 2
    oy = (size - VIEW_H * s) / 2
    return [(ox + x * s, oy + y * s) for x, y in points]


def _body_mask(size: int) -> Image.Image:
    mask = Image.new("L", (size, size), 0)
    d = ImageDraw.Draw(mask)
    d.polygon(_scale(GHOST_OUTLINE, size), fill=255)
    d.polygon(_scale(EYE_LEFT,  size), fill=0)
    d.polygon(_scale(EYE_RIGHT, size), fill=0)
    return mask


def render_ghost(pct: float, size: int = 64, base_fill=FILL_COLOR) -> Image.Image:
    pct = max(0.0, min(100.0, float(pct or 0)))
    mask = _body_mask(size)
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))

    ghost = Image.new("RGBA", (size, size), GHOST_COLOR)
    img.paste(ghost, mask=mask)

    # Alert red overrides whatever base colour was passed in - it's a
    # threshold warning, not a per-metric style.
    fill_color = ALERT_COLOR if pct >= 90 else base_fill
    fill_layer = Image.new("RGBA", (size, size), fill_color)
    cut_top = int(round(size * (1 - pct / 100)))
    if cut_top < size:
        bottom_mask = Image.new("L", (size, size), 0)
        bottom_mask.paste(mask.crop((0, cut_top, size, size)), (0, cut_top))
        img.paste(fill_layer, mask=bottom_mask)
    return img


# ---------------------------------------------------------------------------
# Text icon (e.g. "19%" rendered in white or orange on transparent).
# ---------------------------------------------------------------------------

def render_text_icon(text: str, color, size: int = 64) -> Image.Image:
    """Render `text` centred in a `size`x`size` icon with the taskbar font.

    We render onto a transparent background so the taskbar's own colour /
    acrylic shows through behind the digits.
    """
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # Auto-fit: pick the largest font size such that the rendered text fits
    # within ~90% of the icon both horizontally and vertically.
    max_w = size * 0.92
    max_h = size * 0.92
    font_size = size  # start from full height, shrink until it fits
    font = pick_taskbar_font(font_size)
    while font_size > 6:
        bbox = d.textbbox((0, 0), text, font=font)
        w = bbox[2] - bbox[0]
        h = bbox[3] - bbox[1]
        if w <= max_w and h <= max_h:
            break
        font_size -= 2
        font = pick_taskbar_font(font_size)

    bbox = d.textbbox((0, 0), text, font=font)
    w = bbox[2] - bbox[0]
    h = bbox[3] - bbox[1]
    # textbbox returns coordinates including font ascent offset; compensate.
    x = (size - w) / 2 - bbox[0]
    y = (size - h) / 2 - bbox[1]
    d.text((x, y), text, font=font, fill=color)
    return img


# ---------------------------------------------------------------------------
# Reset-time tooltip helpers.
# ---------------------------------------------------------------------------

def _fmt_short(end) -> str:
    if not end:
        return ""
    if isinstance(end, str):
        end = datetime.fromisoformat(end)
    now = datetime.now(timezone.utc)
    mins = round((end - now).total_seconds() / 60)
    if mins <= 0:
        return "resetting"
    if mins < 60:
        return f"in {mins}m"
    hrs = mins / 60
    if hrs < 48:
        return f"in {int(hrs)}h {mins % 60}m"
    return f"in {int(hrs / 24)}d {int(hrs) % 24}h"


def _fmt_pct(pct):
    return "--" if pct is None else f"{round(pct)}%"


# ---------------------------------------------------------------------------
# Tray prefs persistence.
# ---------------------------------------------------------------------------

DEFAULT_PREFS = {
    "show_session_ghost": True,
    "show_weekly_ghost":  False,
    "show_session_pct":   False,
    "show_weekly_pct":    False,
}


def _load_prefs() -> dict:
    try:
        data = json.loads(PREFS_FILE.read_text())
        return {**DEFAULT_PREFS, **data}
    except Exception:
        return dict(DEFAULT_PREFS)


def _save_prefs(prefs: dict) -> None:
    PREFS_FILE.parent.mkdir(parents=True, exist_ok=True)
    PREFS_FILE.write_text(json.dumps(prefs, indent=2))


# ---------------------------------------------------------------------------
# Tray app: a ghost icon plus optional text icons.
# ---------------------------------------------------------------------------

ICON_SIZE = 64
TEXT_ICON_SIZE = 64


# Each entry: (pref_key, icon_name, menu_label) for the four toggleable icons.
# Order here is the order they appear in the menu.
ICON_SPECS = [
    ("show_session_ghost", "claude-usage-session-ghost", "Show session ghost"),
    ("show_weekly_ghost",  "claude-usage-weekly-ghost",  "Show weekly ghost"),
    ("show_session_pct",   "claude-usage-session-pct",   "Show session %"),
    ("show_weekly_pct",    "claude-usage-weekly-pct",    "Show weekly %"),
]


class TrayApp:
    def __init__(self):
        self._state = {
            "session_pct": None, "session_end": None,
            "weekly_pct":  None, "weekly_end":  None,
        }
        self._prefs = _load_prefs()
        self._stopping = False
        # Wired in by main() so the menu's "Confirm usage %" and the
        # hourly ticker can trigger an API hit.
        self.refresh_callback = None

        # Build all four icons up front. Visibility is driven by self._prefs
        # via the .visible attribute, so toggling on/off is cheap and the
        # main event loop (run() on the session ghost) never needs to stop.
        self.icons: dict[str, pystray.Icon] = {}
        for pref_key, name, _label in ICON_SPECS:
            self.icons[pref_key] = pystray.Icon(
                name,
                icon=self._render_icon_for(pref_key),
                title=self._title_for(pref_key),
                menu=self._build_menu(),
                # pystray on Windows lets us set visibility before the icon
                # is shown; the run loop will honour it.
            )

    # ------ menu --------------------------------------------------------

    def _build_menu(self) -> pystray.Menu:
        # The same menu is attached to every icon, so the user can right-click
        # whichever one's visible and reach all toggles + Quit.
        items = [
            pystray.MenuItem(lambda _: self._ghost_tooltip(), None, enabled=False),
            pystray.Menu.SEPARATOR,
        ]
        for pref_key, _name, label in ICON_SPECS:
            items.append(pystray.MenuItem(
                label,
                self._make_toggle(pref_key),
                # Bind pref_key per-iteration via default arg to dodge
                # Python's late-binding lambda gotcha.
                checked=lambda _i, k=pref_key: self._prefs[k],
            ))
        items += [
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Confirm usage %", self._confirm_usage),
            pystray.MenuItem("Open dashboard", self._open_dashboard, default=True),
            pystray.MenuItem("Quit", self._quit),
        ]
        return pystray.Menu(*items)

    def _make_toggle(self, pref_key: str):
        def handler(_icon, _item):
            # Refuse to hide the last visible icon - otherwise the menu
            # (and therefore Quit) becomes unreachable.
            currently_on = self._prefs[pref_key]
            if currently_on and self._visible_count() <= 1:
                self.icons[pref_key].notify(
                    "Keep at least one icon visible so the menu stays reachable.",
                    "Claude Usage",
                )
                return
            self._prefs[pref_key] = not currently_on
            _save_prefs(self._prefs)
            self._apply_visibility()
            self._refresh_all()
        return handler

    def _visible_count(self) -> int:
        return sum(1 for k in self.icons if self._prefs[k])

    def _apply_visibility(self):
        for pref_key, icon in self.icons.items():
            icon.visible = self._prefs[pref_key]

    def _confirm_usage(self, _icon, _item):
        """User-triggered API hit to re-verify the current utilisation."""
        if self.refresh_callback is None:
            return
        # Run off the UI thread - the API call can take a few seconds.
        threading.Thread(target=self.refresh_callback, daemon=True).start()

    # ------ rendering ---------------------------------------------------

    def _render_icon_for(self, pref_key: str) -> Image.Image:
        s_pct = self._state["session_pct"] or 0
        w_pct = self._state["weekly_pct"]  or 0
        if pref_key == "show_session_ghost":
            return render_ghost(s_pct, ICON_SIZE, base_fill=FILL_COLOR)
        if pref_key == "show_weekly_ghost":
            return render_ghost(w_pct, ICON_SIZE, base_fill=WEEKLY_COLOR)
        if pref_key == "show_session_pct":
            return render_text_icon(_fmt_pct(self._state["session_pct"]),
                                    (255, 255, 255, 255), TEXT_ICON_SIZE)
        if pref_key == "show_weekly_pct":
            return render_text_icon(_fmt_pct(self._state["weekly_pct"]),
                                    WEEKLY_COLOR, TEXT_ICON_SIZE)
        raise KeyError(pref_key)

    def _title_for(self, pref_key: str) -> str:
        if pref_key in ("show_session_ghost", "show_weekly_ghost"):
            # Both ghosts get the full two-line tooltip so the user can read
            # either window's status from whichever ghost they hover.
            return self._ghost_tooltip()
        if pref_key == "show_session_pct":
            return (f"Session {_fmt_pct(self._state['session_pct'])} "
                    f"{_fmt_short(self._state['session_end'])}").strip()
        if pref_key == "show_weekly_pct":
            return (f"Weekly {_fmt_pct(self._state['weekly_pct'])} "
                    f"{_fmt_short(self._state['weekly_end'])}").strip()
        return ""

    # ------ state updates ----------------------------------------------

    def on_state_change(self, payload: dict):
        self._state.update(payload)
        self._refresh_all()

    def tick(self):
        # Snap pct to 0 when the relevant window has expired (mirrors widget.html).
        end = self._state["session_end"]
        if isinstance(end, str):
            end = datetime.fromisoformat(end)
        if end and end < datetime.now(timezone.utc):
            self._state["session_pct"] = 0
        self._refresh_all()

    def _refresh_all(self):
        if self._stopping:
            return
        for pref_key, icon in self.icons.items():
            try:
                icon.icon = self._render_icon_for(pref_key)
                icon.title = self._title_for(pref_key)
            except Exception as e:
                print(f"  {pref_key} refresh error: {e}")

    def _ghost_tooltip(self) -> str:
        s = _fmt_pct(self._state["session_pct"])
        w = _fmt_pct(self._state["weekly_pct"])
        s_when = _fmt_short(self._state["session_end"])
        w_when = _fmt_short(self._state["weekly_end"])
        # Two-line tooltip - Windows renders this in the system font.
        return f"Session: {s} {s_when}\nWeekly: {w} {w_when}".strip()

    # ------ misc --------------------------------------------------------

    def _open_dashboard(self, _i, _it):
        webbrowser.open(f"http://127.0.0.1:{SERVER_PORT}/")

    def _quit(self, _icon, _it):
        self._stopping = True
        for icon in self.icons.values():
            try: icon.stop()
            except Exception: pass

    def _make_setup(self, pref_key: str):
        # pystray.Icon.run/run_detached internally set visible=True before
        # firing the setup callback. We use the callback to immediately
        # restore the user's pref - so an icon the user has toggled off
        # never flashes into the tray on startup.
        def setup(icon):
            icon.visible = self._prefs[pref_key]
        return setup

    def run(self):
        # If somehow no icon is enabled (corrupt prefs?), force the session
        # ghost on so the user has a way back to the menu.
        if self._visible_count() == 0:
            self._prefs["show_session_ghost"] = True
            _save_prefs(self._prefs)

        # We need at least one icon to host the blocking run() call. Pick
        # the first one regardless of its visibility - a hidden icon's
        # event loop still runs, so menus on the *other* visible icons
        # keep working.
        items = list(self.icons.items())
        primary_key, primary_icon = items[0]
        for key, icon in items[1:]:
            icon.run_detached(setup=self._make_setup(key))
        primary_icon.run(setup=self._make_setup(primary_key))


# ---------------------------------------------------------------------------
# Entry point.
# ---------------------------------------------------------------------------

def main():
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "ClaudeUsage.Widget"
        )
    except Exception:
        pass

    tray = TrayApp()

    server = HTTPServer(("127.0.0.1", SERVER_PORT), _WidgetHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"Widget HTTP at http://127.0.0.1:{SERVER_PORT}/")

    handler = TranscriptHandler(on_state_change=tray.on_state_change)
    tray.refresh_callback = handler.force_refresh
    tray.on_state_change({
        "session_pct": handler.session_pct,
        "session_end": handler.session_end,
        "weekly_pct":  handler.weekly_pct,
        "weekly_end":  handler.weekly_end,
    })

    observer = Observer()
    observer.schedule(handler, str(PROJECTS_DIR), recursive=True)
    observer.start()
    print(f"Watching {PROJECTS_DIR}")

    # 30-second ticker: refreshes the tooltip countdown + zeros the icon
    # if the session window expires while the user is idle.
    def _ticker():
        while True:
            time.sleep(30)
            tray.tick()
    threading.Thread(target=_ticker, daemon=True).start()

    # Hourly ticker: hits the claude.ai API even when there's no JSONL
    # activity, so weekly% and session% don't go stale while the laptop
    # is on but idle. Sleeps in 60s chunks so a quit is responsive.
    HOURLY = 3600
    def _hourly_refresh():
        elapsed = 0
        while True:
            time.sleep(60)
            elapsed += 60
            if elapsed >= HOURLY:
                elapsed = 0
                try:
                    handler.force_refresh()
                except Exception as e:
                    print(f"  hourly refresh error: {e}")
    threading.Thread(target=_hourly_refresh, daemon=True).start()

    try:
        tray.run()
    finally:
        observer.stop()
        observer.join(timeout=2)


if __name__ == "__main__":
    main()
