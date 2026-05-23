#!/usr/bin/env python3
"""
Widget updater — watches ~/.claude/projects/ for JSONL changes, maintains
widget_state.json with current session token counts, and calibrates the
token-to-utilisation relationship by calling the Claude.ai API on startup
and on the first two file-change events each session.

Run once; leave it in the background.
"""

import json
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import browser_cookie3
from curl_cffi import requests as cffi_requests
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

WIDGET_HTML = Path(__file__).parent / "widget.html"
SERVER_PORT = 7433

PROJECTS_DIR     = Path.home() / ".claude" / "projects"
STATE_FILE        = Path(__file__).parent / "usage_data" / "widget_state.json"
CALIBRATION_FILE  = Path(__file__).parent / "usage_data" / "calibration.jsonl"
DISCREPANCY_FILE  = Path(__file__).parent / "usage_data" / "discrepancies.jsonl"
# Min absolute pp difference between widget's displayed pct and a fresh API
# reading before we consider it worth logging. Set tight (>1pp) so we
# capture drift bugs early; the log is silent so noise has no UX cost.
DISCREPANCY_THRESHOLD_PP = 1.0
# When the API confirms our display is off by this much, we don't just log
# it - we assume the running widget is stuck and offer the user a restart.
LARGE_DISCREPANCY_PP = 10.0
# After this many consecutive API failures, treat the widget as
# disconnected and offer a restart. Two in a row means it's not a one-off
# transient.
DISCONNECT_FAIL_THRESHOLD = 2

def _load_org_id() -> str:
    env = os.environ.get("CLAUDE_ORG_ID")
    if env:
        return env
    cfg = Path(__file__).parent / "config.json"
    if cfg.exists():
        return json.loads(cfg.read_text(encoding="utf-8"))["org_id"]
    raise SystemExit(
        "Claude organization ID not configured. Set CLAUDE_ORG_ID or create "
        "config.json next to this script (see config.example.json)."
    )


USAGE_URL     = f"https://claude.ai/api/organizations/{_load_org_id()}/usage"
SESSION_HOURS = 5
# Call API on startup + this many subsequent file-change events per session.
CALIBRATION_CALLS_PER_SESSION = 2
# Re-calibrate if this many seconds have passed since the last API call.
CALIBRATION_MAX_AGE_SECS = 3600


# ---------------------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------------------

def _fetch_usage() -> dict | None:
    try:
        cookies = browser_cookie3.firefox(domain_name=".claude.ai")
        cookie_dict = {c.name: c.value for c in cookies}
        r = cffi_requests.get(
            USAGE_URL, cookies=cookie_dict,
            impersonate="firefox", headers={"Referer": "https://claude.ai/"},
            timeout=10,
        )
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"  API error: {e}")
        return None


def _parse_session(raw: dict) -> tuple[datetime | None, datetime | None, float | None]:
    """Returns (session_start, session_end, utilisation_pct) or (None, None, None)."""
    five_hour = (raw or {}).get("five_hour") or {}
    resets_at_str = five_hour.get("resets_at")
    pct = five_hour.get("utilization")
    if not resets_at_str or pct is None:
        return None, None, None
    session_end   = datetime.fromisoformat(resets_at_str)
    session_start = session_end - timedelta(hours=SESSION_HOURS)
    return session_start, session_end, pct


def _parse_weekly(raw: dict) -> tuple[float | None, datetime | None]:
    """Returns (weekly_pct, weekly_reset) from the seven_day bucket."""
    seven = (raw or {}).get("seven_day") or {}
    pct = seven.get("utilization")
    resets_at = seven.get("resets_at")
    if pct is None:
        return None, None
    end = datetime.fromisoformat(resets_at) if resets_at else None
    return pct, end


# ---------------------------------------------------------------------------
# State persistence
# ---------------------------------------------------------------------------

def _load_state() -> dict:
    try:
        s = json.loads(STATE_FILE.read_text())
        s["seen_ids"] = set(s.get("seen_ids", []))
        return s
    except Exception:
        return _empty_state()


def _empty_state(session_start: datetime | None = None) -> dict:
    return {
        "seen_ids": set(),
        "input_tokens": 0,
        "output_tokens": 0,
        "by_model": {},
        "session_start": session_start.isoformat() if session_start else None,
        "calibration_calls_remaining": CALIBRATION_CALLS_PER_SESSION,
    }


def _save_state(state: dict, session_pct: float | None = None,
                session_end: datetime | None = None,
                weekly_pct: float | None = None,
                weekly_end: datetime | None = None) -> None:
    out = {
        **state,
        "seen_ids": list(state["seen_ids"]),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    if session_pct is not None:
        out["session_pct"] = session_pct
    if session_end is not None:
        out["session_end"] = session_end.isoformat()
    if weekly_pct is not None:
        out["weekly_pct"] = weekly_pct
    if weekly_end is not None:
        out["weekly_end"] = weekly_end.isoformat()
    STATE_FILE.write_text(json.dumps(out, indent=2))


def _log_discrepancy(kind: str, stored: float | None, api: float,
                     last_calibrated: datetime | None, state: dict,
                     scraped_at: datetime) -> None:
    """Quietly record a stored-vs-API mismatch for later inspection.

    `kind` is "session" or "weekly". `stored` is what the widget was about to
    display (None if we had no prior reading). The threshold check is done
    by the caller - this function unconditionally writes.
    """
    age = ((scraped_at - last_calibrated).total_seconds()
           if last_calibrated else None)
    record = {
        "scraped_at":              scraped_at.isoformat(),
        "kind":                    kind,
        "stored_pct":              stored,
        "api_pct":                 api,
        "diff_pp":                 None if stored is None else round(api - stored, 2),
        "seconds_since_calibration": age,
        "session_start":           state.get("session_start"),
        "transcript_io_total":     state.get("input_tokens", 0)
                                   + state.get("output_tokens", 0),
    }
    DISCREPANCY_FILE.parent.mkdir(parents=True, exist_ok=True)
    with DISCREPANCY_FILE.open("a") as f:
        f.write(json.dumps(record) + "\n")


def _check_discrepancy(kind: str, stored: float | None, api: float | None,
                       last_calibrated: datetime | None, state: dict,
                       scraped_at: datetime) -> None:
    """Log if the freshly-fetched API value differs from what we were showing.

    Skips when we have no prior value to compare against (first-ever fetch)
    or when the API didn't return a number.
    """
    if api is None or stored is None:
        return
    if abs(api - stored) > DISCREPANCY_THRESHOLD_PP:
        _log_discrepancy(kind, stored, api, last_calibrated, state, scraped_at)


def _append_calibration(state: dict, pct: float, scraped_at: datetime) -> None:
    total_io = state["input_tokens"] + state["output_tokens"]
    implied  = round(total_io / (pct / 100)) if pct > 0 else None
    record   = {
        "scraped_at":              scraped_at.isoformat(),
        "session_pct":             pct,
        "session_start":           state.get("session_start"),
        "transcript_input_tokens": state["input_tokens"],
        "transcript_output_tokens":state["output_tokens"],
        "transcript_io_total":     total_io,
        "implied_session_budget":  implied,
        "by_model":                state["by_model"],
        "source":                  "widget",
    }
    CALIBRATION_FILE.parent.mkdir(parents=True, exist_ok=True)
    with CALIBRATION_FILE.open("a") as f:
        f.write(json.dumps(record) + "\n")
    print(f"  calibration: {pct}% = {total_io} tokens => budget ~{implied}")


# ---------------------------------------------------------------------------
# Token counting
# ---------------------------------------------------------------------------

def process_file(path: Path, state: dict, session_start: datetime, session_end: datetime) -> bool:
    changed = False
    try:
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            obj = json.loads(line)
            if obj.get("type") != "assistant":
                continue
            msg   = obj.get("message", {})
            usage = msg.get("usage")
            mid   = msg.get("id")
            if not usage or not mid or mid in state["seen_ids"]:
                continue
            ts = datetime.fromisoformat(obj["timestamp"].replace("Z", "+00:00"))
            if not (session_start <= ts <= session_end):
                continue
            state["seen_ids"].add(mid)
            model = msg.get("model", "unknown")
            entry = state["by_model"].setdefault(model, {"input": 0, "output": 0})
            entry["input"]          += usage.get("input_tokens", 0)
            entry["output"]         += usage.get("output_tokens", 0)
            state["input_tokens"]   += usage.get("input_tokens", 0)
            state["output_tokens"]  += usage.get("output_tokens", 0)
            changed = True
    except Exception:
        pass
    return changed


def full_scan(state: dict, session_start: datetime, session_end: datetime) -> None:
    for f in PROJECTS_DIR.rglob("*.jsonl"):
        mtime = datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc)
        if mtime >= session_start:
            process_file(f, state, session_start, session_end)


# ---------------------------------------------------------------------------
# Watcher
# ---------------------------------------------------------------------------

class TranscriptHandler(FileSystemEventHandler):
    def __init__(self, on_state_change=None, on_disconnect=None):
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        self.state            = _load_state()
        self.session_start    = None
        self.session_end      = None
        self.session_pct      = None
        self.weekly_pct       = None
        self.weekly_end       = None
        self.last_calibrated  = None
        # Consecutive API failures since the last success. Reset to 0 on
        # any successful fetch.
        self._api_failures    = 0
        # Callback invoked after any state update. Receives a dict so we can
        # add fields without breaking callers; today we send session + weekly.
        self.on_state_change  = on_state_change
        # Callback invoked when we suspect the widget is out of sync with
        # reality (repeated API failures, or a confirmed large discrepancy).
        # Receives a short reason string for the toast.
        self.on_disconnect    = on_disconnect
        # Seed from previously-saved state so the tray has data immediately
        # on startup, before the first API call returns.
        self.session_pct = self.state.get("session_pct")
        if self.state.get("session_start"):
            self.session_start = datetime.fromisoformat(self.state["session_start"])
        if self.state.get("session_end"):
            self.session_end = datetime.fromisoformat(self.state["session_end"])
        self.weekly_pct = self.state.get("weekly_pct")
        if self.state.get("weekly_end"):
            self.weekly_end = datetime.fromisoformat(self.state["weekly_end"])
        self._startup()

    def _fetch_with_tracking(self) -> dict | None:
        """Wraps _fetch_usage to track consecutive failures. Fires the
        disconnect callback when failures hit DISCONNECT_FAIL_THRESHOLD and
        we haven't successfully calibrated in over an hour - the joint
        condition keeps a brief connectivity blip from spamming toasts."""
        raw = _fetch_usage()
        if raw is None:
            self._api_failures += 1
            stale = (
                self.last_calibrated is None
                or (datetime.now(timezone.utc) - self.last_calibrated).total_seconds()
                   > CALIBRATION_MAX_AGE_SECS
            )
            if self._api_failures >= DISCONNECT_FAIL_THRESHOLD and stale and self.on_disconnect:
                try:
                    self.on_disconnect(
                        f"Lost contact with claude.ai ({self._api_failures} fetches failed)."
                    )
                except Exception as e:
                    print(f"  on_disconnect callback error: {e}")
        else:
            self._api_failures = 0
        return raw

    def _check_and_maybe_disconnect(self, kind: str, stored: float | None,
                                    api: float | None,
                                    scraped_at: datetime) -> None:
        """Log a discrepancy (always >1pp) and, if it's large enough, also
        fire the disconnect callback so the tray can prompt a restart."""
        _check_discrepancy(kind, stored, api, self.last_calibrated, self.state, scraped_at)
        if (stored is not None and api is not None
                and abs(api - stored) >= LARGE_DISCREPANCY_PP
                and self.on_disconnect):
            try:
                self.on_disconnect(
                    f"Widget {kind} was {round(stored)}%, API says {round(api)}% - likely stuck."
                )
            except Exception as e:
                print(f"  on_disconnect callback error: {e}")

    def _notify(self):
        if self.on_state_change:
            try:
                self.on_state_change({
                    "session_start": self.session_start,
                    "session_pct":   self.session_pct,
                    "session_end":   self.session_end,
                    "weekly_pct":    self.weekly_pct,
                    "weekly_end":    self.weekly_end,
                })
            except Exception as e:
                print(f"  tray notify error: {e}")

    def _startup(self):
        print("Fetching usage from API...")
        raw = self._fetch_with_tracking()
        session_start, session_end, pct = _parse_session(raw)
        wk_pct, wk_end = _parse_weekly(raw)
        # Compare against what we'd been displaying (loaded from disk in
        # __init__) - catches the case where the widget restarts and the
        # saved state was stale.
        scraped_at = datetime.now(timezone.utc)
        self._check_and_maybe_disconnect("session", self.session_pct, pct, scraped_at)
        self._check_and_maybe_disconnect("weekly",  self.weekly_pct, wk_pct, scraped_at)
        if wk_pct is not None:
            self.weekly_pct = wk_pct
            self.weekly_end = wk_end

        if session_start is None:
            print("  No active session (utilisation=0 or API unavailable).")
            _save_state(self.state, weekly_pct=self.weekly_pct, weekly_end=self.weekly_end)
            self._notify()
            return

        stored_start = self.state.get("session_start")
        if stored_start != session_start.isoformat():
            print(f"  New session detected, resetting state.")
            self.state = _empty_state(session_start)
            full_scan(self.state, session_start, session_end)

        self.session_start = session_start
        self.session_end   = session_end
        self.session_pct   = pct

        self.last_calibrated = datetime.now(timezone.utc)
        _append_calibration(self.state, pct, self.last_calibrated)
        _save_state(self.state, pct, session_end, self.weekly_pct, self.weekly_end)
        print(f"  Session {pct}% | weekly {self.weekly_pct}% | tokens in+out: "
              f"{self.state['input_tokens'] + self.state['output_tokens']}")
        self._notify()

    def _maybe_calibrate(self) -> None:
        now = datetime.now(timezone.utc)
        calls_left = self.state.get("calibration_calls_remaining", 0)
        age = (now - self.last_calibrated).total_seconds() if self.last_calibrated else float("inf")
        if calls_left <= 0 and age < CALIBRATION_MAX_AGE_SECS:
            return
        print("Fetching usage from API (calibration)...")
        raw = self._fetch_with_tracking()
        _, _, pct = _parse_session(raw)
        wk_pct, wk_end = _parse_weekly(raw)
        self._check_and_maybe_disconnect("session", self.session_pct, pct, now)
        self._check_and_maybe_disconnect("weekly",  self.weekly_pct, wk_pct, now)
        if wk_pct is not None:
            self.weekly_pct = wk_pct
            self.weekly_end = wk_end
        if pct is None:
            return
        self.session_pct = pct
        self.last_calibrated = now
        _append_calibration(self.state, pct, now)
        if calls_left > 0:
            self.state["calibration_calls_remaining"] -= 1

    def on_modified(self, event):
        if event.is_directory or not event.src_path.endswith(".jsonl"):
            return
        now = datetime.now(timezone.utc)
        if self.session_start is None or (self.session_end and now > self.session_end):
            self._startup()
            return

        changed = process_file(
            Path(event.src_path), self.state,
            self.session_start, datetime.now(timezone.utc),
        )
        if changed:
            self._maybe_calibrate()
            _save_state(self.state, self.session_pct, self.session_end,
                        self.weekly_pct, self.weekly_end)
            print(f"[{datetime.now().strftime('%H:%M:%S')}] "
                  f"in={self.state['input_tokens']} "
                  f"out={self.state['output_tokens']} "
                  f"pct={self.session_pct}")
            self._notify()

    on_created = on_modified

    def force_refresh(self) -> bool:
        """Hit the API immediately and update state. Returns True on success.

        Used by (a) the tray's hourly ticker so utilisation stays current
        while the user is idle, and (b) the right-click "Confirm usage %"
        menu item so they can re-verify on demand.
        """
        print("Fetching usage from API (forced)...")
        raw = self._fetch_with_tracking()
        if raw is None:
            return False
        session_start, session_end, pct = _parse_session(raw)
        wk_pct, wk_end = _parse_weekly(raw)
        scraped_at = datetime.now(timezone.utc)
        self._check_and_maybe_disconnect("session", self.session_pct, pct, scraped_at)
        self._check_and_maybe_disconnect("weekly",  self.weekly_pct, wk_pct, scraped_at)
        if wk_pct is not None:
            self.weekly_pct = wk_pct
            self.weekly_end = wk_end
        if pct is not None:
            self.session_pct = pct
        if session_start is not None:
            stored_start = self.state.get("session_start")
            if stored_start != session_start.isoformat():
                self.state = _empty_state(session_start)
                full_scan(self.state, session_start, session_end)
            self.session_start = session_start
            self.session_end = session_end
        self.last_calibrated = datetime.now(timezone.utc)
        if pct is not None:
            _append_calibration(self.state, pct, self.last_calibrated)
        _save_state(self.state, self.session_pct, self.session_end,
                    self.weekly_pct, self.weekly_end)
        self._notify()
        return True


class _WidgetHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/state"):
            body = STATE_FILE.read_bytes() if STATE_FILE.exists() else b"{}"
            self._respond(body, "application/json")
        else:
            body = WIDGET_HTML.read_bytes() if WIDGET_HTML.exists() else b"<h1>widget.html not found</h1>"
            self._respond(body, "text/html")

    def _respond(self, body: bytes, ctype: str):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass


def main():
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)

    server = HTTPServer(("127.0.0.1", SERVER_PORT), _WidgetHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"Widget at http://127.0.0.1:{SERVER_PORT}/")

    handler  = TranscriptHandler()
    observer = Observer()
    observer.schedule(handler, str(PROJECTS_DIR), recursive=True)
    observer.start()
    print(f"Watching {PROJECTS_DIR}")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
    observer.join()


if __name__ == "__main__":
    main()
