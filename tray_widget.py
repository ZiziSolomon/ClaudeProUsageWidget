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
# Start-menu shortcut created by install_start_menu.ps1 (Programs known folder
# == the Startup folder's parent). Uninstall must remove this too, or a dead
# entry pointing at the deleted exe is left behind.
_START_MENU_LNK = _STARTUP_FOLDER.parent / "Claude Usage.lnk"


def _shortcut_target_args() -> "tuple[str, str]":
    """(TargetPath, Arguments) the shortcuts should point at. Frozen build: the
    ClaudeUsage.exe itself, no args. Source checkout: the Python interpreter
    plus this script. Centralised so every shortcut we create is consistent -
    the historic divergence here is exactly what left a stale Start-menu .lnk."""
    if getattr(sys, "frozen", False):
        return sys.executable, ""
    return sys.executable, f'"{os.path.abspath(__file__)}"'


def _write_shortcut(lnk_path: Path, *, icon: bool = False) -> None:
    """Create/overwrite a .lnk pointing at the widget.

    WindowStyle 7 = start minimized (straight to the tray, no flashing window).
    Driven through PowerShell's WScript.Shell COM object so we carry no
    win32com runtime dependency. Always overwrites, so toggling a shortcut off
    then on regenerates it cleanly - self-healing against any stale shortcut.
    """
    target, args = _shortcut_target_args()
    working_dir = os.path.dirname(os.path.abspath(__file__))
    lnk_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "$wsh = New-Object -ComObject WScript.Shell",
        f"$lnk = $wsh.CreateShortcut('{lnk_path}')",
        f"$lnk.TargetPath = '{target}'",
        f"$lnk.Arguments = '{args}'",
        f"$lnk.WorkingDirectory = '{working_dir}'",
        "$lnk.WindowStyle = 7",
        "$lnk.Description = 'Claude session usage tray widget'",
    ]
    if icon and getattr(sys, "frozen", False):
        # The frozen exe has the widget icon embedded; point the shortcut at it
        # so the Start-menu tile shows the Claude icon, not a generic one.
        lines.append(f"$lnk.IconLocation = '{target},0'")
    lines.append("$lnk.Save()")
    subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command",
         "; ".join(lines)],
        capture_output=True,
    )


def _remove_shortcut(lnk_path: Path) -> None:
    try:
        lnk_path.unlink()
    except FileNotFoundError:
        pass


def _startup_enabled() -> bool:
    """True if our startup shortcut is present in the Startup folder."""
    return _STARTUP_LNK.exists()


def _set_startup(enabled: bool) -> None:
    """Create or remove the Startup-folder ("Start at login") shortcut."""
    if enabled:
        _write_shortcut(_STARTUP_LNK)
    else:
        _remove_shortcut(_STARTUP_LNK)


def _start_menu_enabled() -> bool:
    """True if the Start-menu shortcut is present."""
    return _START_MENU_LNK.exists()


def _set_start_menu(enabled: bool) -> None:
    """Create or remove the Start-menu shortcut. Toggling on regenerates a
    clean shortcut, which is how a user fixes a stale one from the dashboard."""
    if enabled:
        _write_shortcut(_START_MENU_LNK, icon=True)
    else:
        _remove_shortcut(_START_MENU_LNK)


# PowerShell cleanup that outlives this process: it waits for our PID to exit
# (so files we hold open are released), removes the Startup shortcut and the
# user-data folder, and — only when we pass an install dir (frozen builds) —
# the installed app folder itself, then deletes itself. Running as a detached
# system-powershell process means it holds no lock on anything it removes.
_UNINSTALL_PS = r"""
param([int]$ProcId, [string]$DataDir, [string]$StateRoot,
      [string]$InstallDir, [string]$StartupLnk, [string]$StartMenuLnk)
Set-Location -LiteralPath $env:TEMP
try { Wait-Process -Id $ProcId -Timeout 30 -ErrorAction SilentlyContinue } catch {}
Start-Sleep -Milliseconds 500
Remove-Item -LiteralPath $StartupLnk -Force -ErrorAction SilentlyContinue
if ($StartMenuLnk) { Remove-Item -LiteralPath $StartMenuLnk -Force -ErrorAction SilentlyContinue }
foreach ($p in @($DataDir, $StateRoot, $InstallDir)) {
  if ($p -and (Test-Path -LiteralPath $p)) {
    Remove-Item -LiteralPath $p -Recurse -Force -ErrorAction SilentlyContinue
  }
}
Remove-Item -LiteralPath $PSCommandPath -Force -ErrorAction SilentlyContinue
"""


def _spawn_uninstall_cleanup(install_dir: str | None) -> None:
    """Launch the detached cleanup script. Deletes the canonical user-data
    folder always; deletes install_dir only when given (frozen builds — never
    in a source checkout, so testing can't nuke the repo)."""
    import tempfile

    local = os.environ.get("LOCALAPPDATA") or str(Path.home())
    state_root = str(Path(local) / "ClaudeUsage")   # canonical user-data root
    data_dir = str(STATE_FILE.parent)               # honours CLAUDE_USAGE_DATA_DIR

    f = tempfile.NamedTemporaryFile(
        "w", suffix=".ps1", delete=False, encoding="utf-8")
    f.write(_UNINSTALL_PS)
    f.close()

    DETACHED_PROCESS = 0x00000008
    CREATE_NEW_PROCESS_GROUP = 0x00000200
    subprocess.Popen(
        ["powershell", "-NoProfile", "-NonInteractive",
         "-ExecutionPolicy", "Bypass", "-File", f.name,
         "-ProcId", str(os.getpid()),
         "-DataDir", data_dir,
         "-StateRoot", state_root,
         "-InstallDir", install_dir or "",
         "-StartupLnk", str(_STARTUP_LNK),
         "-StartMenuLnk", str(_START_MENU_LNK)],
        creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
        close_fds=True,
        cwd=os.environ.get("TEMP", os.getcwd()),
    )


from widget_updater import (
    PROJECTS_DIR,
    SERVER_PORT,
    STATE_FILE,
    TranscriptHandler,
    _WidgetHandler,
    _poll_interval_minutes,
    _liveness_oneshot_pcts,
    _liveness_delta_pct,
    _read_widget_config_all,
    _widget_shape_path,
    _widget_show_text,
    resolve_widget_color,
)


def _load_widget_mask(widget: str, size: int) -> "Image.Image | None":
    """Load the custom shape mask for `widget` resized to `size`, or None when
    no custom shape is set (so the caller falls back to the built-in ghost).
    Failures degrade gracefully to the built-in shape rather than crash the
    icon refresh."""
    sp = _widget_shape_path(widget)
    if sp is None:
        return None
    try:
        import widget_shapes
        return widget_shapes.load_mask(sp, size)
    except Exception as e:
        print(f"[X] _load_widget_mask({widget}) failed, using built-in: {e}")
        return None

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
    except Exception as e:
        print(f"[X] _query_system_status_font failed, using Segoe UI: {type(e).__name__}: {e}")
    return "Segoe UI"


def _is_windows_11() -> bool:
    try:
        return sys.platform == "win32" and sys.getwindowsversion().build >= 22000
    except Exception as e:
        print(f"[X] _is_windows_11 check failed: {type(e).__name__}: {e}")
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
            except Exception as e:
                print(f"[X] pick_taskbar_font failed to load Segoe UI Variable ({path}): {type(e).__name__}: {e}")

    # Whatever the OS says is the status-bar font (usually Segoe UI).
    face = _query_system_status_font()
    path = (_resolve_bold_font_path if bold else _resolve_font_path)(face)
    if path:
        try:
            return ImageFont.truetype(path, size)
        except Exception as e:
            print(f"[X] pick_taskbar_font failed to load {face} ({path}): {type(e).__name__}: {e}")

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


def _hex_to_rgba(hex_color: str, alpha: int = 255) -> tuple[int, int, int, int]:
    """Convert a CSS hex colour (#rgb or #rrggbb) to an RGBA tuple.

    Falls back to opaque black on any parse error so a bad config value never
    crashes the renderer."""
    s = hex_color.strip().lstrip("#")
    try:
        if len(s) == 3:
            r = int(s[0] * 2, 16)
            g = int(s[1] * 2, 16)
            b = int(s[2] * 2, 16)
        elif len(s) == 6:
            r, g, b = int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16)
        else:
            raise ValueError(f"bad hex: #{s!r}")
    except Exception as e:
        print(f"[X] _hex_to_rgba({hex_color!r}): {e}")
        return (0, 0, 0, alpha)
    return (r, g, b, alpha)


def render_ghost(pct: float, size: int = 64,
                 base_fill=FILL_COLOR, alert_fill=ALERT_COLOR,
                 base_color: str | None = None,
                 color_stops: str | None = None,
                 fill_mode: str = "level",
                 body_mask: Image.Image | None = None,
                 show_text: bool = False,
                 label: str | None = None) -> Image.Image:
    """Render the ghost icon at the given percentage fill.

    Colour precedence: if base_color / color_stops are supplied (from the
    per-widget config), they are resolved via resolve_widget_color and take
    priority over the legacy base_fill / alert_fill RGBA tuple arguments.
    This lets the config-driven path (colour resolver) and the old direct-
    tuple path coexist during the transition — callers that already pass RGBA
    tuples keep working unchanged.

    body_mask: an optional L-mode silhouette to use INSTEAD of the built-in
    ghost body (a custom uploaded shape, already resized to `size`). The whole
    colour + fill-mode pipeline below operates on whatever mask it's given, so
    a custom shape fills and tints identically to the ghost. None = built-in.

    fill_mode:
      "level"   — existing bottom-to-top linear fill (default).
      "angular" — clockwise wedge from 12 o'clock, intersected with the body
                  mask.  Useful when you want the ghost to read like a clock.

    show_text / label: when show_text is True and label is non-empty, the label
    (typically the pct, e.g. "73%") is drawn centred on top of the filled body,
    mirroring how the clock widget shows its remaining-time number. The text
    sits above the fill so it stays legible at any fill level.
    """
    pct = max(0.0, min(100.0, float(pct or 0)))
    mask = body_mask if body_mask is not None else _body_mask(size)
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))

    ghost = Image.new("RGBA", (size, size), GHOST_COLOR)
    img.paste(ghost, mask=mask)

    # Resolve fill colour: config-driven resolver wins over legacy RGBA tuples.
    if base_color is not None:
        hex_color = resolve_widget_color(pct, base_color, color_stops)
        fill_rgba = _hex_to_rgba(hex_color)
    else:
        # Legacy path: alert_fill at 90%+, base_fill otherwise.
        fill_rgba = alert_fill if pct >= 90 else base_fill

    fill_layer = Image.new("RGBA", (size, size), fill_rgba)

    if fill_mode == "angular":
        # Angular fill: clockwise wedge from 12 o'clock (like the clock arc),
        # intersected with the body mask so only ghost pixels are coloured.
        # Uses ImageDraw.pieslice on a scratch mask then combines with the body
        # mask via ImageChops.multiply (pure PIL, no numpy needed).
        from PIL import ImageChops
        bbox = mask.getbbox()
        if bbox and pct > 0:
            wedge_mask = Image.new("L", (size, size), 0)
            d = ImageDraw.Draw(wedge_mask)
            # Centre the pie slice on the ghost body's bounding box, and make
            # the radius large enough to cover the whole body so the wedge
            # clips entirely at the mask boundary, not at the circle edge.
            cx = (bbox[0] + bbox[2]) / 2
            cy = (bbox[1] + bbox[3]) / 2
            r  = max(bbox[2] - bbox[0], bbox[3] - bbox[1]) / 2
            pie_box = [cx - r, cy - r, cx + r, cy + r]
            end_angle = -90 + 360 * (pct / 100)
            if pct >= 99.99:
                # Full circle: avoids floating-point gap at exactly 360°.
                d.pieslice(pie_box, start=-90, end=270, fill=255)
            else:
                d.pieslice(pie_box, start=-90, end=end_angle, fill=255)
            # AND the wedge with the body mask (ImageChops.multiply = min on L).
            combined_mask = ImageChops.multiply(wedge_mask, mask)
            img.paste(fill_layer, mask=combined_mask)
    else:
        # Level fill: existing bottom-to-top clip on the ghost body.
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

    # Optional centred label (e.g. "73%") on top of the fill. White + bold so it
    # reads against both the unfilled ghost body and the coloured fill, matching
    # the clock widget's number styling.
    if show_text and label:
        _draw_centered_text(ImageDraw.Draw(img), label, size,
                            (255, 255, 255, 255), bold=True)
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
                          size: int = 64,
                          body_mask: Image.Image | None = None,
                          show_text: bool = True) -> Image.Image:
    """Pie-arc background + remaining-time label.

    The arc represents *elapsed* time: it starts empty and fills clockwise
    until the session resets. So a glance at the icon tells you how close
    you are to a fresh window.

    Arc colour is sourced from the 'clock' widget config (base_color /
    color_stops). The colour resolver treats elapsed-time % as the pct driver,
    matching what updateArc() in widget.html does — so both renderers agree.

    body_mask: when a custom shape is set for the clock, the elapsed-time wedge
    is intersected with that silhouette (via render_ghost's angular path) so the
    clock fills a custom shape instead of a circle. None = built-in circle arc.

    show_text: when False, the reset-time number is omitted (the user wants a
    bare shape). The standalone session%/weekly% text icons are unaffected.
    """
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d   = ImageDraw.Draw(img)

    # Geometry: a full-bleed circle with a tiny margin so the edges don't
    # clip against the tray slot's bounds.
    margin = max(1, size // 32)
    box = (margin, margin, size - 1 - margin, size - 1 - margin)

    def _empty(label: str) -> Image.Image:
        # Neutral "no live session" rendering, shared by the no-data and
        # already-elapsed cases.
        if body_mask is None:
            d.ellipse(box, outline=ARC_TRACK_COLOR, width=max(1, size // 24))
        else:
            track = Image.new("RGBA", (size, size), ARC_TRACK_COLOR)
            img.paste(track, mask=body_mask)
        if show_text:
            _draw_centered_text(d, label, size, (200, 200, 200, 255))
        return img

    # Compute progress and remaining seconds. When data is missing, render
    # an empty track + a dash so the user knows the icon is alive but idle.
    if not session_start or not session_end:
        return _empty("--")

    if isinstance(session_start, str):
        session_start = datetime.fromisoformat(session_start)
    if isinstance(session_end, str):
        session_end = datetime.fromisoformat(session_end)

    now   = datetime.now(timezone.utc)
    # Once the window has elapsed there's no live session to count down, so
    # fall back to the same neutral empty-track + dash as the no-data case
    # rather than a misleading full arc reading "0".
    if now >= session_end:
        return _empty("--")

    total = (session_end - session_start).total_seconds()
    used  = (now - session_start).total_seconds()
    remaining = max(0.0, total - used)
    progress  = 0.0 if total <= 0 else max(0.0, min(1.0, used / total))

    # Resolve arc colour from config. The clock uses elapsed % (progress*100) as
    # the pct driver so the alert threshold in color_stops (e.g. "90:#D64E2A")
    # triggers at 90% elapsed — matching the HTML's remaining<=10% condition.
    from widget_updater import _widget_config
    cc = _widget_config("clock")
    fill_mode   = cc.get("fill_mode", "angular")
    elapsed_pct = progress * 100
    hex_color   = resolve_widget_color(elapsed_pct, cc.get("base_color", "#2A78D6"),
                                       cc.get("color_stops"))
    arc_rgba    = _hex_to_rgba(hex_color)

    if body_mask is not None:
        # Custom shape: reuse render_ghost's pipeline so the elapsed-time fill is
        # clipped to the silhouette, identical to the ghost widgets. elapsed_pct
        # is the fill driver (matching the HTML) and the clock's own fill_mode
        # (level or angular) is honoured.
        img = render_ghost(elapsed_pct, size,
                           base_color=cc.get("base_color", "#2A78D6"),
                           color_stops=cc.get("color_stops"),
                           fill_mode=fill_mode, body_mask=body_mask)
        if show_text:
            _draw_centered_text(ImageDraw.Draw(img),
                                _fmt_remaining_label(remaining), size,
                                (255, 255, 255, 255), bold=True)
        return img

    # Default circle. The dim track shows the "empty" portion; the coloured fill
    # grows with elapsed time. Angular = clockwise pie wedge (clock-face look);
    # level = bottom-to-top fill of the disc (matches the ghost widgets).
    d.ellipse(box, fill=ARC_TRACK_COLOR)
    if progress > 0 and fill_mode == "level":
        # Build a disc mask, then crop it from the fill line down and paste the
        # colour through it — same level technique as render_ghost.
        disc = Image.new("L", (size, size), 0)
        ImageDraw.Draw(disc).ellipse(box, fill=255)
        dbbox = disc.getbbox()
        if dbbox:
            top, bottom = dbbox[1], dbbox[3]
            cut_top = int(round(bottom - (bottom - top) * progress))
            cut_top = max(top, min(bottom, cut_top))
            if cut_top < bottom:
                fill_layer  = Image.new("RGBA", (size, size), arc_rgba)
                bottom_mask = Image.new("L", (size, size), 0)
                bottom_mask.paste(disc.crop((0, cut_top, size, size)), (0, cut_top))
                img.paste(fill_layer, mask=bottom_mask)
    elif progress > 0:
        # Angular pie slice. pieslice angle 0 = east, so -90 starts at 12 o'clock.
        d.pieslice(
            box,
            start=-90,
            end=-90 + 360 * progress,
            fill=arc_rgba,
        )

    # Label on top. White looks crisp on both the blue arc and the dim track.
    # Bold so the number is legible at small tray-icon sizes.
    if show_text:
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

def _fmt_short(end, weekly: bool = False) -> str:
    """Format time remaining for tooltip display.

    Session (weekly=False):
    - Under 1 hour: "in 34m"
    - 1 hour+: "in 2h 41m" (minutes dropped if exactly on hour)

    Weekly (weekly=True):
    - Under 24 hours: "in 2h 41m"
    - 24 hours+: "in Nd Xh"
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
    day_threshold = 24 if weekly else 48
    if hrs < day_threshold:
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
    except Exception as e:
        print(f"[X] _load_prefs failed, using defaults: {type(e).__name__}: {e}")
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
    ("show_session_reset", "claude-usage-session-reset", "Session clock"),
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
        return pystray.Menu(
            pystray.MenuItem(lambda _: self._ghost_tooltip(), None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Open dashboard", self._open_dashboard, default=True),
            pystray.MenuItem("Confirm usage %", self._confirm_usage),
            pystray.MenuItem("Restart widget", self._restart),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", self._quit),
        )

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
            # Prefer config-driven colour resolver; fall back to hardcoded RGBA
            # for back-compat when config is absent.
            from widget_updater import _widget_config
            sc = _widget_config("session")
            return render_ghost(s_pct, ICON_SIZE,
                                base_color=sc.get("base_color"),
                                color_stops=sc.get("color_stops"),
                                fill_mode=sc.get("fill_mode", "level"),
                                body_mask=_load_widget_mask("session", ICON_SIZE),
                                show_text=_widget_show_text("session"),
                                label=_fmt_pct(self._state["session_pct"]))
        if pref_key == "show_weekly_ghost":
            from widget_updater import _widget_config
            wc = _widget_config("weekly")
            return render_ghost(w_pct, ICON_SIZE,
                                base_color=wc.get("base_color"),
                                color_stops=wc.get("color_stops"),
                                fill_mode=wc.get("fill_mode", "level"),
                                body_mask=_load_widget_mask("weekly", ICON_SIZE),
                                show_text=_widget_show_text("weekly"),
                                label=_fmt_pct(self._state["weekly_pct"]))
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
                body_mask=_load_widget_mask("clock", ICON_SIZE),
                show_text=_widget_show_text("clock"),
            )
        raise KeyError(pref_key)

    def _title_for(self, pref_key: str) -> str:
        if not self._status_ok():
            return f"Claude Usage: {self._status_tooltip()}"
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
        menu = self._build_menu()
        for pref_key, icon in self.icons.items():
            try:
                icon.icon  = self._render_icon_for(pref_key)
                icon.title = self._title_for(pref_key)
                icon.menu  = menu
            except Exception as e:
                print(f"  {pref_key} refresh error: {e}")

    def _ghost_tooltip(self) -> str:
        s = _fmt_pct(self._state["session_pct"])
        w = _fmt_pct(self._state["weekly_pct"])
        s_when = _fmt_short(self._state["session_end"])
        w_when = _fmt_short(self._state["weekly_end"], weekly=True)
        # Two-line tooltip - Windows renders this in the system font.
        return f"Session: {s} {s_when}  \nWeekly: {w} {w_when}".strip()

    # ------ restart prompt + snooze ------------------------------------

    def _restart_prompts_snoozed(self) -> bool:
        until = self._prefs.get("restart_prompt_snoozed_until")
        if not until:
            return False
        try:
            return datetime.fromisoformat(until) > datetime.now(timezone.utc)
        except Exception as e:
            print(f"[X] _restart_prompts_snoozed: bad timestamp {until!r}: {type(e).__name__}: {e}")
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

    def web_toggle(self, key: str) -> None:
        """Toggle a pref key or start_at_login, called from the HTTP handler."""
        if key == "start_at_login":
            _set_startup(not _startup_enabled())
            return
        if key == "start_menu_shortcut":
            _set_start_menu(not _start_menu_enabled())
            return
        if key not in self._prefs:
            return
        currently_on = self._prefs[key]
        if currently_on and self._visible_count() <= 1:
            return  # can't hide last visible icon
        self._prefs[key] = not currently_on
        _save_prefs(self._prefs)
        self._apply_visibility()
        self._refresh_all()

    def web_action(self, name: str) -> None:
        """Run a named action, called from the HTTP handler (any thread)."""
        if name == "confirm" and self.refresh_callback:
            threading.Thread(target=self.refresh_callback, daemon=True).start()
        elif name == "open_config":
            self._open_config_folder()
        elif name == "restart":
            self._restart(None, None)
        elif name == "quit":
            self._quit(None, None)
        elif name == "uninstall":
            self._uninstall()

    def _open_config_folder(self) -> None:
        """Open the per-user config/log folder (where config.json and the run
        logs live) in Explorer. A safe, read-only-by-default action so users
        can find/edit settings the dashboard doesn't expose yet."""
        folder = STATE_FILE.parent
        try:
            folder.mkdir(parents=True, exist_ok=True)
            os.startfile(str(folder))  # noqa: S606 - Windows-only, fixed path
        except Exception as e:
            print(f"[X] open config folder failed: {type(e).__name__}: {e}")

    # ------ misc --------------------------------------------------------

    def _open_dashboard(self, _i, _it):
        webbrowser.open(f"http://127.0.0.1:{SERVER_PORT}/")

    def _uninstall(self):
        """Full uninstall: drop the Startup entry, hand the rest to a detached
        cleanup script (it waits for us to exit, then removes user data and —
        when frozen — the installed app folder), then quit. A source checkout
        passes no install dir, so the repo is never deleted."""
        try:
            _set_startup(False)
        except Exception as e:
            print(f"[X] uninstall: removing startup entry failed: "
                  f"{type(e).__name__}: {e}")
        frozen = getattr(sys, "frozen", False)
        install_dir = os.path.dirname(sys.executable) if frozen else None
        try:
            _spawn_uninstall_cleanup(install_dir)
        except Exception as e:
            print(f"[X] uninstall: launching cleanup failed: "
                  f"{type(e).__name__}: {e}")
        self._quit(None, None)

    def _quit(self, _icon, _it):
        self._stopping = True
        for icon in self.icons.values():
            try: icon.stop()
            except Exception as e: print(f"[X] icon.stop() failed during quit: {type(e).__name__}: {e}")

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
# Single-instance guard.
# ---------------------------------------------------------------------------
# A named mutex is the canonical Windows mechanism. We deliberately do NOT key
# off the HTTP port: on Windows SO_REUSEADDR lets two processes bind the same
# 127.0.0.1 port without error, so a slow Startup-folder auto-start racing a
# manual Start-menu launch silently stacks a *second* visible widget instead of
# failing. The mutex detects the existing instance so the latecomer exits
# quietly.
_ERROR_ALREADY_EXISTS = 183
_single_instance_handle = None  # held open for the process lifetime


def _acquire_single_instance() -> bool:
    """True if we are the only instance; False if one is already running.

    On success the mutex handle is parked in a module global so it stays open
    (and the named object stays alive) for as long as the process runs. Any
    failure to probe fails *open* - we'd rather risk a rare duplicate than
    block the widget from ever starting in an odd environment.
    """
    global _single_instance_handle
    try:
        kernel32 = ctypes.windll.kernel32
        kernel32.CreateMutexW.restype  = wintypes.HANDLE
        kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL,
                                          wintypes.LPCWSTR]
        # Plain (no-backslash) name => session-local: one widget per interactive
        # logon session, which is exactly the granularity we want.
        handle = kernel32.CreateMutexW(None, False,
                                       "ClaudeUsageWidget_SingleInstance")
        last_error = kernel32.GetLastError()
        if not handle:
            return True
        if last_error == _ERROR_ALREADY_EXISTS:
            return False
        _single_instance_handle = handle
        return True
    except Exception as e:
        print(f"[X] single-instance check failed, starting anyway: "
              f"{type(e).__name__}: {e}")
        return True


def _notify_already_running() -> None:
    """Pop a message box when a second launch is blocked, instead of exiting
    silently. Someone who has lost track of the running widget (e.g. its tray
    icons are hidden in the overflow flyout) and relaunches would otherwise see
    nothing happen and assume it's broken. We tell them it's already running and
    offer to open its dashboard so they can find/manage it."""
    MB_YESNO           = 0x0004
    MB_ICONINFORMATION = 0x0040
    MB_SETFOREGROUND   = 0x10000
    MB_TOPMOST         = 0x40000
    IDYES              = 6
    try:
        resp = ctypes.windll.user32.MessageBoxW(
            0,
            "Claude Usage is already running.\n\n"
            "Its icons live in the system tray - check the up-arrow "
            "'hidden icons' flyout. Open its dashboard now?",
            "Claude Usage",
            MB_YESNO | MB_ICONINFORMATION | MB_SETFOREGROUND | MB_TOPMOST,
        )
        if resp == IDYES:
            webbrowser.open(f"http://127.0.0.1:{SERVER_PORT}/")
    except Exception as e:
        print(f"[X] already-running notification failed: "
              f"{type(e).__name__}: {e}")


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

    if not _acquire_single_instance():
        print("Another Claude Usage widget is already running; exiting.")
        _notify_already_running()
        sys.exit(0)

    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "ClaudeUsage.Widget"
        )
    except Exception as e:
        print(f"[X] SetCurrentProcessExplicitAppUserModelID failed: {type(e).__name__}: {e}")

    tray = TrayApp()
    _WidgetHandler._prefs_getter    = lambda: {
        **tray._prefs,
        "start_at_login": _startup_enabled(),
        "start_menu_shortcut": _start_menu_enabled(),
        "poll_interval_minutes": _poll_interval_minutes(),
        "liveness_oneshot_pcts": sorted(_liveness_oneshot_pcts()),
        "liveness_delta_pct": _liveness_delta_pct(),
        # Per-widget colour/fill config for the Appearance section in the
        # dashboard.  Merged defaults+config so the JS can always read complete
        # objects even when the user has only set one or two fields.
        "widgets": _read_widget_config_all(),
    }
    _WidgetHandler._toggle_callback = tray.web_toggle
    _WidgetHandler._action_callback = tray.web_action

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
    handler.start_watchdog()
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
