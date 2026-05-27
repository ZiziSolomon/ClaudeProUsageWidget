#!/usr/bin/env python3
"""
Claude Usage - system-tray widget.

Up to four tray icons, each independently toggleable via any icon's
right-click menu:
  - session ghost: shape from widget.html, filled bottom-up by session %,
    blue fill (red at >=90%). On by default.
  - weekly ghost:  same shape, orange fill, driven by weekly %.
  - session %:     text-only icon in white, taskbar font.
  - weekly  %:     text-only icon in orange, taskbar font.

The toggle logic refuses to hide the last visible icon, so the menu (which
includes Quit) is always reachable.

Why not put text *next* to the ghost in the taskbar's slot: the taskbar slot
is just one bitmap, so each value gets its own slot. See DECISION.md.
"""

import ctypes
import json
import os
import subprocess
import sys
import threading
import time
import webbrowser
from ctypes import wintypes
from datetime import datetime, timedelta, timezone
from http.server import HTTPServer
from pathlib import Path

import pystray
from PIL import Image, ImageDraw, ImageFont
from watchdog.observers import Observer

# ---------------------------------------------------------------------------
# Startup-folder path and helpers for "Start at login".
# ---------------------------------------------------------------------------
_STARTUP_FOLDER = Path(os.environ.get("APPDATA", "")) / r"Microsoft\Windows\Start Menu\Programs\Startup"
_STARTUP_LNK    = _STARTUP_FOLDER / "Claude Usage.lnk"


def _startup_enabled() -> bool:
    """True if our startup shortcut is present in the Startup folder."""
    return _STARTUP_LNK.exists()


def _set_startup(enabled: bool) -> None:
    """Create or remove the Startup-folder shortcut."""
    if enabled:
        # Determine the exe / script target (same logic as install_start_menu.ps1).
        if getattr(sys, "frozen", False):
            target = sys.executable  # PyInstaller: ClaudeUsage.exe
        else:
            target = sys.executable  # python.exe ...
            # We'll pass the script as an argument via Arguments field.
        script = os.path.abspath(__file__)

        _STARTUP_FOLDER.mkdir(parents=True, exist_ok=True)
        wsh_script = f"""
import os, sys
import win32com.client
wsh = win32com.client.Dispatch('WScript.Shell')
lnk = wsh.CreateShortcut(r'{_STARTUP_LNK}')
lnk.TargetPath = r'{target}'
lnk.Arguments  = '' if {getattr(sys, 'frozen', False)} else r'"{script}"'
lnk.WorkingDirectory = r'{os.path.dirname(script)}'
lnk.WindowStyle = 7
lnk.Description = 'Claude session usage tray widget'
lnk.Save()
"""
        # Use subprocess + powershell WScript.Shell so we don't need win32com.
        frozen = getattr(sys, "frozen", False)
        if frozen:
            args_field = ""
        else:
            script_path = os.path.abspath(__file__)
            args_field = f'"{script_path}"'
        working_dir = os.path.dirname(os.path.abspath(__file__))

        ps = (
            f"$wsh = New-Object -ComObject WScript.Shell; "
            f"$lnk = $wsh.CreateShortcut('{_STARTUP_LNK}'); "
            f"$lnk.TargetPath = '{target}'; "
            f"$lnk.Arguments = '{args_field}'; "
            f"$lnk.WorkingDirectory = '{working_dir}'; "
            f"$lnk.WindowStyle = 7; "
            f"$lnk.Description = 'Claude session usage tray widget'; "
            f"$lnk.Save()"
        )
        subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True,
        )
    else:
        try:
            _STARTUP_LNK.unlink()
        except FileNotFoundError:
            pass

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

# Bold variants used for the reset-time number on the icon face.
_FONT_BOLD_CANDIDATES = {
    "Segoe UI Variable": [
        "SegUIVar.ttf",          # VF file supports bold via weight axis
        "SegoeUIVF.ttf",
    ],
    "Segoe UI": ["segoeuib.ttf"],  # segoeuib = Segoe UI Bold
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


def _resolve_bold_font_path(face: str) -> str | None:
    """Map a face name to a bold TTF path under C:\\Windows\\Fonts."""
    fonts_dir = Path(r"C:\Windows\Fonts")
    for candidate in _FONT_BOLD_CANDIDATES.get(face, []):
        p = fonts_dir / candidate
        if p.exists():
            return str(p)
    return _resolve_font_path(face)  # fall back to regular


def pick_taskbar_font(size: int, bold: bool = False) -> ImageFont.ImageFont:
    """Best-effort match for the Win11 taskbar typeface, sized for the tray.

    Pass bold=True for the reset-time number so it's legible at small sizes.
    """
    # Win11 taskbar specifically uses Segoe UI Variable Small; the legacy
    # SystemParametersInfo API can't see that, so override on Win11.
    if _is_windows_11():
        path = (_resolve_bold_font_path if bold else _resolve_font_path)("Segoe UI Variable")
        if path:
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                pass

    # Whatever the OS says is the status-bar font (usually Segoe UI).
    face = _query_system_status_font()
    path = (_resolve_bold_font_path if bold else _resolve_font_path)(face)
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
FILL_COLOR          = (42, 120, 214, 255)
ALERT_COLOR         = (214, 78, 42, 255)
WEEKLY_COLOR        = (245, 166, 35, 255)   # orange

# Desaturated grey used for disconnected/error ghost icons.
ERROR_GHOST_COLOR = (110, 110, 110, 200)

# Human-readable tooltips for each status value.
_STATUS_TOOLTIPS = {
    "no_cookie":      "Not logged in to claude.ai",
    "no_login":       "Not logged in to claude.ai",
    "fetch_error":    "claude.ai unreachable",
    "config_missing": "Org ID not configured",
    "tracker_down":   "Usage tracker stopped",
    "no_projects":    "Claude logs folder not found",
}


def render_ghost_error(size: int = 64) -> Image.Image:
    """Desaturated ghost with a small warning exclamation mark overlay,
    used whenever status != 'ok'."""
    mask = _body_mask(size)
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))

    ghost = Image.new("RGBA", (size, size), ERROR_GHOST_COLOR)
    img.paste(ghost, mask=mask)

    # Small warning marker: yellow "!" in the lower-right quadrant.
    d = ImageDraw.Draw(img)
    marker_r = max(6, size // 5)
    cx = size - marker_r - 2
    cy = size - marker_r - 2
    d.ellipse(
        (cx - marker_r, cy - marker_r, cx + marker_r, cy + marker_r),
        fill=(255, 200, 0, 230),
    )
    # "!" text centred in the circle.
    font_size = max(6, marker_r)
    font = pick_taskbar_font(font_size, bold=True)
    bbox = d.textbbox((0, 0), "!", font=font)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    tx = cx - tw / 2 - bbox[0]
    ty = cy - th / 2 - bbox[1]
    d.text((tx, ty), "!", font=font, fill=(0, 0, 0, 255))
    return img


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


def render_ghost(pct: float, size: int = 64, base_fill=FILL_COLOR,
                 alert_fill=ALERT_COLOR) -> Image.Image:
    pct = max(0.0, min(100.0, float(pct or 0)))
    mask = _body_mask(size)
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))

    ghost = Image.new("RGBA", (size, size), GHOST_COLOR)
    img.paste(ghost, mask=mask)

    fill_color = alert_fill if pct >= 90 else base_fill
    fill_layer = Image.new("RGBA", (size, size), fill_color)
    # Measure the fill against the ghost's actual vertical extent, not the
    # full icon height: _scale() centres the body, leaving empty margins
    # above the head and below the feet. Using `size` here painted low
    # percentages into the dead band beneath the feet, so anything under
    # ~23% showed no blue at all. getbbox() gives the true top/bottom.
    bbox = mask.getbbox()
    if bbox:
        top, bottom = bbox[1], bbox[3]
        cut_top = int(round(bottom - (bottom - top) * (pct / 100)))
        cut_top = max(top, min(bottom, cut_top))
        if cut_top < bottom:
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
# Session-reset icon: text label over an arc that fills clockwise as the
# 5h session window elapses. At session start the arc is empty; just before
# reset it's almost a full circle.
# ---------------------------------------------------------------------------

ARC_COLOR        = (42, 120, 214, 255)   # same blue as the session ghost
ARC_TRACK_COLOR  = (54, 54, 53, 110)     # dim grey "unfilled" track
ARC_ALERT_COLOR  = (214, 78, 42, 255)    # red when <10% of session remains


def _fmt_remaining_label(remaining_secs: float | None) -> str:
    """Short label for the icon face. Keep it 1-3 chars where possible.

    - Under 1 hour: show minutes as a plain number (e.g. "34").
    - 1 hour or more: show hours rounded to nearest (e.g. 2h41m -> "3h").
    """
    if remaining_secs is None or remaining_secs <= 0:
        return "0"
    mins = remaining_secs / 60
    if mins < 1:
        return "1"  # less than a minute -- show 1 rather than "<1m"
    if mins < 60:
        return f"{int(round(mins))}"
    # Round to nearest hour (standard rounding, not floor).
    hrs = int(round(mins / 60))
    return f"{hrs}h"


def render_reset_arc_icon(session_start, session_end,
                          size: int = 64) -> Image.Image:
    """Pie-arc background + remaining-time label.

    The arc represents *elapsed* time: it starts empty and fills clockwise
    until the session resets. So a glance at the icon tells you how close
    you are to a fresh window.
    """
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d   = ImageDraw.Draw(img)

    # Geometry: a full-bleed circle with a tiny margin so the edges don't
    # clip against the tray slot's bounds.
    margin = max(1, size // 32)
    box = (margin, margin, size - 1 - margin, size - 1 - margin)

    # Compute progress and remaining seconds. When data is missing, render
    # an empty track + a dash so the user knows the icon is alive but idle.
    if not session_start or not session_end:
        d.ellipse(box, outline=ARC_TRACK_COLOR, width=max(1, size // 24))
        _draw_centered_text(d, "--", size, (200, 200, 200, 255))
        return img

    if isinstance(session_start, str):
        session_start = datetime.fromisoformat(session_start)
    if isinstance(session_end, str):
        session_end = datetime.fromisoformat(session_end)

    now   = datetime.now(timezone.utc)
    # Once the window has elapsed there's no live session to count down, so
    # fall back to the same neutral empty-track + dash as the no-data case
    # rather than a misleading full arc reading "0".
    if now >= session_end:
        d.ellipse(box, outline=ARC_TRACK_COLOR, width=max(1, size // 24))
        _draw_centered_text(d, "--", size, (200, 200, 200, 255))
        return img

    total = (session_end - session_start).total_seconds()
    used  = (now - session_start).total_seconds()
    remaining = max(0.0, total - used)
    progress  = 0.0 if total <= 0 else max(0.0, min(1.0, used / total))

    # Track first (the "empty" part), then the pie slice for elapsed time.
    # pieslice angle 0 = east, so -90 puts the start at 12 o'clock.
    d.ellipse(box, fill=ARC_TRACK_COLOR)
    if progress > 0:
        alert = (remaining / total) <= 0.10 if total > 0 else False
        d.pieslice(
            box,
            start=-90,
            end=-90 + 360 * progress,
            fill=ARC_ALERT_COLOR if alert else ARC_COLOR,
        )

    # Label on top. White looks crisp on both the blue arc and the dim track.
    # Bold so the number is legible at small tray-icon sizes.
    _draw_centered_text(d, _fmt_remaining_label(remaining), size,
                        (255, 255, 255, 255), bold=True)
    return img


def _draw_centered_text(d: ImageDraw.ImageDraw, text: str, size: int, color,
                        bold: bool = False):
    """Shared centered-text routine. Smaller fit-fraction than the % icon
    because here the digits sit on top of a filled arc, so we want clear
    breathing room around them.

    bold=True uses the bold variant of the taskbar font for legibility at
    small tray-icon sizes (used for the reset-time number).
    """
    max_w = size * 0.62
    max_h = size * 0.62
    font_size = size
    font = pick_taskbar_font(font_size, bold=bold)
    while font_size > 6:
        bbox = d.textbbox((0, 0), text, font=font)
        if (bbox[2] - bbox[0]) <= max_w and (bbox[3] - bbox[1]) <= max_h:
            break
        font_size -= 2
        font = pick_taskbar_font(font_size, bold=bold)
    bbox = d.textbbox((0, 0), text, font=font)
    w = bbox[2] - bbox[0]; h = bbox[3] - bbox[1]
    x = (size - w) / 2 - bbox[0]
    y = (size - h) / 2 - bbox[1]
    d.text((x, y), text, font=font, fill=color)


# ---------------------------------------------------------------------------
# Reset-time tooltip helpers.
# ---------------------------------------------------------------------------

def _fmt_short(end) -> str:
    """Format time remaining for tooltip display.

    - Under 1 hour: "in 34m"
    - 1 hour+: "in 2h 41m" (minutes omitted if exactly on the hour)
    - 2+ days: "in Nd Nh"
    """
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
    hrs = mins // 60
    rem = mins % 60
    if hrs < 48:
        return f"in {hrs}h {rem}m" if rem else f"in {hrs}h"
    days = hrs // 24
    leftover_h = hrs % 24
    if leftover_h:
        return f"in {days}d {leftover_h}h"
    return f"in {days}d"


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
    "show_session_reset": False,
    # ISO timestamp until which we suppress "widget out of sync" prompts.
    # Stored as a string (or null). Picked up by _restart_prompts_snoozed().
    "restart_prompt_snoozed_until": None,
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
    ("show_session_reset", "claude-usage-session-reset", "Show time till session reset"),
]


class TrayApp:
    def __init__(self):
        self._state = {
            "session_start": None,
            "session_pct": None, "session_end": None,
            "weekly_pct":  None, "weekly_end":  None,
            # status may be absent from older widget_updater builds; treat
            # absence as "ok" (backward compatible).
            "status": "ok",
        }
        self._prefs = _load_prefs()
        self._stopping = False
        # Local-tracker problem, set by main()'s health check, kept separate
        # from the API `status` field so the two failure modes don't clobber
        # each other. None when the watcher is healthy; else (status, tooltip).
        self._tracker_issue = None
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

    # ------ status helpers -----------------------------------------------

    def _status_ok(self) -> bool:
        """True only when both the API link and the local watcher are healthy."""
        return (self._state.get("status", "ok") == "ok"
                and self._tracker_issue is None)

    def _status_tooltip(self) -> str:
        """Short human-readable reason for non-ok status. A dead local tracker
        takes precedence over an API issue: if we can't count locally, the
        number is wrong regardless of the link."""
        if self._tracker_issue is not None:
            return self._tracker_issue[1]
        status = self._state.get("status", "ok")
        return _STATUS_TOOLTIPS.get(status, f"Error ({status})")

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
            pystray.MenuItem("Restart widget", self._restart),
            pystray.MenuItem(
                "Don't prompt to restart again today",
                self._toggle_snooze,
                checked=lambda _i: self._restart_prompts_snoozed(),
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                "Start at login",
                self._toggle_startup,
                # State is the presence of the shortcut file — checked on
                # every menu render so it reflects external changes too.
                checked=lambda _i: _startup_enabled(),
            ),
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
        # When status is not ok, all ghost icons show the error variant and
        # text icons show "--" in a dimmed colour so the user notices
        # something is wrong without needing to hover.
        if not self._status_ok():
            if pref_key in ("show_session_ghost", "show_weekly_ghost"):
                return render_ghost_error(ICON_SIZE)
            if pref_key == "show_session_pct":
                return render_text_icon("--", (160, 160, 160, 255), TEXT_ICON_SIZE)
            if pref_key == "show_weekly_pct":
                return render_text_icon("--", (160, 140, 80, 255), TEXT_ICON_SIZE)
            if pref_key == "show_session_reset":
                return render_reset_arc_icon(None, None, ICON_SIZE)

        s_pct = self._state["session_pct"] or 0
        w_pct = self._state["weekly_pct"]  or 0
        if pref_key == "show_session_ghost":
            return render_ghost(s_pct, ICON_SIZE, base_fill=FILL_COLOR)
        if pref_key == "show_weekly_ghost":
            return render_ghost(w_pct, ICON_SIZE, base_fill=WEEKLY_COLOR,
                                alert_fill=WEEKLY_COLOR)
        if pref_key == "show_session_pct":
            return render_text_icon(_fmt_pct(self._state["session_pct"]),
                                    (255, 255, 255, 255), TEXT_ICON_SIZE)
        if pref_key == "show_weekly_pct":
            return render_text_icon(_fmt_pct(self._state["weekly_pct"]),
                                    WEEKLY_COLOR, TEXT_ICON_SIZE)
        if pref_key == "show_session_reset":
            return render_reset_arc_icon(
                self._state.get("session_start"),
                self._state.get("session_end"),
                ICON_SIZE,
            )
        raise KeyError(pref_key)

    def _title_for(self, pref_key: str) -> str:
        if not self._status_ok():
            # All icons show the same explanatory message when disconnected.
            return f"Claude Usage: {self._status_tooltip()}"
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
        if pref_key == "show_session_reset":
            return self._ghost_tooltip()
        return ""

    # ------ state updates ----------------------------------------------

    def on_state_change(self, payload: dict):
        # Defensive: if the payload has no "status" key (older widget_updater),
        # preserve the existing status rather than overwriting with None.
        if "status" not in payload:
            payload = {**payload, "status": self._state.get("status", "ok")}
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

    # ------ restart prompt + snooze ------------------------------------

    def _restart_prompts_snoozed(self) -> bool:
        until = self._prefs.get("restart_prompt_snoozed_until")
        if not until:
            return False
        try:
            return datetime.fromisoformat(until) > datetime.now(timezone.utc)
        except Exception:
            return False

    def _toggle_snooze(self, _icon, _item):
        # Toggle: if currently snoozed, clear it; otherwise snooze until
        # local midnight tonight. Using local time so "today" matches the
        # user's expectation rather than UTC drift.
        if self._restart_prompts_snoozed():
            self._prefs["restart_prompt_snoozed_until"] = None
        else:
            now_local = datetime.now()
            tomorrow_local = (now_local + timedelta(days=1)).replace(
                hour=0, minute=0, second=0, microsecond=0)
            # Store as UTC ISO so comparison in _restart_prompts_snoozed
            # is unambiguous across DST etc.
            snooze_until_utc = tomorrow_local.astimezone(timezone.utc)
            self._prefs["restart_prompt_snoozed_until"] = snooze_until_utc.isoformat()
        _save_prefs(self._prefs)

    def _toast(self, reason: str):
        """Pop a tray balloon unless the user snoozed prompts for today.
        pystray attaches the balloon to a specific Icon object, so host it on
        whichever icon is currently visible."""
        if self._restart_prompts_snoozed() or self._stopping:
            return
        host = next(
            (i for k, i in self.icons.items() if self._prefs[k]),
            next(iter(self.icons.values())),
        )
        try:
            host.notify(
                f"{reason}\nRight-click the tray icon to Restart.",
                "Claude Usage",
            )
        except Exception as e:
            print(f"  notify error: {e}")

    def on_disconnect(self, reason: str):
        """Called by the updater when it suspects we're out of sync with the
        live API (repeated fetch failures)."""
        self._toast(reason)

    def on_tracker_status(self, status: str, reason: str):
        """Called by main()'s health check when the local JSONL watcher is
        dead or the projects folder is missing. Greys the icon and toasts
        once per distinct problem (not every tick)."""
        first = self._tracker_issue is None or self._tracker_issue[0] != status
        self._tracker_issue = (status, reason)
        self._refresh_all()
        if first:
            self._toast(reason)

    def on_tracker_recovered(self):
        """Clear a previously-reported tracker problem once the watcher is
        healthy again."""
        if self._tracker_issue is not None:
            self._tracker_issue = None
            self._refresh_all()

    def _restart(self, _icon, _item):
        """Spawn a fresh copy of ourselves, then quit. Uses sys.executable
        which on a PyInstaller --onedir build resolves to ClaudeUsage.exe,
        and to python.exe when running the script directly - both produce
        a new tray instance with current code."""
        try:
            # DETACHED_PROCESS + CREATE_NEW_PROCESS_GROUP so the child
            # outlives us cleanly; close_fds avoids inheriting tray handles.
            DETACHED = 0x00000008
            NEW_GROUP = 0x00000200
            argv = [sys.executable]
            # If we're running as a python script (not frozen), pass our
            # script path so the new interpreter knows what to run.
            if not getattr(sys, "frozen", False):
                argv.append(os.path.abspath(__file__))
            subprocess.Popen(
                argv,
                close_fds=True,
                creationflags=DETACHED | NEW_GROUP,
            )
        except Exception as e:
            print(f"  restart spawn error: {e}")
            return
        self._quit(_icon, _item)

    def _toggle_startup(self, _icon, _item):
        """Toggle "Start at login" by creating or removing the Startup shortcut."""
        _set_startup(not _startup_enabled())

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
    # Redirect stdout/stderr to a log file so prints survive the --windowed
    # PyInstaller build (runw.exe discards both streams by default).
    log_path = STATE_FILE.parent / "widget_run.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    _log_fh = open(log_path, "a", encoding="utf-8", buffering=1)
    sys.stdout = _log_fh
    sys.stderr = _log_fh

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

    handler = TranscriptHandler(
        on_state_change=tray.on_state_change,
        on_disconnect=tray.on_disconnect,
    )
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

    # Held in a one-element dict so the health check can swap in a fresh
    # Observer when the old watcher thread dies.
    observer_box = {"obs": observer}

    def _check_tracker():
        """Cheap, local watcher-health probe (no network). Runs every tick.
        Catches the failure mode the API status can't: the JSONL watcher
        silently dying or the projects folder going missing, which would
        otherwise freeze the estimate with no warning."""
        if not PROJECTS_DIR.exists():
            tray.on_tracker_status(
                "no_projects",
                "Can't find the Claude logs folder - usage tracking is paused.",
            )
            return
        obs = observer_box["obs"]
        if not obs.is_alive():
            print("  watcher thread died; restarting observer")
            try:
                new_obs = Observer()
                new_obs.schedule(handler, str(PROJECTS_DIR), recursive=True)
                new_obs.start()
                observer_box["obs"] = new_obs
                tray.on_tracker_status(
                    "tracker_down",
                    "Usage tracker stopped and was auto-restarted.",
                )
            except Exception as e:
                print(f"  observer restart failed: {e}")
                tray.on_tracker_status(
                    "tracker_down",
                    "Usage tracker stopped and could not restart.",
                )
            return
        tray.on_tracker_recovered()

    # 30-second ticker: watcher-health probe, authoritative session rollover,
    # then refresh the tooltip countdown / icon.
    TICK_SECS = 30

    def _next_tick_sleep() -> float:
        """Normally TICK_SECS, but if the session window ends within the next
        interval, shorten THIS sleep to land ~1s after the boundary. That way
        the rollover below fires right at the reset (a ~1s delta, comfortably
        inside ROLLOVER_GRACE_SECS) and snaps cleanly to a fresh 0% instead of
        drifting up to a full tick late."""
        now = datetime.now(timezone.utc)
        end = handler.session_end
        if end and now < end <= now + timedelta(seconds=TICK_SECS):
            return max(1.0, (end - now).total_seconds() + 1.0)
        return TICK_SECS

    def _ticker():
        while True:
            time.sleep(_next_tick_sleep())
            try:
                _check_tracker()
            except Exception as e:
                print(f"  tracker check error: {e}")
            try:
                # Network-free, event-free session rollover the instant the
                # window ends. On a live catch, immediately re-anchor against
                # the API so the new window's % + countdown populate fast
                # instead of waiting for the hourly tick.
                if handler._roll_over_if_expired():
                    handler.force_refresh()
            except Exception as e:
                print(f"  rollover check error: {e}")
            try:
                now = datetime.now(timezone.utc)
                est = handler._local_estimate()
                if est is not None and handler._estimate_is_suspect(est, now):
                    handler.last_forced_recal = now
                    handler._maybe_calibrate(force=True)
            except Exception as e:
                print(f"  suspect-estimate check error: {e}")
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
        observer_box["obs"].stop()
        observer_box["obs"].join(timeout=2)


if __name__ == "__main__":
    main()
