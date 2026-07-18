"""title_probe.py — log the leading state-glyph(s) of every Claude-ish terminal
window once per second, to learn the title vocabulary for turn-state
(processing / idle / awaiting-permission / awaiting-question).

Claude Code (and the user's custom title-setter) encode live turn state in the
characters BEFORE the title's first letter (e.g. a cycling braille spinner while
processing). We record exactly that prefix — everything up to the first
alphabetic char — per window, once a second, flushing to disk ~once a minute.

Cost is trivial: one EnumWindows + a few GetWindowText per second, and we only
WRITE a row when a window's prefix changes (plus a periodic heartbeat), so the
file stays tiny (~tens of KB over a full day).

Run:  python analysis/title_probe.py [outfile]
Stop: Ctrl-C (flushes on exit).
Output: JSONL, one row per (window, prefix-change), fields:
  ts   ISO local time
  hwnd window handle (hex)
  pre  the prefix (everything before the first letter), repr-safe
  full the full title (only on change, for context)
"""
import io
import json
import sys
import time
from datetime import datetime

import win32gui

OUT = sys.argv[1] if len(sys.argv) > 1 else "analysis/title_log.jsonl"
POLL_S = 1.0
FLUSH_S = 60.0
# Heartbeat: even with no change, emit a row this often so we can tell the probe
# was alive (and distinguish "no change" from "probe died") without bloating.
HEARTBEAT_S = 300.0


def first_letter_idx(s: str) -> int:
    for i, ch in enumerate(s):
        if ch.isalpha():
            return i
    return len(s)


def is_spinner_active(title: str) -> bool:
    """True if the title's first non-space char is a Braille glyph (U+2800-28FF).
    Claude Code animates its 'processing' spinner through the Braille block, so
    the positive presence of a Braille char at the front means a turn is running.
    Idle = no such window (we key on the active signal, not absence of ✳)."""
    for ch in title:
        if ch == " ":
            continue
        return 0x2800 <= ord(ch) <= 0x28FF
    return False


def claude_windows():
    """Visible windows whose title looks like a Claude Code terminal.
    Match on the trailing marker / known substrings, plus the WindowsTerminal
    host class, so we catch both native and custom-title-setter windows."""
    acc = []

    def cb(h, _):
        if not win32gui.IsWindowVisible(h):
            return
        t = win32gui.GetWindowText(h)
        if not t:
            return
        cls = win32gui.GetClassName(h)
        is_term = "CASCADIA" in cls or "WindowsTerminal" in cls
        # Only terminal-host windows; an Explorer/editor window whose PATH
        # happens to contain "Claude" must not match (class gate, not title).
        # Allow non-terminal hosts ONLY if they carry the marker glyph itself.
        if is_term or "✳" in t:
            acc.append((h, t))

    win32gui.EnumWindows(cb, None)
    return acc


def main():
    out = io.open(OUT, "a", encoding="utf-8")
    last_pre = {}        # hwnd -> last prefix seen
    last_emit = {}       # hwnd -> monotonic time of last row emitted
    last_flush = time.monotonic()
    print(f"title_probe -> {OUT} (poll {POLL_S}s, flush {FLUSH_S}s). Ctrl-C to stop.")
    try:
        while True:
            now_mono = time.monotonic()
            ts = datetime.now().isoformat(timespec="seconds")
            seen = set()
            for h, full in claude_windows():
                seen.add(h)
                pre = full[: first_letter_idx(full)]
                changed = last_pre.get(h) != pre
                stale = (now_mono - last_emit.get(h, -1e9)) >= HEARTBEAT_S
                if changed or stale:
                    # full title on every row so each window is self-identifying
                    # (heartbeats included) — makes multi-window streams readable.
                    row = {"ts": ts, "hwnd": hex(h), "pre": pre, "full": full}
                    out.write(json.dumps(row, ensure_ascii=False) + "\n")
                    last_pre[h] = pre
                    last_emit[h] = now_mono
            # Note windows that disappeared (turn ended + window closed, etc.)
            for h in list(last_pre):
                if h not in seen:
                    out.write(json.dumps(
                        {"ts": ts, "hwnd": hex(h), "pre": None, "gone": True},
                        ensure_ascii=False) + "\n")
                    del last_pre[h]
                    last_emit.pop(h, None)
            if now_mono - last_flush >= FLUSH_S:
                out.flush()
                last_flush = now_mono
            time.sleep(POLL_S)
    except KeyboardInterrupt:
        pass
    finally:
        out.flush()
        out.close()
        print(f"\nstopped. log at {OUT}")


if __name__ == "__main__":
    main()
