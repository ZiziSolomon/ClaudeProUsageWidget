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

import subprocess
import sys

import browser_cookie3
from curl_cffi import requests as cffi_requests
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

_BASE = Path(sys._MEIPASS) if getattr(sys, "frozen", False) else Path(__file__).parent
WIDGET_HTML = _BASE / "widget.html"
SERVER_PORT = 7433

PROJECTS_DIR     = Path.home() / ".claude" / "projects"


def _data_dir() -> Path:
    """Stable per-user location for runtime state (widget_state, calibration,
    discrepancies, tray_prefs).

    Deliberately *not* relative to __file__: in a PyInstaller bundle that
    resolves to dist\\ClaudeUsage\\_internal\\, which COLLECT wipes on every
    rebuild - so history was being destroyed on each build, and source vs.
    frozen runs kept separate stores. Anchoring to %LOCALAPPDATA% fixes both:
    one shared store that survives rebuilds. Override with CLAUDE_USAGE_DATA_DIR.
    """
    env = os.environ.get("CLAUDE_USAGE_DATA_DIR")
    if env:
        d = Path(env)
    else:
        base = os.environ.get("LOCALAPPDATA") or str(Path.home())
        d = Path(base) / "ClaudeUsage" / "usage_data"
    d.mkdir(parents=True, exist_ok=True)
    # One-time migration: seed from the legacy bundle-relative location so an
    # existing install keeps its calibration history. Copy only when the
    # destination file is absent, so we never clobber the live store.
    legacy = Path(__file__).parent / "usage_data"
    try:
        if legacy.exists() and legacy.resolve() != d.resolve():
            for name in ("widget_state.json", "calibration.jsonl",
                         "discrepancies.jsonl", "tray_prefs.json",
                         "session_history.jsonl"):
                src, dst = legacy / name, d / name
                if src.exists() and not dst.exists():
                    dst.write_bytes(src.read_bytes())
    except Exception as e:
        print(f"[!] legacy state migration skipped: {e}")
    return d


DATA_DIR          = _data_dir()
STATE_FILE        = DATA_DIR / "widget_state.json"
CALIBRATION_FILE  = DATA_DIR / "calibration.jsonl"
DISCREPANCY_FILE  = DATA_DIR / "discrepancies.jsonl"
CHART_FILE        = DATA_DIR / "chart_latest.png"
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

# ---------------------------------------------------------------------------
# Status contract (carried in widget_state.json and the on_state_change
# payload; the tray codes against these EXACT strings):
#   "ok"             - fetched live usage successfully
#   "no_cookie"      - no claude.ai cookies in any supported browser
#   "no_login"       - cookies present but the session is not authenticated
#   "fetch_error"    - network/HTTP/parse failure talking to claude.ai
#   "config_missing" - org_id could not be resolved (no config, no discovery)
#   "tracker_down"   - the local JSONL file watcher stopped (set by the tray)
#   "no_projects"    - the ~/.claude/projects folder is missing (set by the tray)
# ---------------------------------------------------------------------------
STATUS_OK             = "ok"
STATUS_NO_COOKIE      = "no_cookie"
STATUS_NO_LOGIN       = "no_login"
STATUS_FETCH_ERROR    = "fetch_error"
STATUS_CONFIG_MISSING = "config_missing"
STATUS_TRACKER_DOWN   = "tracker_down"
STATUS_NO_PROJECTS    = "no_projects"

_SUPPORTED_BROWSER = "firefox"


def _config_path() -> Path:
    """Preferred config location: %LOCALAPPDATA%\\ClaudeUsage\\config.json.

    Sidesteps the PyInstaller rebuild-wipe hazard - COLLECT wipes the bundle
    dir on every build, taking a bundled config.json with it. The per-user
    location survives rebuilds and is where we persist any auto-discovered
    org_id. Reads still fall back to the bundled/repo copy (see _read_config)
    so existing installs keep working."""
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(base) / "ClaudeUsage" / "config.json"


def _bundled_config_path() -> Path:
    """Legacy/bundled config next to this script (repo root, or _internal/)."""
    return Path(__file__).parent / "config.json"


def _read_config() -> dict:
    """Load config from the per-user location first, then the bundled copy.
    Returns {} when neither exists or is unreadable."""
    for p in (_config_path(), _bundled_config_path()):
        try:
            if p.exists():
                return json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"  config read error ({p}): {e}")
    return {}


def _write_org_id(org_id: str) -> None:
    """Persist a (discovered or user-chosen) org_id to the per-user config,
    merging with whatever is already there."""
    p = _config_path()
    try:
        cfg = {}
        if p.exists():
            cfg = json.loads(p.read_text(encoding="utf-8"))
        cfg["org_id"] = org_id
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        print(f"  saved org_id to {p}")
    except Exception as e:
        print(f"  could not save org_id to {p}: {e}")


def _write_config_value(key: str, value) -> None:
    """Persist a single key to the per-user config, merging with whatever is
    already there. Used by the dashboard's Settings controls (e.g. the poll
    interval). Mirrors _write_org_id's merge-and-write behaviour."""
    p = _config_path()
    try:
        cfg = {}
        if p.exists():
            cfg = json.loads(p.read_text(encoding="utf-8"))
        cfg[key] = value
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        print(f"  saved {key}={value!r} to {p}")
    except Exception as e:
        print(f"  could not save {key} to {p}: {e}")


def _configured_org_id() -> str | None:
    """org_id from env or config, WITHOUT triggering discovery. None if unset
    (import must not hard-fail on a missing org_id)."""
    env = os.environ.get("CLAUDE_ORG_ID")
    if env:
        return env
    return _read_config().get("org_id")




SESSION_HOURS = 5
# Call API on startup + this many subsequent file-change events per session.
CALIBRATION_CALLS_PER_SESSION = 2
# Re-calibrate if this many seconds have passed since the last API call.
# Calibration CORRECTS the token->utilisation estimate; it is intentionally
# infrequent (the live number drifts slowly within a session).
CALIBRATION_MAX_AGE_SECS = 3600
# Don't back-derive a budget from a live reading below this pct. The API reports
# utilisation at integer (1%) resolution, so at pct=1-2 a single 1pp of rounding
# is ±25-50% of the implied budget -- a budget locked in there is so unstable it
# drives the estimate past 100%. Below the floor we hold no budget and show
# pending ("--") rather than a wild guess. (Full fix: CALIBRATION-PLAN.md.)
CALIBRATION_PCT_FLOOR = 5
# Emergency re-anchor: if the local estimate pegs at the 100% clamp or sprints
# this many points past the last API-confirmed pct, the budget is suspect
# (locked too small early, or skewed by off-laptop usage) -- spend one API call
# to re-anchor rather than trust the runaway local number. Gated by a cooldown
# so a session stuck at the clamp can't fetch on every file event.
FORCE_RECAL_GAP_PP        = 5
FORCE_RECAL_COOLDOWN_SECS = 300
# When a freshly-fetched API pct disagrees with what we were displaying by more
# than this, the budget is wrong (not just drifting) -- re-derive it from the
# current session token count. BELOW this we adopt the API pct for display but
# leave the budget alone, so a 1pp API wobble can't thrash it. Same number as
# FORCE_RECAL_GAP_PP by design: one threshold for "the estimate and the API
# disagree enough to act."
RECAL_DISCREPANCY_PP      = 5
# When we recalibrate we actively re-scan the transcript folder rather than
# trust the running tally (which is built only from on_modified pings). A
# seen_ids-deduped re-scan recovers any tokens the watcher missed. If it
# recovers some AND live events have been silent this long, the watcher looks
# stuck. Off-laptop usage leaves NO local-disk evidence, so it can never trip
# this.
WATCHER_STUCK_SILENCE_SECS = 60
# Before actually crying "stuck", wait this long and re-check: a ping can be a
# beat behind, so if a transcript event lands in this grace window the watcher
# was alive after all and we stay quiet.
WATCHER_STUCK_RECHECK_SECS = 5
# Proactive 1-minute watchdog: walk PROJECTS_DIR transcripts and compare the
# newest mtime to last_event_at. If disk has been written more recently than
# we got an event AND that gap exceeds WATCHDOG_MTIME_GAP_SECS, the watcher
# is missing events. Trigger a rescan (which heals the count) and let the
# existing stuck-recheck logic decide whether to alarm. Closes the gap where
# reactive detection (calibration-piggybacked) never runs because nothing
# else is firing -- exactly today's "stuck at 1%" scenario.
WATCHDOG_INTERVAL_SECS = 60
WATCHDOG_MTIME_GAP_SECS = 30
# Liveness heartbeat: how often we re-poll claude.ai to refresh `status` and
# adopt the authoritative pct, INDEPENDENT of the calibration budget. Between
# polls the live number keeps moving on its own by counting tokens from local
# Claude Code transcripts, so the poll only needs to be frequent enough to catch
# a dead/disconnected link and re-anchor the estimate. Default 20 min;
# user-settable via the `poll_interval_minutes` config key or the
# CLAUDE_POLL_INTERVAL_MINUTES env var. Floored at LIVENESS_MIN_SECS so a
# misconfiguration can't hammer the endpoint.
LIVENESS_INTERVAL_SECS = 1200
LIVENESS_MIN_SECS = 120
# Fire an extra liveness poll the first time the local estimate crosses each
# of these thresholds (one-shot per session). 5% = floor crossing (budget
# calibration becomes reliable); 10% = meaningful lb evidence; 95% = cap
# warning so the user isn't surprised by a cutoff.
LIVENESS_ONE_SHOT_PCTS: frozenset[int] = frozenset({5, 10, 95})
# Also fire when the local estimate has moved this many pp since the last
# liveness poll — naturally front-loads early (fast burn) and slackens mid-
# session without any explicit schedule.
LIVENESS_PCT_DELTA_TRIGGER = 10


def _liveness_interval_secs() -> int:
    """Resolve the heartbeat poll interval in seconds. Priority: env var >
    config key > LIVENESS_INTERVAL_SECS default. Clamped to LIVENESS_MIN_SECS."""
    raw = os.environ.get("CLAUDE_POLL_INTERVAL_MINUTES")
    if raw is None:
        raw = _read_config().get("poll_interval_minutes")
    if raw is None:
        return LIVENESS_INTERVAL_SECS
    try:
        return max(LIVENESS_MIN_SECS, int(float(raw) * 60))
    except (TypeError, ValueError):
        print(f"  ignoring invalid poll_interval_minutes={raw!r}")
        return LIVENESS_INTERVAL_SECS


def _poll_interval_minutes() -> float:
    """Effective heartbeat interval in minutes, for display in the dashboard.
    Derived from _liveness_interval_secs() so the floor clamp and env/config
    resolution order are the single source of truth. Whole numbers come back
    as ints (20.0 -> 20) so the dashboard's preset highlight matches cleanly."""
    mins = _liveness_interval_secs() / 60
    return int(mins) if mins == int(mins) else round(mins, 2)


def _parse_pct_list(raw) -> frozenset[int]:
    """Parse a comma-separated string ("5,10,95") or a list into a set of ints
    clamped to 1..99. Garbage entries are dropped silently; returns empty if
    nothing valid parsed (callers fall back to the default)."""
    if isinstance(raw, str):
        parts = raw.split(",")
    elif isinstance(raw, (list, tuple)):
        parts = raw
    else:
        parts = [raw]
    out = set()
    for p in parts:
        try:
            v = int(float(str(p).strip()))
        except (TypeError, ValueError):
            continue
        if 1 <= v <= 99:
            out.add(v)
    return frozenset(out)


def _liveness_oneshot_pcts() -> frozenset[int]:
    """One-shot liveness thresholds, as percentages. Resolution order:
    env CLAUDE_LIVENESS_ONESHOT_PCTS > config liveness_oneshot_pcts > default
    (LIVENESS_ONE_SHOT_PCTS). Accepts a comma-separated string or a list. An
    empty/all-garbage value falls back to the default so the heartbeat never
    loses its safety pokes entirely."""
    raw = os.environ.get("CLAUDE_LIVENESS_ONESHOT_PCTS")
    if raw is None:
        raw = _read_config().get("liveness_oneshot_pcts")
    if raw is None:
        return LIVENESS_ONE_SHOT_PCTS
    return _parse_pct_list(raw) or LIVENESS_ONE_SHOT_PCTS


def _liveness_delta_pct() -> float:
    """Delta-trigger threshold in pp. env CLAUDE_LIVENESS_DELTA_PCT > config
    liveness_delta_pct > default (LIVENESS_PCT_DELTA_TRIGGER). Floored at 1pp
    so a misconfig can't fire a poll on essentially every tick."""
    raw = os.environ.get("CLAUDE_LIVENESS_DELTA_PCT")
    if raw is None:
        raw = _read_config().get("liveness_delta_pct")
    if raw is None:
        return LIVENESS_PCT_DELTA_TRIGGER
    try:
        return max(1.0, float(raw))
    except (TypeError, ValueError):
        print(f"  ignoring invalid liveness_delta_pct={raw!r}")
        return LIVENESS_PCT_DELTA_TRIGGER

# A single failed live fetch is often a momentary blip (laptop waking, Wi-Fi
# reassociating). Rather than toast immediately, we retry once after this delay
# and only declare a disconnect if the retry ALSO fails. See _fetch_with_tracking.
FETCH_RETRY_DELAY_SECS = 30

# A session window ends at a wall-clock instant we already know (session_end).
# Once it passes, the old window's token tally and % are meaningless. We reset
# locally WITHOUT waiting for a file event or an API round-trip (see
# _roll_over_if_expired). Whether the reset shows a fresh 0% or a "pending"
# blank depends on whether we caught the boundary live: a rollover running
# within ROLLOVER_GRACE_SECS of session_end means we were watching in real time
# and the new window genuinely just started at 0; noticing it long after (woke
# from sleep, or relaunched after a logout) means we can't know the new window's
# usage yet, so we blank to "--" until the API or a transcript confirms it.
ROLLOVER_GRACE_SECS = 90


# Lazily-resolved org_id and the resulting usage URL. Deliberately NOT computed
# at import time: import must succeed with no config so that auto-discovery and
# usage_check can run. USAGE_URL stays exposed for back-compat but is None until
# resolved.
_ORG_ID: str | None = None
USAGE_URL: str | None = None


def _resolve_org_id(cookies: dict | None = None, allow_prompt: bool = True) -> str | None:
    """Return a usable org_id, resolving in priority order and caching it:
      1. env / config (no network)
      2. auto-discovery via /api/organizations (needs cookies)   [D1]
      3. first-run prompt when discovery is ambiguous/fails       [D2]
    Returns None only when everything fails (-> status config_missing)."""
    global _ORG_ID, USAGE_URL
    if _ORG_ID:
        return _ORG_ID
    chosen = _configured_org_id()
    persist = False  # only write back ids we newly obtained, not env/config ones
    if not chosen:
        chosen = _discover_org_id(cookies)
        persist = bool(chosen)
    if not chosen and allow_prompt:
        chosen = _prompt_for_org_id(cookies)
        persist = bool(chosen)
    if chosen:
        _ORG_ID = chosen
        USAGE_URL = f"https://claude.ai/api/organizations/{chosen}/usage"
        if persist:
            _write_org_id(chosen)
    return _ORG_ID


# Back-compat: some callers (and earlier code) referenced _load_org_id().
def _load_org_id() -> str | None:
    return _resolve_org_id()


# ---------------------------------------------------------------------------
# Cookie loading (Firefox only)  [C1]
# ---------------------------------------------------------------------------

def _load_browser_cookies() -> dict:
    """Return {cookie_name: value} of claude.ai cookies from Firefox.

    Returns {} when Firefox has no claude.ai cookies or the DB is locked."""
    try:
        jar = browser_cookie3.firefox(domain_name=".claude.ai")
        return {c.name: c.value for c in jar}
    except Exception as e:
        print(f"  could not read Firefox cookies: {e}")
        return {}


# claude.ai sets a session cookie (sessionKey / __Secure-*) once logged in.
# Presence of *any* such marker distinguishes "logged out" from "no cookies".
_LOGIN_COOKIE_HINTS = ("sessionkey", "session", "lastactiveorg")


def _looks_logged_in(cookie_dict: dict) -> bool:
    lower = {k.lower() for k in cookie_dict}
    return any(any(h in k for h in _LOGIN_COOKIE_HINTS) for k in lower)


# ---------------------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------------------

def _fetch_usage_status() -> tuple[dict | None, str]:
    """Fetch live usage and classify the outcome.

    Returns (raw_json_or_None, status) where status is one of the STATUS_*
    contract values. Never swallows the failure silently - the cause is
    printed and reflected in the returned status so the tray can surface it."""
    cookie_dict = _load_browser_cookies()
    if not cookie_dict:
        print(f"  STATUS=no_cookie: no claude.ai cookies found in Firefox. "
              f"Open claude.ai in Firefox and log in.")
        return None, STATUS_NO_COOKIE
    if not _looks_logged_in(cookie_dict):
        print(f"  STATUS=no_login: cookies found in Firefox but no active "
              f"claude.ai session. Log in at https://claude.ai/.")
        return None, STATUS_NO_LOGIN

    org_id = _resolve_org_id(cookie_dict)
    if not org_id:
        print("  STATUS=config_missing: could not resolve an organization id "
              "(no config and auto-discovery failed).")
        return None, STATUS_CONFIG_MISSING

    url = f"https://claude.ai/api/organizations/{org_id}/usage"
    try:
        r = cffi_requests.get(
            url, cookies=cookie_dict,
            impersonate="firefox",
            headers={"Referer": "https://claude.ai/"},
            timeout=10,
        )
        if r.status_code in (401, 403):
            print(f"  STATUS=no_login: claude.ai returned {r.status_code} "
                  f"(session expired or not authorized).")
            return None, STATUS_NO_LOGIN
        r.raise_for_status()
        return r.json(), STATUS_OK
    except Exception as e:
        print(f"  STATUS=fetch_error: API call failed: {e}")
        return None, STATUS_FETCH_ERROR


def _fetch_usage() -> dict | None:
    """Back-compat wrapper kept for usage_check.py and existing callers:
    returns the raw JSON dict or None. Use _fetch_usage_status() when you
    need the status classification."""
    raw, _ = _fetch_usage_status()
    return raw


# ---------------------------------------------------------------------------
# org_id auto-discovery  [D1] + first-run prompt  [D2]
# ---------------------------------------------------------------------------

def _discover_org_id(cookies: dict | None) -> str | None:
    """Fetch /api/organizations and pick an org automatically. Prefers a
    Pro/Max org when several exist; returns None if ambiguous-without-signal
    or on any failure (caller then falls back to the prompt)."""
    if not cookies:
        return None
    try:
        r = cffi_requests.get(
            "https://claude.ai/api/organizations", cookies=cookies,
            impersonate="firefox", headers={"Referer": "https://claude.ai/"},
            timeout=10,
        )
        r.raise_for_status()
        orgs = r.json()
    except Exception as e:
        print(f"  org discovery failed: {e}")
        return None
    if not isinstance(orgs, list) or not orgs:
        return None
    if len(orgs) == 1:
        oid = orgs[0].get("uuid") or orgs[0].get("id")
        if oid:
            print(f"  auto-discovered org_id {oid} "
                  f"({orgs[0].get('name', '?')})")
        return oid
    # Multiple orgs: prefer one whose capabilities/plan signals Pro or Max.
    def _is_paid(o: dict) -> bool:
        caps = " ".join(str(c) for c in (o.get("capabilities") or []))
        blob = (caps + " " + str(o.get("billing_type", "")) + " "
                + str(o.get("rate_limit_tier", ""))).lower()
        return any(k in blob for k in ("claude_pro", "claude_max", "pro", "max", "raven"))
    paid = [o for o in orgs if _is_paid(o)]
    if len(paid) == 1:
        oid = paid[0].get("uuid") or paid[0].get("id")
        print(f"  auto-discovered Pro/Max org_id {oid} "
              f"({paid[0].get('name', '?')})")
        return oid
    print(f"  {len(orgs)} organizations found, none unambiguously Pro/Max - "
          f"will prompt.")
    return None  # ambiguous -> let the prompt decide


def _prompt_for_org_id(cookies: dict | None) -> str | None:
    """Ask the user to pick an org once. Tries a tkinter dialog (Windows),
    falls back to console. Returns the chosen org_id, or None if the user
    can't be asked (no orgs / non-interactive)."""
    orgs = []
    if cookies:
        try:
            r = cffi_requests.get(
                "https://claude.ai/api/organizations", cookies=cookies,
                impersonate="firefox", headers={"Referer": "https://claude.ai/"},
                timeout=10,
            )
            r.raise_for_status()
            orgs = r.json() if isinstance(r.json(), list) else []
        except Exception as e:
            print(f"  prompt: could not list organizations: {e}")
    if not orgs:
        return None

    def _oid(o):
        return o.get("uuid") or o.get("id")
    labels = [f"{o.get('name', '(unnamed)')}  [{_oid(o)}]" for o in orgs]

    # Try a GUI dialog first.
    try:
        import tkinter as tk
        from tkinter import simpledialog
        root = tk.Tk()
        root.withdraw()
        menu = "\n".join(f"{i+1}. {l}" for i, l in enumerate(labels))
        ans = simpledialog.askstring(
            "Claude Usage - choose organization",
            "Multiple Claude organizations found.\nEnter the number to use:\n\n"
            + menu,
        )
        root.destroy()
        if ans and ans.strip().isdigit():
            idx = int(ans.strip()) - 1
            if 0 <= idx < len(orgs):
                return _oid(orgs[idx])
    except Exception as e:
        print(f"  GUI prompt unavailable ({e}); falling back to console.")

    # Console fallback.
    try:
        print("Multiple Claude organizations found:")
        for i, l in enumerate(labels):
            print(f"  {i+1}. {l}")
        ans = input("Enter the number to use: ").strip()
        if ans.isdigit():
            idx = int(ans) - 1
            if 0 <= idx < len(orgs):
                return _oid(orgs[idx])
    except Exception as e:
        print(f"  console prompt failed ({e}).")
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
    except Exception as e:
        print(f"[X] _load_state failed, starting fresh: {type(e).__name__}: {e}")
        return _empty_state()


def _empty_state(session_start: datetime | None = None) -> dict:
    return {
        "seen_ids": set(),
        "input_tokens": 0,
        "output_tokens": 0,
        "by_model": {},
        # Per-file byte offsets for incremental JSONL parsing. See process_file.
        # Cleared on session reset so the new session counts from scratch.
        "offsets": {},
        "session_start": session_start.isoformat() if session_start else None,
        "calibration_calls_remaining": CALIBRATION_CALLS_PER_SESSION,
        # Guaranteed lower bound on the true session budget. Reset each session.
        "session_budget_lb": 0,
        # All API-confirmed (pct, io) pairs this session, used to find the
        # widest clean span for the tightest lower bound. See _set_anchor.
        "session_anchors": [],
    }


def _save_state(state: dict, session_pct: float | None = None,
                session_end: datetime | None = None,
                weekly_pct: float | None = None,
                weekly_end: datetime | None = None,
                status: str | None = None) -> None:
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
    if status is not None:
        out["status"] = status
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


# Number of recent above-floor calibrations to take the median of when
# seeding a prior_budget. Big enough to wash out a one-off outlier session,
# small enough to track real changes in usage habits over a few weeks.
PRIOR_BUDGET_WINDOW = 10


def _load_prior_budget_median() -> int | None:
    """Median implied_session_budget over the last PRIOR_BUDGET_WINDOW
    live-derived calibration samples. None if we don't have any history yet
    (fresh install, or every prior session also stalled below the floor).

    Only "live" entries (above-floor, direct back-derivation) are used.
    "blended" entries are partially derived from the prior itself, so
    including them creates a feedback loop that drifts the prior over time.

    Reads from calibration.jsonl, which the widget already writes. Same-day
    repeats are fine - they're all valid budget evidence."""
    if not CALIBRATION_FILE.exists():
        return None
    budgets: list[int] = []
    try:
        # Read tail-ish: the file is append-only and small (one line per
        # calibration, a few KB per session). Cheap to read whole.
        for line in CALIBRATION_FILE.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("budget_source") != "live":
                continue
            b = rec.get("implied_session_budget")
            if isinstance(b, (int, float)) and b > 0:
                budgets.append(int(b))
    except OSError as e:
        print(f"[X] prior budget read failed: {e}")
        return None
    if not budgets:
        return None
    recent = budgets[-PRIOR_BUDGET_WINDOW:]
    recent.sort()
    n = len(recent)
    return recent[n // 2] if n % 2 else (recent[n // 2 - 1] + recent[n // 2]) // 2


def _blended_sub_floor_budget(total_io: int, pct_live: float,
                              prior_median: int | None) -> int | None:
    """Estimate a session budget when the live API pct is below the
    CALIBRATION_PCT_FLOOR, by blending a live-reading midpoint X with the
    user's historical median M.

    The API only ever returns integer percentages, so a reading of N means
    the true pct lies in [N-0.5, N+0.5] (clamped at 0). The midpoint of that
    range, fed back through total_io, gives X - our best single-shot guess
    from the live reading alone.

    M (prior_median) is the user's typical session budget across recent
    above-floor calibrations - their gravity. It carries real signal even
    when it disagrees with X, especially at pct=0 where X's bounds are wide
    enough to be barely informative.

    Blend: w * X + (1-w) * M, where w = pct_live / (pct_live + 1).
    This follows directly from the floor-rounding uncertainty: at pct=N the
    true budget is in [100*io/(N+1), 100*io/N], a ratio of (N+1)/N. So:
      pct=0 → w=0   (entirely prior; local gives no upper bound at all)
      pct=1 → w=0.5 (factor-of-2 uncertainty; equal weight)
      pct=2 → w=0.67
      pct=4 → w=0.8 (25% uncertainty; mostly local)
    At pct≥5 the main _append_calibration path handles it directly.

    Lower-clamp at X/2 only - asymmetric. The symmetric [X/2, 2X] clamp
    would actively cause the off-laptop-contamination overshoot it's
    supposed to prevent: if the user runs Claude elsewhere, local total_io
    undercounts true usage so X comes out too small, and M (which reflects
    full-session reality) is the more correct value. Capping upward at 2X
    would yank the blended answer back to the wrong X-derived number.
    M < X is the suspicious direction (prior sessions somehow smaller than
    this one's X estimate suggests) - that's where the clamp earns its
    keep. M > X is the contamination signature - let it through.
    Returns None if we can't form even an X (zero tokens)."""
    if total_io <= 0 or pct_live is None or pct_live >= CALIBRATION_PCT_FLOOR:
        return None
    # Floor rounding: pct=N means true pct ∈ [N, N+1) (floor convention).
    # Midpoint of [N, N+1) is N+0.5; clamped lower edge at 0 for pct=0.
    lower_pct = max(pct_live, 0.0)
    midpoint_pct = lower_pct + 0.5
    x = total_io / (midpoint_pct / 100)
    if prior_median is None or prior_median <= 0:
        return int(round(x))
    # w = pct/(pct+1): 0 at pct=0 (entirely prior), 0.8 at pct=4.
    w = pct_live / (pct_live + 1.0)
    blended = w * x + (1 - w) * prior_median
    # Asymmetric clamp: floor at X/2 to stop M dragging us implausibly low,
    # but no ceiling - M > X is the off-laptop-contamination signature and
    # we want M to win in that case.
    blended = max(x * 0.5, blended)
    return int(round(blended))


def _append_calibration(state: dict, pct: float, scraped_at: datetime,
                        update_budget: bool = True,
                        stale_pct: float | None = None,
                        trigger: str = "scheduled") -> None:
    total_io = state["input_tokens"] + state["output_tokens"]
    # Above-floor: back-derive the budget straight from the live pct. Below
    # the floor, integer-rounding swamps a direct back-derivation, so we
    # blend a live-reading midpoint with the user's historical median (see
    # _blended_sub_floor_budget). Pre-fix behaviour was to record None and
    # leave the display stuck at whatever pct the API last returned - which
    # could mean an hour of "1%" after a calibration burned both startup
    # calls under the floor.
    if pct >= CALIBRATION_PCT_FLOOR:
        implied = round(total_io / (pct / 100))
        budget_source = "live"
    else:
        prior = _load_prior_budget_median()
        implied = _blended_sub_floor_budget(total_io, pct, prior)
        budget_source = "blended" if implied is not None else None
    # Never let the budget fall below the delta-derived lower bound. If the
    # absolute back-derivation yields a smaller number, it's wrong (the math
    # guarantees lb ≤ B_true always), so clamp up. Off-laptop contamination
    # only inflates Δpct, producing a smaller (more conservative) lb — the
    # max() in _set_anchor ensures we hold the tightest bound we've seen.
    lb = state.get("session_budget_lb", 0)
    if lb and implied and implied < lb:
        print(f"  budget lb clamp: {implied} -> {lb} (delta evidence)")
        implied = lb
    if implied and update_budget:
        state["implied_session_budget"] = implied
    record   = {
        "scraped_at":              scraped_at.isoformat(),
        "session_pct":             pct,
        "stale_pct_before":        stale_pct,
        "trigger":                 trigger,
        "session_start":           state.get("session_start"),
        "transcript_input_tokens": state["input_tokens"],
        "transcript_output_tokens":state["output_tokens"],
        "transcript_io_total":     total_io,
        "implied_session_budget":  implied,
        "budget_source":           budget_source,
        "by_model":                state["by_model"],
        "source":                  "widget",
    }
    CALIBRATION_FILE.parent.mkdir(parents=True, exist_ok=True)
    with CALIBRATION_FILE.open("a") as f:
        f.write(json.dumps(record) + "\n")
    print(f"  calibration: {pct}% = {total_io} tokens => budget ~{implied} "
          f"({budget_source})")


# ---------------------------------------------------------------------------
# Token counting
# ---------------------------------------------------------------------------

def _estimate_session_pct(state: dict) -> float | None:
    """Session % extrapolated from the local token count and the budget
    implied by the last API calibration. None until a budget exists.

    When an API anchor is present (set by _set_anchor on every API read),
    extrapolates as:  anchor_pct + 100 * (current_io - anchor_io) / budget
    This snaps the estimate to the exact API value at calibration time and
    grows it only by locally-observed tokens written after that snapshot,
    eliminating drift from budget rounding and off-laptop usage.

    Module-level (not just a handler method) so the freeze-regression test can
    exercise it without standing up a network-touching TranscriptHandler."""
    budget = state.get("implied_session_budget")
    if not budget:
        return None
    io_total   = state["input_tokens"] + state["output_tokens"]
    anchor_pct = state.get("anchor_pct")
    anchor_io  = state.get("anchor_io", 0)
    if anchor_pct is not None:
        raw = anchor_pct + 100 * (io_total - anchor_io) / budget
    else:
        # Fallback before first API reading: total-io / budget.
        raw = 100 * io_total / budget
    return min(100, round(raw, 1))


# Cheap byte-level prefilter: assistant records with usage are the only lines
# that contribute to token totals. Skipping json.loads on the ~90% of lines
# that can't possibly match (user turns, tool results) is the bulk of the
# speedup on top of the seek-to-offset incremental read.
#
# Match on bare quoted tokens (not "type":"assistant" with no space) so the
# filter survives any whitespace Claude Code or json.dumps might insert
# around colons. False positives are fine - json.loads runs as a second
# gate and the obj.get("type") check rejects them. Only false NEGATIVES
# would lose tokens, and the quoted word "assistant" only appears in
# transcripts as the type value.
_ASSISTANT_MARKER = b'"assistant"'
_USAGE_MARKER     = b'"usage"'


def process_file(path: Path, state: dict, session_start: datetime, session_end: datetime) -> bool:
    """Incrementally read `path` from the last byte offset we consumed.

    JSONL transcripts are append-only, so we seek to where we left off and
    parse only the new bytes. Offsets are persisted in state["offsets"], keyed
    by absolute path string. A trailing partial line (write landed mid-line
    between FS events) is left for the next call.

    Failures are LOUD - swallowing exceptions silently is what masked the
    2.5MB-file stall in the first place. Anything unexpected prints to
    stdout (which the tray captures to widget_run.log).
    """
    key = str(path)
    offsets = state.setdefault("offsets", {})
    offset  = offsets.get(key, 0)

    try:
        size = path.stat().st_size
    except OSError as e:
        print(f"[X] process_file stat failed for {path}: {e}")
        return False

    # File shrank => rotated/truncated/replaced. Rescan from 0; seen_ids
    # still dedupes anything we already counted.
    if size < offset:
        print(f"[!] {path.name} shrank ({size} < {offset}); rescanning from 0")
        offset = 0
    if size == offset:
        return False

    try:
        with path.open("rb") as f:
            f.seek(offset)
            chunk = f.read()
    except OSError as e:
        print(f"[X] process_file read failed for {path} at offset {offset}: {e}")
        return False

    # Keep any unterminated trailing line for next event. Without this we'd
    # corrupt one record per write that lands mid-line.
    last_nl = chunk.rfind(b"\n")
    if last_nl == -1:
        return False  # no complete line yet
    complete = chunk[:last_nl]
    offsets[key] = offset + last_nl + 1

    changed = False
    for raw in complete.split(b"\n"):
        if _ASSISTANT_MARKER not in raw or _USAGE_MARKER not in raw:
            continue
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError as e:
            # Loud: a line passed the prefilter but isn't valid JSON. Either
            # the format changed or our offset bookkeeping desynced.
            print(f"[X] process_file JSON decode failed in {path.name}: {e} "
                  f"(line starts: {raw[:80]!r})")
            continue
        try:
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
        except Exception as e:
            # Loud: an assistant/usage record failed downstream accounting.
            # Don't kill the whole scan - keep going - but make the failure
            # visible so it can be fixed before the next release.
            print(f"[X] process_file record accounting failed in {path.name}: "
                  f"{type(e).__name__}: {e}")
    return changed


def full_scan(state: dict, session_start: datetime, session_end: datetime) -> None:
    seen_paths = set()
    for f in PROJECTS_DIR.rglob("*.jsonl"):
        seen_paths.add(str(f))
        mtime = datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc)
        if mtime >= session_start:
            process_file(f, state, session_start, session_end)
    # Prune offset entries for files that are gone or stale - keeps the state
    # file from growing without bound across weeks of sessions.
    offsets = state.get("offsets", {})
    for key in list(offsets):
        if key not in seen_paths:
            del offsets[key]


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
        # Last pct we adopted straight from the API (calibration/liveness/force),
        # used to measure how far the local estimate has drifted ahead of truth.
        self.last_api_pct     = None
        # Cooldown stamp for emergency re-anchoring (see _estimate_is_suspect),
        # so a session pegged at the 100% clamp can't fetch on every file event.
        self.last_forced_recal = None
        # When we last got a transcript file event -- proof the watcher is
        # delivering. Used by _rescan_and_check_watcher to tell a stuck watcher
        # (silent + missed tokens on disk) from a healthy idle one.
        self.last_event_at    = None
        # Last successful/attempted liveness ping (distinct from calibration).
        self.last_liveness    = None
        # Pct at the time of the last liveness attempt; delta-trigger baseline.
        self._liveness_anchor_pct  = None
        # One-shot thresholds already triggered this session.
        self._triggered_thresholds: set[int] = set()
        # Most recent status contract value (see STATUS_* constants). Seeded
        # from disk so a restart doesn't briefly report "ok" before the first
        # fetch returns.
        self.status           = self.state.get("status")
        # Consecutive API failures since the last success. Reset to 0 on
        # any successful fetch.
        self._api_failures    = 0
        # One-shot timer for the post-failure retry, and a once-per-outage
        # guard so a sustained outage toasts once rather than every cycle.
        self._retry_timer        = None
        # One-shot timer for the deferred "watcher stuck" re-check (see
        # _arm_stuck_recheck), so a ping arriving a beat late cancels the alarm.
        self._stuck_timer        = None
        # Recurring timer for the proactive watchdog (_watchdog_tick), started
        # by start_watchdog() after the observer is up.
        self._watchdog_timer     = None
        self._disconnect_notified = False
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
        # If the saved window already ended (widget was closed across a session
        # boundary), roll it over before _startup so its stale pct can't seed a
        # false "stuck" discrepancy against the fresh API value.
        self._roll_over_if_expired()
        self._startup()

    def _fetch_with_tracking(self) -> dict | None:
        """Wraps _fetch_usage_status to track consecutive failures and the
        current status.

        The FIRST failure in a streak doesn't toast: it schedules a single
        retry FETCH_RETRY_DELAY_SECS later (see _retry_fetch). Only if failures
        reach DISCONNECT_FAIL_THRESHOLD do we fire the disconnect callback, and
        then just once per outage (self._disconnect_notified) so a sustained
        outage doesn't re-toast every cycle.

        Side effects: updates self.status and self.last_liveness (any attempt
        counts as a liveness check, success or failure)."""
        raw, status = _fetch_usage_status()
        self.status = status
        self.last_liveness = datetime.now(timezone.utc)
        if raw is None:
            self._api_failures += 1
            if self._api_failures == 1:
                # First blip: don't alarm the user yet, just retry shortly.
                self._schedule_fetch_retry()
            elif (self._api_failures >= DISCONNECT_FAIL_THRESHOLD
                  and not self._disconnect_notified and self.on_disconnect):
                self._disconnect_notified = True
                try:
                    self.on_disconnect(
                        f"Lost contact with claude.ai ({self._api_failures} fetches "
                        f"failed; status={status})."
                    )
                except Exception as e:
                    print(f"  on_disconnect callback error: {e}")
        else:
            self._api_failures = 0
            self._disconnect_notified = False
            if self._retry_timer is not None:
                self._retry_timer.cancel()
                self._retry_timer = None
        return raw

    def _schedule_fetch_retry(self) -> None:
        """Arm a one-shot retry FETCH_RETRY_DELAY_SECS after a first failed
        fetch. Idempotent: a retry already pending is not re-armed."""
        if self._retry_timer is not None:
            return
        t = threading.Timer(FETCH_RETRY_DELAY_SECS, self._retry_fetch)
        t.daemon = True
        self._retry_timer = t
        t.start()

    def _retry_fetch(self) -> None:
        """Re-attempt a fetch after a first failure. On success, adopt the
        fresh numbers and notify the tray (clearing the error state); on a
        second failure, _fetch_with_tracking fires the disconnect toast."""
        self._retry_timer = None
        prev_status = self.status
        self._log_estimate()
        raw = self._fetch_with_tracking()
        if raw is not None:
            _, _, pct = _parse_session(raw)
            wk_pct, wk_end = _parse_weekly(raw)
            if pct is not None:
                self.session_pct = pct
                self._set_anchor(pct)
                self._log_estimate()
            if wk_pct is not None:
                self.weekly_pct = wk_pct
                self.weekly_end = wk_end
        if self.status != prev_status or raw is not None:
            _save_state(self.state, self.session_pct, self.session_end,
                        self.weekly_pct, self.weekly_end, status=self.status)
            self._notify()

    def _check_and_maybe_disconnect(self, kind: str, stored: float | None,
                                    api: float | None,
                                    scraped_at: datetime,
                                    suppress_toast: bool = False) -> None:
        """Log a discrepancy (always >1pp) and, if it's large enough, also
        fire the disconnect callback so the tray can prompt a restart.

        suppress_toast: we just re-derived the budget from this same API value
        (the >RECAL_DISCREPANCY_PP path), so the mismatch is already being
        corrected -- record it, but don't tell the user the widget is stuck."""
        _check_discrepancy(kind, stored, api, self.last_calibrated, self.state, scraped_at)
        if (not suppress_toast
                and stored is not None and api is not None
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
                    "status":        self.status,
                })
            except Exception as e:
                print(f"  tray notify error: {e}")

    def _log_estimate(self) -> None:
        if self.session_pct is not None:
            print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] "
                  f"in={self.state['input_tokens']} "
                  f"out={self.state['output_tokens']} "
                  f"pct={self.session_pct}")

    def _startup(self):
        self._log_estimate()
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
            # Clear stale session state so the tray drops to a neutral timer
            # ("--") and zero usage instead of lingering on the expired
            # window's last reading.
            self.session_start = None
            self.session_end   = None
            self.session_pct   = 0
            _save_state(self.state, self.session_pct, self.session_end,
                        self.weekly_pct, self.weekly_end, status=self.status)
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
        self._set_anchor(pct)  # also resets _liveness_anchor_pct + thresholds
        self._log_estimate()

        self.last_calibrated = datetime.now(timezone.utc)
        _append_calibration(self.state, pct, self.last_calibrated,
                            stale_pct=self.session_pct, trigger="startup")
        _save_state(self.state, pct, session_end, self.weekly_pct,
                    self.weekly_end, status=self.status)
        print(f"  Session {pct}% | weekly {self.weekly_pct}% | tokens in+out: "
              f"{self.state['input_tokens'] + self.state['output_tokens']}")
        self._notify()

    def _local_estimate(self) -> float | None:
        """Extrapolate the session % from the local token count using the
        budget implied by the last API calibration. Returns None until we've
        calibrated at least once (no budget to divide by). This is what keeps
        the display moving between API hits instead of freezing."""
        return _estimate_session_pct(self.state)

    def _set_anchor(self, pct: float) -> None:
        """Record the API-confirmed pct and current local io as the extrapolation
        anchor.  _local_estimate then starts from pct and adds delta only, so the
        display snaps to the API value and drifts only on new local tokens.

        Also updates session_budget_lb by checking the current reading against
        every prior anchor this session and taking the best lower bound:

          lb = 100 * Δio_local / (Δpct_api + 1)

        The "+1" accounts for worst-case floor rounding (true Δpct can be up to
        observed + 1 pp). Checking all past anchors, not just the most recent,
        lets this correction amortize over the widest clean span available: if
        poll 2 was contaminated (off-laptop inflated Δpct → small lb), pair
        (1, 3) may still span a large clean Δio relative to Δpct and give a
        tighter bound. max() across all pairs keeps the best seen."""
        io_now  = self.state["input_tokens"] + self.state["output_tokens"]
        anchors = self.state.get("session_anchors") or []
        if io_now > 0 and anchors:
            best_lb = 0
            for prev_pct, prev_io in anchors:
                d_pct = pct - prev_pct
                d_io  = io_now - prev_io
                # Skip negative Δpct (session reset) and non-positive Δio.
                if d_pct >= 0 and d_io > 0:
                    lb = int(100 * d_io / (d_pct + 1.0))
                    if lb > best_lb:
                        best_lb = lb
            if best_lb > 0:
                self.state["session_budget_lb"] = max(
                    self.state.get("session_budget_lb", 0), best_lb
                )
        self.state["session_anchors"] = anchors + [[pct, io_now]]
        self.state["anchor_pct"] = pct
        self.state["anchor_io"]  = io_now
        # Reset both liveness baselines so the 20-min timer and the 10pp
        # delta trigger are always measured from the last API read, regardless
        # of whether it came from calibration, liveness, force_refresh, etc.
        self._liveness_anchor_pct = pct
        self._triggered_thresholds |= {
            t for t in _liveness_oneshot_pcts() if pct >= t
        }

    def _adopt_api_pct(self, pct: float | None, now: datetime,
                       trigger: str = "scheduled") -> bool:
        """Adopt a freshly-fetched session pct as the authoritative display value
        and re-derive the budget IFF it disagrees with what we were showing by
        more than RECAL_DISCREPANCY_PP (or we hold no budget yet) and clears the
        pct floor. Always records the sample for history. Returns True if it
        re-derived the budget, so the caller can suppress the now-redundant
        'stuck' toast (we're already correcting the mismatch)."""
        if pct is None:
            return False
        prior     = self.session_pct
        no_budget = not self.state.get("implied_session_budget")
        big_diff  = prior is not None and abs(pct - prior) > RECAL_DISCREPANCY_PP
        recalibrate = (no_budget or big_diff) and pct >= CALIBRATION_PCT_FLOOR
        self.session_pct  = pct
        self.last_api_pct = pct
        # Re-scan from disk before deriving the budget so it's computed against
        # the true token count, and so a stuck watcher surfaces (see method).
        if recalibrate:
            self._rescan_and_check_watcher(now)
        self._set_anchor(pct)
        _append_calibration(self.state, pct, now, update_budget=recalibrate,
                            stale_pct=prior, trigger=trigger)
        return recalibrate

    def _rescan_and_check_watcher(self, now: datetime) -> int:
        """Active full re-scan of the transcript folder. The running tally is
        built only from on_modified pings; if the watcher died/missed events the
        on-disk transcripts hold tokens we never counted. A seen_ids-deduped
        full_scan adds exactly those, healing the count. Returns the number of
        recovered tokens.

        If it recovered any AND live events have been silent for
        WATCHER_STUCK_SILENCE_SECS, the watcher is stuck (a healthy watcher
        delivers events, so silence + on-disk growth is the tell) -- prompt a
        restart. Off-laptop usage leaves no local-disk evidence, so it never
        trips this."""
        if self.session_start is None or self.session_end is None:
            return 0
        io_before = self.state["input_tokens"] + self.state["output_tokens"]
        full_scan(self.state, self.session_start, self.session_end)
        missed = (self.state["input_tokens"] + self.state["output_tokens"]) - io_before
        if missed <= 0 or self.last_event_at is None:
            return missed
        silent_for = (now - self.last_event_at).total_seconds()
        if silent_for > WATCHER_STUCK_SILENCE_SECS:
            self._arm_stuck_recheck(self.last_event_at)
        return missed

    def _arm_stuck_recheck(self, marker: datetime) -> None:
        """We found on-disk tokens with no recent ping. Don't alarm yet -- a ping
        may be a beat behind. Wait WATCHER_STUCK_RECHECK_SECS, then alarm only if
        still no event has arrived (last_event_at unchanged from `marker`).
        Idempotent: one pending re-check at a time."""
        if self._stuck_timer is not None:
            return
        t = threading.Timer(WATCHER_STUCK_RECHECK_SECS, self._stuck_recheck, args=(marker,))
        t.daemon = True
        self._stuck_timer = t
        t.start()

    def _stuck_recheck(self, marker: datetime) -> None:
        self._stuck_timer = None
        if self.last_event_at != marker:
            return  # a ping landed during the grace window -- watcher is alive
        if self.on_disconnect:
            try:
                self.on_disconnect(
                    "Live tracking looks stuck (found usage on disk we weren't "
                    "notified about). Restart to resume live updates."
                )
            except Exception as e:
                print(f"  on_disconnect callback error: {e}")

    def _watchdog_tick(self) -> None:
        """Periodic proactive check that the watcher is keeping up. Walks
        PROJECTS_DIR for the newest transcript mtime; if it's more than
        WATCHDOG_MTIME_GAP_SECS ahead of last_event_at, the watcher missed
        events. Trigger a rescan (which heals the token count via seen_ids
        dedupe) and let _arm_stuck_recheck decide whether to alarm.

        Mtime check is cheap (one stat per file, no parsing). The rescan
        only runs if mtime says something's actually off, so the hot path
        on a healthy widget is just a directory walk + stat. Loud on
        failure - swallowing a watchdog error would defeat its purpose.

        Reschedules itself unconditionally so a single failed tick doesn't
        kill the watchdog forever."""
        try:
            if self.session_start is None:
                return  # no active session => nothing to watchdog
            newest = 0.0
            try:
                for f in PROJECTS_DIR.rglob("*.jsonl"):
                    try:
                        m = f.stat().st_mtime
                        if m > newest:
                            newest = m
                    except OSError:
                        continue  # file vanished between rglob and stat - fine
            except OSError as e:
                print(f"[X] watchdog directory walk failed: {e}")
                return
            if newest == 0.0:
                return  # no transcripts at all
            now = datetime.now(timezone.utc)
            newest_dt = datetime.fromtimestamp(newest, tz=timezone.utc)
            # If we've never had an event, treat session_start as the baseline -
            # otherwise a fresh start would alarm against the first transcript.
            baseline = self.last_event_at or self.session_start
            gap = (newest_dt - baseline).total_seconds()
            if gap <= WATCHDOG_MTIME_GAP_SECS:
                return
            print(f"[!] watchdog: transcript mtime {gap:.0f}s ahead of last event; "
                  f"rescanning")
            self._rescan_and_check_watcher(now)
        except Exception as e:
            # Loud - silent watchdog failure is exactly the class of bug
            # the watchdog exists to catch.
            print(f"[X] watchdog tick failed: {type(e).__name__}: {e}")
        finally:
            t = threading.Timer(WATCHDOG_INTERVAL_SECS, self._watchdog_tick)
            t.daemon = True
            self._watchdog_timer = t
            t.start()

    def start_watchdog(self) -> None:
        """Kick off the periodic watchdog. Idempotent - safe to call twice."""
        if getattr(self, "_watchdog_timer", None) is not None:
            return
        t = threading.Timer(WATCHDOG_INTERVAL_SECS, self._watchdog_tick)
        t.daemon = True
        self._watchdog_timer = t
        t.start()

    def _maybe_calibrate(self, force: bool = False) -> bool:
        """Hit the API for a fresh session % if the per-session budget or the
        max-age window allows. Returns True if it adopted an API value, so the
        caller knows whether to fall back to the local estimate instead.

        force=True bypasses the per-session budget / max-age gate for an
        emergency re-anchor (see _estimate_is_suspect); the caller is
        responsible for the cooldown that keeps it from hammering the link."""
        now = datetime.now(timezone.utc)
        calls_left = self.state.get("calibration_calls_remaining", 0)
        age = (now - self.last_calibrated).total_seconds() if self.last_calibrated else float("inf")
        if not force and calls_left <= 0 and age < CALIBRATION_MAX_AGE_SECS:
            return False
        self._log_estimate()
        print("Fetching usage from API (calibration)...")
        raw = self._fetch_with_tracking()
        _, _, pct = _parse_session(raw)
        wk_pct, wk_end = _parse_weekly(raw)
        prior = self.session_pct
        recalibrated = self._adopt_api_pct(pct, now,
                                           trigger="suspect" if force else "scheduled")
        self._check_and_maybe_disconnect("session", prior, pct, now,
                                         suppress_toast=recalibrated)
        self._check_and_maybe_disconnect("weekly",  self.weekly_pct, wk_pct, now)
        if wk_pct is not None:
            self.weekly_pct = wk_pct
            self.weekly_end = wk_end
        if pct is None:
            return False
        self.last_calibrated = now
        if calls_left > 0:
            self.state["calibration_calls_remaining"] -= 1
        return True

    def _estimate_is_suspect(self, est: int, now: datetime) -> bool:
        """True when the local estimate has gone somewhere that means the budget
        is wrong and we should re-anchor against the API: it pegged at the 100%
        clamp, or it sprinted FORCE_RECAL_GAP_PP past the last API-confirmed pct.
        Cooldown-gated so a session stuck at the clamp can't fetch every event."""
        if (self.last_forced_recal is not None and
                (now - self.last_forced_recal).total_seconds() < FORCE_RECAL_COOLDOWN_SECS):
            return False
        budget = self.state.get("implied_session_budget")
        if not budget:
            return False
        io_total = self.state["input_tokens"] + self.state["output_tokens"]
        clamp_hit = 100 * io_total / budget >= 100          # unclamped >= 100
        big_gap   = (self.last_api_pct is not None and
                     est - self.last_api_pct >= FORCE_RECAL_GAP_PP)
        return clamp_hit or big_gap

    def _maybe_liveness(self) -> None:
        """Liveness poll: re-anchor the displayed pct against the API and detect
        a dead/disconnected link. Fires on any of:

          1. Time: LIVENESS_INTERVAL_SECS (20 min) since the last attempt.
          2. Delta: local estimate has moved LIVENESS_PCT_DELTA_TRIGGER (10pp)
             since the last attempt — naturally front-loads early when the budget
             is burning fast, slackens mid-session without an explicit schedule.
          3. One-shots: first time the local estimate crosses 5%, 10%, or 95%
             (LIVENESS_ONE_SHOT_PCTS). 5% = floor crossing; 10% = meaningful lb
             evidence; 95% = cap warning.

        Does not consume the calibration budget."""
        now = datetime.now(timezone.utc)
        age = ((now - self.last_liveness).total_seconds()
               if self.last_liveness else float("inf"))
        est = _estimate_session_pct(self.state)  # process_file already ran

        time_due  = age >= _liveness_interval_secs()
        delta_due = (self._liveness_anchor_pct is not None
                     and est is not None
                     and abs(est - self._liveness_anchor_pct) >= _liveness_delta_pct())
        due_shots = (set() if est is None else
                     {t for t in _liveness_oneshot_pcts()
                      if est >= t and t not in self._triggered_thresholds})

        if not (time_due or delta_due or due_shots):
            return

        if   due_shots and 5  in due_shots: trigger = "liveness_5pct"
        elif due_shots and 10 in due_shots: trigger = "liveness_10pct"
        elif due_shots and 95 in due_shots: trigger = "liveness_95pct"
        elif delta_due:                     trigger = "liveness_10ppdelta"
        else:                               trigger = "liveness"

        prev_status = self.status
        self._log_estimate()
        raw = self._fetch_with_tracking()  # updates self.status + last_liveness
        if raw is not None:
            _, _, pct = _parse_session(raw)
            wk_pct, wk_end = _parse_weekly(raw)
            prior = self.session_pct
            recalibrated = self._adopt_api_pct(pct, now, trigger=trigger)
            self._log_estimate()
            self._check_and_maybe_disconnect("session", prior, pct, now,
                                             suppress_toast=recalibrated)
            self._check_and_maybe_disconnect("weekly",  self.weekly_pct, wk_pct, now)
            if wk_pct is not None:
                self.weekly_pct = wk_pct
                self.weekly_end = wk_end

        # On success _set_anchor (called inside _adopt_api_pct) already reset
        # both baselines. On failure, do it here so a persistent API outage
        # can't cause continuous re-polling on the same triggers.
        if raw is None:
            self._liveness_anchor_pct = est if est is not None else self.session_pct
            self._triggered_thresholds |= due_shots

        if self.status != prev_status or raw is not None:
            _save_state(self.state, self.session_pct, self.session_end,
                        self.weekly_pct, self.weekly_end, status=self.status)
            self._notify()

    def _roll_over_if_expired(self, now: datetime | None = None) -> bool:
        """Reset local state the instant the session window ends. Network-free,
        so it works while idle or with the API leg down, and event-free, so it
        doesn't depend on a transcript file changing. Returns True if it rolled
        over.

        Caught live (within ROLLOVER_GRACE_SECS of the boundary) => show a fresh
        0%; noticed late (woke from sleep / relaunched after a logout) => blank
        to pending (None / "--") until the API or a transcript confirms the new
        window. Either way the stale token tally, budget and pct are cleared so
        the display can't linger on the dead window AND the discrepancy check
        can't fire a bogus "stuck" alert against it when the API next reports
        the new window at 0%."""
        now = now or datetime.now(timezone.utc)
        if self.session_end is None or now < self.session_end:
            return False
        caught_live = now <= self.session_end + timedelta(seconds=ROLLOVER_GRACE_SECS)
        self.state = _empty_state(None)
        self.session_start = None
        self.session_end   = None
        self.session_pct   = 0 if caught_live else None
        # Drop the calibration anchor so the next _maybe_calibrate re-anchors the
        # new window immediately instead of waiting out CALIBRATION_MAX_AGE_SECS.
        self.last_calibrated = None
        self._liveness_anchor_pct = None
        self._triggered_thresholds = set()
        _save_state(self.state, self.session_pct, self.session_end,
                    self.weekly_pct, self.weekly_end, status=self.status)
        self._notify()
        return True

    def on_modified(self, event):
        if event.is_directory or not event.src_path.endswith(".jsonl"):
            return
        now = datetime.now(timezone.utc)
        # Proof the watcher is delivering events (see _rescan_and_check_watcher).
        self.last_event_at = now
        # Time-based rollover first: clears the dead window's token tally + pct
        # so _startup's discrepancy check can't fire a false "stuck" alert.
        self._roll_over_if_expired(now)
        if self.session_start is None:
            self._startup()
            return

        changed = process_file(
            Path(event.src_path), self.state,
            self.session_start, datetime.now(timezone.utc),
        )

        # Liveness heartbeat after token counting so the anchor is set against
        # the up-to-date io total, preventing an immediate post-anchor overshoot
        # from tokens that arrived in this same file event.
        self._maybe_liveness()

        if changed:
            calibrated = self._maybe_calibrate()
            # If we didn't just adopt a fresh API value, advance the displayed
            # % from the local token count so it tracks usage instead of
            # freezing at the last calibration. The API still wins whenever it
            # is consulted (calibration/liveness/Confirm).
            if not calibrated:
                est = self._local_estimate()
                if est is not None:
                    self.session_pct = est
                    # Self-heal: a pegged-at-100 or runaway estimate means the
                    # budget is wrong -- spend one (cooldown-gated) API call to
                    # re-anchor instead of trusting it. _maybe_calibrate adopts
                    # the API pct into session_pct on success.
                    if self._estimate_is_suspect(est, now):
                        self.last_forced_recal = now
                        self._maybe_calibrate(force=True)
            _save_state(self.state, self.session_pct, self.session_end,
                        self.weekly_pct, self.weekly_end, status=self.status)
            print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] "
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
        self._log_estimate()
        print("Fetching usage from API (forced)...")
        raw = self._fetch_with_tracking()
        if raw is None:
            return False
        session_start, session_end, pct = _parse_session(raw)
        wk_pct, wk_end = _parse_weekly(raw)
        scraped_at = datetime.now(timezone.utc)
        prior = self.session_pct
        if wk_pct is not None:
            self.weekly_pct = wk_pct
            self.weekly_end = wk_end
        if pct is not None:
            self.session_pct = pct
            self.last_api_pct = pct
        reset = False
        if session_start is not None:
            stored_start = self.state.get("session_start")
            if stored_start != session_start.isoformat():
                self.state = _empty_state(session_start)
                full_scan(self.state, session_start, session_end)
                reset = True
            self.session_start = session_start
            self.session_end = session_end
        self.last_calibrated = datetime.now(timezone.utc)
        # Re-derive the budget only on a meaningful disagreement (or no budget /
        # fresh window), same gate as _adopt_api_pct -- inlined here because the
        # session-reset above must run before we derive against the new tokens.
        recalibrated = False
        if pct is not None:
            no_budget   = not self.state.get("implied_session_budget")
            big_diff    = prior is not None and abs(pct - prior) > RECAL_DISCREPANCY_PP
            recalibrated = (no_budget or big_diff) and pct >= CALIBRATION_PCT_FLOOR
            # A fresh window already full_scanned from empty, so there's no
            # missed-token baseline to check; only re-scan otherwise.
            if recalibrated and not reset:
                self._rescan_and_check_watcher(self.last_calibrated)
            self._set_anchor(pct)
            self._log_estimate()
            _append_calibration(self.state, pct, self.last_calibrated,
                                update_budget=recalibrated,
                                stale_pct=prior, trigger="force_refresh")
        self._check_and_maybe_disconnect("session", prior, pct, scraped_at,
                                         suppress_toast=recalibrated)
        self._check_and_maybe_disconnect("weekly",  self.weekly_pct, wk_pct, scraped_at)
        _save_state(self.state, self.session_pct, self.session_end,
                    self.weekly_pct, self.weekly_end, status=self.status)
        self._notify()
        return True


def _list_sessions() -> list[dict]:
    """Distinct sessions from calibration.jsonl, newest first.

    Session_starts within 5 minutes of each other are merged into one entry
    (handles drift from widget restarts re-deriving session_start = resets_at - 5h).
    Each entry has "at" (local datetime string, passable to save_accuracy_chart --at)
    and "label" (same string for display)."""
    if not CALIBRATION_FILE.exists():
        return []

    trunc_starts = []
    for line in CALIBRATION_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        ss = r.get("session_start")
        if not ss:
            continue
        try:
            dt_utc = datetime.fromisoformat(ss)
        except ValueError:
            continue
        trunc_starts.append(dt_utc.replace(second=0, microsecond=0))

    if not trunc_starts:
        return []

    distinct = sorted(set(trunc_starts))
    canonicals = []
    current = None
    for s in distinct:
        if current is None or (s - current).total_seconds() > 300:
            current = s
            canonicals.append(current)

    sessions = []
    for c in reversed(canonicals):
        dt_local = datetime.fromtimestamp(c.timestamp())
        label = dt_local.strftime("%Y-%m-%d %H:%M")
        sessions.append({"at": label, "label": label})
    return sessions


def _run_accuracy_chart(at: str | None) -> bool:
    """Run save_accuracy_chart synchronously; save PNG to CHART_FILE.

    Returns True on success. Blocks for ~1-2 s (matplotlib startup).

    In a frozen build the bundled ClaudeUsageChart.exe is used directly —
    sys.executable there is the bootloader, not a Python interpreter."""
    if getattr(sys, "frozen", False):
        chart_bin = Path(sys.executable).parent / "ClaudeUsageChart.exe"
        if not chart_bin.exists():
            print(f"  chart: {chart_bin} not found")
            return False
        cmd = [str(chart_bin)]
    else:
        script = _BASE / "save_accuracy_chart.py"
        if not script.exists():
            print(f"  chart: {script} not found")
            return False
        cmd = [sys.executable, str(script)]
    cmd += ["--no-open", "--out", str(CHART_FILE)]
    if at:
        cmd += ["--at", at]
    flags = 0x08000000 if sys.platform == "win32" else 0  # CREATE_NO_WINDOW
    result = subprocess.run(cmd, creationflags=flags, timeout=30)
    return result.returncode == 0


class _WidgetHandler(BaseHTTPRequestHandler):
    # Wired in by tray_widget.main() after TrayApp is constructed.
    _prefs_getter    = staticmethod(lambda: {})
    _toggle_callback = staticmethod(lambda key: None)
    _action_callback = staticmethod(lambda name: None)

    def do_GET(self):
        from urllib.parse import urlparse
        path = urlparse(self.path).path
        if path == "/state":
            body = STATE_FILE.read_bytes() if STATE_FILE.exists() else b"{}"
            self._respond(body, "application/json")
        elif path == "/prefs":
            self._respond(json.dumps(_WidgetHandler._prefs_getter()).encode(), "application/json")
        elif path == "/sessions":
            self._respond(json.dumps(_list_sessions()).encode(), "application/json")
        elif path == "/chart_image":
            if CHART_FILE.exists():
                self._respond(CHART_FILE.read_bytes(), "image/png")
            else:
                self.send_response(404)
                self.end_headers()
        else:
            body = WIDGET_HTML.read_bytes() if WIDGET_HTML.exists() else b"<h1>widget.html not found</h1>"
            self._respond(body, "text/html")

    def do_POST(self):
        from urllib.parse import urlparse, parse_qs
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        if parsed.path == "/toggle":
            key = params.get("key", [None])[0]
            if key:
                _WidgetHandler._toggle_callback(key)
            self._respond(b'{"ok":true}', "application/json")
        elif parsed.path == "/action":
            name = params.get("name", [None])[0]
            if name:
                _WidgetHandler._action_callback(name)
            self._respond(b'{"ok":true}', "application/json")
        elif parsed.path == "/set_poll":
            # Dashboard "Check usage every" control. Writes poll_interval_minutes
            # to the per-user config; _liveness_interval_secs() re-reads config
            # each poll, so the change takes effect without a restart (an env
            # override, if set, still wins — by design).
            raw = params.get("minutes", [None])[0]
            try:
                minutes = float(raw)
            except (TypeError, ValueError):
                self._respond(b'{"ok":false}', "application/json")
                return
            _write_config_value("poll_interval_minutes", minutes)
            self._respond(b'{"ok":true}', "application/json")
        elif parsed.path == "/set_liveness_pcts":
            # Comma-separated one-shot liveness thresholds, e.g. "5,10,95".
            # Parsed/clamped to 1..99; an empty result is rejected so the user
            # can't accidentally wipe all safety pokes (default still applies
            # via _liveness_oneshot_pcts when the key is absent).
            raw = params.get("pcts", [""])[0]
            pcts = sorted(_parse_pct_list(raw))
            if not pcts:
                self._respond(b'{"ok":false}', "application/json")
                return
            _write_config_value("liveness_oneshot_pcts", pcts)
            self._respond(json.dumps({"ok": True, "pcts": pcts}).encode(),
                          "application/json")
        elif parsed.path == "/set_liveness_delta":
            raw = params.get("pct", [None])[0]
            try:
                val = max(1.0, float(raw))
            except (TypeError, ValueError):
                self._respond(b'{"ok":false}', "application/json")
                return
            _write_config_value("liveness_delta_pct", val)
            self._respond(b'{"ok":true}', "application/json")
        elif parsed.path == "/chart":
            at = params.get("at", [None])[0]
            ok = _run_accuracy_chart(at)
            self._respond(json.dumps({"ok": ok}).encode(), "application/json")
        else:
            self.send_response(404)
            self.end_headers()

    def _respond(self, body: bytes, ctype: str):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass


def _port_is_free(port: int) -> bool:
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def startup_sanity_check() -> dict:
    """Print clear, human-readable diagnostics at startup so a confused user
    gets a readable reason instead of a frozen icon  [G1].

    Reports: which browser cookies were found, whether the session looks
    logged in, whether an org_id resolved, and whether the widget port is
    free. Returns a dict of the findings (also handy for tests)."""
    print("=" * 60)
    print("Claude Usage Widget - startup check")
    print("=" * 60)

    cookie_dict = _load_browser_cookies()
    logged_in = bool(cookie_dict) and _looks_logged_in(cookie_dict)

    if not cookie_dict:
        print("[X] Cookies: none found in Firefox. "
              "Open https://claude.ai/ in Firefox and log in.")
    else:
        print(f"[OK] Cookies: found in Firefox "
              f"({len(cookie_dict)} claude.ai cookies).")
        if not logged_in:
            print("[X] Login: cookies present but no active session - "
                  "log in at https://claude.ai/.")
        else:
            print("[OK] Login: session cookie present.")

    org_id = _resolve_org_id(cookie_dict if logged_in else None,
                             allow_prompt=False)
    if org_id:
        src = ("env/config" if _configured_org_id() else "auto-discovered")
        print(f"[OK] Org id: {org_id} ({src}).")
    else:
        print("[X] Org id: not configured and could not auto-discover. "
              "Will prompt on first run, or set CLAUDE_ORG_ID / config.json.")

    if _port_is_free(SERVER_PORT):
        print(f"[OK] Port: {SERVER_PORT} is free.")
    else:
        print(f"[X] Port: {SERVER_PORT} is in use - another widget instance "
              f"may already be running.")

    print(f"     Config:  {_config_path()} (fallback: {_bundled_config_path()})")
    print(f"     State:   {STATE_FILE}")
    print("=" * 60)
    return {
        "cookies": bool(cookie_dict),
        "logged_in": logged_in,
        "org_id": org_id,
        "port_free": _port_is_free(SERVER_PORT),
    }


def main():
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    startup_sanity_check()

    server = HTTPServer(("127.0.0.1", SERVER_PORT), _WidgetHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"Widget at http://127.0.0.1:{SERVER_PORT}/")

    handler  = TranscriptHandler()
    observer = Observer()
    observer.schedule(handler, str(PROJECTS_DIR), recursive=True)
    observer.start()
    handler.start_watchdog()
    print(f"Watching {PROJECTS_DIR}")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()
    observer.join()


if __name__ == "__main__":
    main()
