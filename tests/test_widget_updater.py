"""
Unit tests for widget_updater._parse_session and widget_updater._parse_weekly.

Import-time limitation: widget_updater.py calls _load_org_id() at module
level (to build USAGE_URL), so importing the module will raise SystemExit
unless CLAUDE_ORG_ID is set or config.json exists. We set the env var to a
dummy UUID before importing. This is a known issue tracked for the refactor
that moves USAGE_URL construction into a function rather than a module-level
constant (so the import stays clean everywhere — including on CI and macOS/Linux
where config.json won't exist).

The tests themselves exercise pure functions that do no I/O and do not use
the org ID at all.
"""

import os
import sys
import json
import importlib
from datetime import datetime, timedelta, timezone
import pytest

# Set the env var BEFORE the import so _load_org_id() succeeds at module load.
os.environ.setdefault("CLAUDE_ORG_ID", "00000000-0000-0000-0000-000000000000")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# widget_updater also imports watchdog, browser_cookie3, curl_cffi.
# On CI these are installed via requirements.txt; locally they must be too.
try:
    import widget_updater
    _IMPORT_OK = True
    _IMPORT_ERROR = None
except Exception as e:
    _IMPORT_OK = False
    _IMPORT_ERROR = str(e)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_raw(five_pct=50.0, five_resets="2099-01-01T12:00:00+00:00",
              seven_pct=30.0, seven_resets="2099-01-07T00:00:00+00:00") -> dict:
    """Build a minimal API response dict matching what claude.ai returns."""
    return {
        "five_hour": {
            "utilization": five_pct,
            "resets_at":   five_resets,
        },
        "seven_day": {
            "utilization": seven_pct,
            "resets_at":   seven_resets,
        },
    }


# ---------------------------------------------------------------------------
# Skip everything if the import failed (e.g. missing native deps)
# ---------------------------------------------------------------------------

pytestmark = pytest.mark.skipif(
    not _IMPORT_OK,
    reason=f"widget_updater could not be imported: {_IMPORT_ERROR}",
)


# ---------------------------------------------------------------------------
# _parse_session
# ---------------------------------------------------------------------------

class TestParseSession:
    def test_normal(self):
        raw = _make_raw(five_pct=42.0, five_resets="2099-06-01T10:00:00+00:00")
        start, end, pct = widget_updater._parse_session(raw)
        assert pct == 42.0
        assert end is not None
        assert start is not None
        # session window is exactly SESSION_HOURS long
        delta = (end - start).total_seconds() / 3600
        assert delta == widget_updater.SESSION_HOURS

    def test_zero_pct(self):
        raw = _make_raw(five_pct=0.0)
        start, end, pct = widget_updater._parse_session(raw)
        # utilization=0 is a valid value — we still parse timestamps
        assert pct == 0.0

    def test_missing_resets_at(self):
        raw = {"five_hour": {"utilization": 50.0}, "seven_day": {}}
        start, end, pct = widget_updater._parse_session(raw)
        assert start is None
        assert end is None
        assert pct is None

    def test_missing_utilization(self):
        raw = {"five_hour": {"resets_at": "2099-01-01T00:00:00+00:00"}, "seven_day": {}}
        start, end, pct = widget_updater._parse_session(raw)
        assert pct is None

    def test_none_input(self):
        start, end, pct = widget_updater._parse_session(None)
        assert start is None
        assert end is None
        assert pct is None

    def test_empty_dict(self):
        start, end, pct = widget_updater._parse_session({})
        assert start is None

    def test_100_pct(self):
        raw = _make_raw(five_pct=100.0)
        _, _, pct = widget_updater._parse_session(raw)
        assert pct == 100.0


# ---------------------------------------------------------------------------
# _parse_weekly
# ---------------------------------------------------------------------------

class TestParseWeekly:
    def test_normal(self):
        raw = _make_raw(seven_pct=25.5, seven_resets="2099-06-07T00:00:00+00:00")
        pct, end = widget_updater._parse_weekly(raw)
        assert pct == 25.5
        assert end is not None

    def test_missing_pct(self):
        raw = {"five_hour": {}, "seven_day": {"resets_at": "2099-01-07T00:00:00+00:00"}}
        pct, end = widget_updater._parse_weekly(raw)
        assert pct is None
        assert end is None

    def test_missing_resets_at(self):
        raw = {"five_hour": {}, "seven_day": {"utilization": 40.0}}
        pct, end = widget_updater._parse_weekly(raw)
        assert pct == 40.0
        assert end is None   # resets_at absent → None

    def test_none_input(self):
        pct, end = widget_updater._parse_weekly(None)
        assert pct is None
        assert end is None

    def test_empty_dict(self):
        pct, end = widget_updater._parse_weekly({})
        assert pct is None

    def test_zero_pct(self):
        raw = _make_raw(seven_pct=0.0)
        pct, end = widget_updater._parse_weekly(raw)
        assert pct == 0.0


# ---------------------------------------------------------------------------
# Local estimate between API calibrations
#
# These guard the bug where session_pct was only ever written by an API fetch,
# so the displayed % froze at the last calibration while local token usage kept
# climbing (it sat "stuck around 40" when the truth was ~58). The contract:
#   1. a calibration records implied_session_budget,
#   2. the estimate extrapolates pct from the live token count + that budget,
#   3. feeding more tokens through on_modified RAISES session_pct without an
#      API call.
# ---------------------------------------------------------------------------

def _assistant_line(msg_id: str, inp: int, out: int, ts: datetime) -> str:
    return json.dumps({
        "type": "assistant",
        "timestamp": ts.isoformat(),
        "message": {
            "id": msg_id,
            "model": "claude-opus-4-7",
            "usage": {"input_tokens": inp, "output_tokens": out},
        },
    })


class TestCalibrationRecordsBudget:
    def test_append_calibration_stores_implied_budget(self):
        state = widget_updater._empty_state(
            datetime(2099, 1, 1, tzinfo=timezone.utc))
        state["input_tokens"] = 40000
        state["output_tokens"] = 60000  # 100k io total
        # 100k tokens reported as 50% => implied budget 200k.
        widget_updater._append_calibration(state, 50.0, datetime.now(timezone.utc))
        assert state["implied_session_budget"] == 200000

    def test_zero_pct_does_not_set_budget(self):
        state = widget_updater._empty_state(
            datetime(2099, 1, 1, tzinfo=timezone.utc))
        state["input_tokens"] = 0
        state["output_tokens"] = 0
        widget_updater._append_calibration(state, 0.0, datetime.now(timezone.utc))
        # No divide-by-zero, and no bogus budget recorded.
        assert not state.get("implied_session_budget")

    def test_below_floor_does_not_set_budget(self):
        # A reading below CALIBRATION_PCT_FLOOR is too rounding-unstable to trust.
        state = widget_updater._empty_state(
            datetime(2099, 1, 1, tzinfo=timezone.utc))
        state["input_tokens"] = 1000
        state["output_tokens"] = 1000
        pct = widget_updater.CALIBRATION_PCT_FLOOR - 1
        widget_updater._append_calibration(state, float(pct), datetime.now(timezone.utc))
        assert not state.get("implied_session_budget")

    def test_at_floor_sets_budget(self):
        # At the floor exactly we DO trust it: 2k io at floor% => 2k/(floor/100).
        state = widget_updater._empty_state(
            datetime(2099, 1, 1, tzinfo=timezone.utc))
        state["input_tokens"] = 1000
        state["output_tokens"] = 1000
        floor = widget_updater.CALIBRATION_PCT_FLOOR
        widget_updater._append_calibration(state, float(floor), datetime.now(timezone.utc))
        assert state["implied_session_budget"] == round(2000 / (floor / 100))


class TestLocalEstimate:
    def test_none_without_budget(self):
        state = {"input_tokens": 5000, "output_tokens": 5000}
        assert widget_updater._estimate_session_pct(state) is None

    def test_extrapolates_from_tokens(self):
        state = {"input_tokens": 30000, "output_tokens": 30000,
                 "implied_session_budget": 200000}  # 60k / 200k = 30%
        assert widget_updater._estimate_session_pct(state) == 30

    def test_rises_as_tokens_grow(self):
        state = {"input_tokens": 50000, "output_tokens": 50000,
                 "implied_session_budget": 200000}
        before = widget_updater._estimate_session_pct(state)  # 50%
        state["output_tokens"] += 40000                       # +40k => 70%
        after = widget_updater._estimate_session_pct(state)
        assert before == 50
        assert after == 70

    def test_clamps_at_100(self):
        # A too-small budget (locked early or contaminated) must not overshoot.
        # 300k io against a 200k budget would be 150% unclamped.
        state = {"input_tokens": 150000, "output_tokens": 150000,
                 "implied_session_budget": 200000}
        assert widget_updater._estimate_session_pct(state) == 100


class TestEmergencyRecal:
    """When the local estimate goes somewhere that proves the budget is wrong
    (pegged at the 100% clamp, or sprinted FORCE_RECAL_GAP_PP past the last API
    truth), on_modified spends one cooldown-gated API call to re-anchor."""

    def _make_handler(self, monkeypatch):
        monkeypatch.setattr(widget_updater.TranscriptHandler, "_startup",
                            lambda self: None)
        monkeypatch.setattr(widget_updater, "_save_state", lambda *a, **k: None)
        return widget_updater.TranscriptHandler()

    def test_clamp_hit_is_suspect(self, monkeypatch):
        h = self._make_handler(monkeypatch)
        now = datetime.now(timezone.utc)
        h.state["implied_session_budget"] = 100000
        h.state["input_tokens"] = 80000
        h.state["output_tokens"] = 40000   # 120k/100k = 120% unclamped
        h.last_api_pct = 40
        assert h._estimate_is_suspect(100, now) is True

    def test_big_gap_is_suspect(self, monkeypatch):
        h = self._make_handler(monkeypatch)
        now = datetime.now(timezone.utc)
        h.state["implied_session_budget"] = 200000
        h.state["input_tokens"] = 40000
        h.state["output_tokens"] = 30000   # 35%, not clamped
        h.last_api_pct = 5                 # 30pp ahead of truth >= 25
        assert h._estimate_is_suspect(35, now) is True

    def test_small_gap_not_suspect(self, monkeypatch):
        h = self._make_handler(monkeypatch)
        now = datetime.now(timezone.utc)
        h.state["implied_session_budget"] = 200000
        h.state["input_tokens"] = 12000
        h.state["output_tokens"] = 12000   # 12%
        h.last_api_pct = 10                # 2pp gap < FORCE_RECAL_GAP_PP, not clamped
        assert h._estimate_is_suspect(12, now) is False

    def test_cooldown_suppresses(self, monkeypatch):
        h = self._make_handler(monkeypatch)
        now = datetime.now(timezone.utc)
        h.state["implied_session_budget"] = 100000
        h.state["input_tokens"] = 120000
        h.state["output_tokens"] = 0       # clamped
        h.last_api_pct = 40
        h.last_forced_recal = now - timedelta(seconds=10)  # inside cooldown
        assert h._estimate_is_suspect(100, now) is False

    def test_no_budget_not_suspect(self, monkeypatch):
        h = self._make_handler(monkeypatch)
        now = datetime.now(timezone.utc)
        h.state.pop("implied_session_budget", None)
        assert h._estimate_is_suspect(100, now) is False

    def test_on_modified_forces_recal_when_clamped(self, monkeypatch, tmp_path):
        h = self._make_handler(monkeypatch)
        now = datetime.now(timezone.utc)
        h.session_start = now - timedelta(hours=1)
        h.session_end   = now + timedelta(hours=4)
        h.last_calibrated = now            # normal calibrate won't fire
        h.last_liveness   = now            # liveness ping won't fire
        h.state["calibration_calls_remaining"] = 0
        h.state["implied_session_budget"] = 50000   # tiny => overshoot
        h.state["input_tokens"]  = 30000
        h.state["output_tokens"] = 20000
        h.last_api_pct = 40

        calls = []
        monkeypatch.setattr(h, "_maybe_calibrate",
                            lambda force=False: calls.append(force) or False)

        jsonl = tmp_path / "s.jsonl"
        jsonl.write_text(_assistant_line("m1", 10000, 10000, now) + "\n",
                         encoding="utf-8")

        class _Evt:
            is_directory = False
            src_path = str(jsonl)

        h.on_modified(_Evt())
        # 70k/50k clamps to 100 => suspect => a forced recal was attempted.
        assert True in calls


class TestAdoptApiPct:
    """A freshly-fetched API pct is always adopted for display, but only
    re-derives the budget when it disagrees with what we showed by more than
    RECAL_DISCREPANCY_PP (or there's no budget yet) and clears the pct floor."""

    def _make_handler(self, monkeypatch):
        monkeypatch.setattr(widget_updater.TranscriptHandler, "_startup",
                            lambda self: None)
        monkeypatch.setattr(widget_updater, "_save_state", lambda *a, **k: None)
        return widget_updater.TranscriptHandler()

    def _capture(self, monkeypatch):
        # Stub _append_calibration so the test neither writes to the real
        # calibration log nor depends on it -- just records the gate decision
        # and applies the budget the same way the real one would.
        calls = {}
        def fake(state, pct, when, update_budget=True):
            calls["update_budget"] = update_budget
            if update_budget and pct >= widget_updater.CALIBRATION_PCT_FLOOR:
                io = state["input_tokens"] + state["output_tokens"]
                state["implied_session_budget"] = round(io / (pct / 100))
        monkeypatch.setattr(widget_updater, "_append_calibration", fake)
        # Keep the recalibrate path off the real disk: the rescan is covered by
        # TestWatcherStuck; here we only care about the budget gate decision.
        monkeypatch.setattr(widget_updater, "full_scan", lambda *a, **k: None)
        return calls

    def test_big_diff_recalibrates(self, monkeypatch):
        h = self._make_handler(monkeypatch)
        calls = self._capture(monkeypatch)
        h.session_pct = 50
        h.state["implied_session_budget"] = 200000
        h.state["input_tokens"], h.state["output_tokens"] = 40000, 0
        assert h._adopt_api_pct(80, datetime.now(timezone.utc)) is True
        assert calls["update_budget"] is True
        assert h.session_pct == 80 and h.last_api_pct == 80
        assert h.state["implied_session_budget"] == 50000   # 40k / (80/100)

    def test_small_diff_keeps_budget(self, monkeypatch):
        h = self._make_handler(monkeypatch)
        calls = self._capture(monkeypatch)
        h.session_pct = 50
        h.state["implied_session_budget"] = 200000
        h.state["input_tokens"], h.state["output_tokens"] = 40000, 0
        assert h._adopt_api_pct(53, datetime.now(timezone.utc)) is False  # 3pp
        assert calls["update_budget"] is False
        assert h.session_pct == 53                            # display adopts
        assert h.state["implied_session_budget"] == 200000    # budget untouched

    def test_no_budget_recalibrates(self, monkeypatch):
        h = self._make_handler(monkeypatch)
        calls = self._capture(monkeypatch)
        h.session_pct = None
        h.state.pop("implied_session_budget", None)
        h.state["input_tokens"], h.state["output_tokens"] = 20000, 0
        assert h._adopt_api_pct(10, datetime.now(timezone.utc)) is True
        assert calls["update_budget"] is True

    def test_below_floor_no_recalibrate(self, monkeypatch):
        h = self._make_handler(monkeypatch)
        calls = self._capture(monkeypatch)
        h.session_pct = 50
        h.state["implied_session_budget"] = 200000
        assert h._adopt_api_pct(3, datetime.now(timezone.utc)) is False  # < floor
        assert calls["update_budget"] is False
        assert h.session_pct == 3                             # display still adopts


class TestWatcherStuck:
    """On recalibration we re-scan the folder from disk. Tokens recovered that
    no on_modified event reported mean the watcher missed events; if it's also
    been silent for WATCHER_STUCK_SILENCE_SECS, it's stuck -> prompt restart.
    Off-laptop usage leaves no local-disk tokens, so it never trips this."""

    def _make_handler(self, monkeypatch):
        monkeypatch.setattr(widget_updater.TranscriptHandler, "_startup",
                            lambda self: None)
        monkeypatch.setattr(widget_updater, "_save_state", lambda *a, **k: None)
        h = widget_updater.TranscriptHandler()
        now = datetime.now(timezone.utc)
        h.session_start = now - timedelta(hours=1)
        h.session_end   = now + timedelta(hours=4)
        h.state["input_tokens"], h.state["output_tokens"] = 10000, 0
        return h

    def _scan_adds(self, monkeypatch, n):
        monkeypatch.setattr(widget_updater, "full_scan",
                            lambda state, s, e: state.__setitem__(
                                "input_tokens", state["input_tokens"] + n))

    def _no_real_timer(self, monkeypatch):
        # Capture the deferred re-check instead of letting a real 5s timer fire.
        armed = {}
        class _FakeTimer:
            def __init__(self, delay, fn, args=()):
                armed["delay"], armed["fn"], armed["args"] = delay, fn, args
            def start(self): pass
            def cancel(self): pass
        monkeypatch.setattr(widget_updater.threading, "Timer", _FakeTimer)
        return armed

    def test_silent_with_missed_tokens_arms_then_warns(self, monkeypatch):
        h = self._make_handler(monkeypatch)
        now = datetime.now(timezone.utc)
        h.last_event_at = now - timedelta(
            seconds=widget_updater.WATCHER_STUCK_SILENCE_SECS + 60)
        self._scan_adds(monkeypatch, 5000)
        armed = self._no_real_timer(monkeypatch)
        warned = []
        h.on_disconnect = lambda msg: warned.append(msg)

        assert h._rescan_and_check_watcher(now) == 5000   # healed
        assert armed["delay"] == widget_updater.WATCHER_STUCK_RECHECK_SECS
        assert not warned                                 # not yet -- deferred
        # Grace window elapses with no new ping (last_event_at unchanged):
        armed["fn"](*armed["args"])
        assert warned                                     # now it warns

    def test_ping_in_grace_window_cancels_warning(self, monkeypatch):
        h = self._make_handler(monkeypatch)
        now = datetime.now(timezone.utc)
        h.last_event_at = now - timedelta(
            seconds=widget_updater.WATCHER_STUCK_SILENCE_SECS + 60)
        self._scan_adds(monkeypatch, 5000)
        armed = self._no_real_timer(monkeypatch)
        warned = []
        h.on_disconnect = lambda msg: warned.append(msg)

        h._rescan_and_check_watcher(now)
        # A transcript event lands during the 5s grace window:
        h.last_event_at = datetime.now(timezone.utc)
        armed["fn"](*armed["args"])
        assert not warned                                 # watcher was alive

    def test_recent_events_no_recheck(self, monkeypatch):
        h = self._make_handler(monkeypatch)
        now = datetime.now(timezone.utc)
        h.last_event_at = now - timedelta(seconds=30)     # events flowing
        self._scan_adds(monkeypatch, 5000)
        armed = self._no_real_timer(monkeypatch)
        assert h._rescan_and_check_watcher(now) == 5000   # still healed
        assert not armed                                  # no re-check armed

    def test_no_missed_tokens_no_recheck(self, monkeypatch):
        h = self._make_handler(monkeypatch)
        now = datetime.now(timezone.utc)
        h.last_event_at = now - timedelta(
            seconds=widget_updater.WATCHER_STUCK_SILENCE_SECS + 60)
        self._scan_adds(monkeypatch, 0)
        armed = self._no_real_timer(monkeypatch)
        assert h._rescan_and_check_watcher(now) == 0
        assert not armed

    def test_never_saw_event_no_recheck(self, monkeypatch):
        h = self._make_handler(monkeypatch)
        now = datetime.now(timezone.utc)
        h.last_event_at = None
        self._scan_adds(monkeypatch, 5000)
        armed = self._no_real_timer(monkeypatch)
        assert h._rescan_and_check_watcher(now) == 5000   # healed silently
        assert not armed


class TestOnModifiedAdvancesPct:
    """Integration guard: on_modified must move session_pct from local token
    growth when no calibration fires. This is the exact path that froze."""

    def _make_handler(self, monkeypatch):
        # __init__ calls _startup() (network); neuter it. Also stub _save_state
        # so the test never writes to the real %LOCALAPPDATA% store.
        monkeypatch.setattr(widget_updater.TranscriptHandler, "_startup",
                            lambda self: None)
        monkeypatch.setattr(widget_updater, "_save_state",
                            lambda *a, **k: None)
        return widget_updater.TranscriptHandler()

    def test_pct_advances_without_api_call(self, monkeypatch, tmp_path):
        h = self._make_handler(monkeypatch)
        now = datetime.now(timezone.utc)
        # Live session window, calibrated already, budget known, budget spent so
        # _maybe_calibrate won't fire; liveness recently done so it won't ping.
        h.session_start = now - timedelta(hours=1)
        h.session_end = now + timedelta(hours=4)
        h.last_calibrated = now
        h.last_liveness = now
        h.state["calibration_calls_remaining"] = 0
        h.state["implied_session_budget"] = 200000
        h.state["input_tokens"] = 40000
        h.state["output_tokens"] = 60000   # 100k => 50%
        h.session_pct = 50.0

        # Fail loudly if any network fetch is attempted on this path.
        monkeypatch.setattr(widget_updater, "_fetch_usage_status",
                            lambda: pytest.fail("on_modified hit the API"))

        jsonl = tmp_path / "session.jsonl"
        jsonl.write_text(
            _assistant_line("msg_new", 20000, 20000, now) + "\n",
            encoding="utf-8")

        class _Evt:
            is_directory = False
            src_path = str(jsonl)

        h.on_modified(_Evt())

        # 100k + 40k = 140k / 200k = 70%. Must have RISEN, not frozen at 50.
        assert h.session_pct == 70


class TestSessionRollover:
    """Time-based session expiry must reset local state WITHOUT a file event or
    an API call, so a window that ends while the widget is idle or closed can't
    keep showing the dead session's % (or fire a false 'stuck' discrepancy
    against it when the API next reports the fresh window at 0%).

    Contract: caught live (within ROLLOVER_GRACE_SECS of the boundary) => fresh
    0%; noticed late => pending '--' (None) until the API/transcript confirms.
    """

    def _make_handler(self, monkeypatch):
        # __init__ calls _startup() (network) and _save_state (writes to the
        # real store); neuter both.
        monkeypatch.setattr(widget_updater.TranscriptHandler, "_startup",
                            lambda self: None)
        monkeypatch.setattr(widget_updater, "_save_state", lambda *a, **k: None)
        return widget_updater.TranscriptHandler()

    def _expired_handler(self, monkeypatch, end):
        """Handler holding a non-trivial reading for a window ending at `end`."""
        h = self._make_handler(monkeypatch)
        h.session_start = end - timedelta(hours=widget_updater.SESSION_HOURS)
        h.session_end = end
        h.session_pct = 74
        h.last_calibrated = datetime.now(timezone.utc)
        h.state["implied_session_budget"] = 100000
        h.state["input_tokens"] = 60000
        h.state["output_tokens"] = 80000
        h.state["seen_ids"] = {"msg_old"}
        return h

    def test_no_rollover_before_expiry(self, monkeypatch):
        now = datetime.now(timezone.utc)
        h = self._expired_handler(monkeypatch, now + timedelta(hours=1))
        assert h._roll_over_if_expired(now) is False
        assert h.session_pct == 74          # untouched
        assert h.session_end is not None

    def test_caught_live_snaps_to_zero(self, monkeypatch):
        now = datetime.now(timezone.utc)
        # Boundary 5s ago — inside the grace window => we were watching.
        h = self._expired_handler(monkeypatch, now - timedelta(seconds=5))
        assert h._roll_over_if_expired(now) is True
        assert h.session_pct == 0
        assert h.session_start is None and h.session_end is None
        # stale tally + budget cleared so the estimator can't extrapolate the
        # dead window
        assert h.state["input_tokens"] == 0 and h.state["output_tokens"] == 0
        assert h.state["seen_ids"] == set()
        assert not h.state.get("implied_session_budget")
        # calibration anchor dropped so the next calibrate re-anchors at once
        assert h.last_calibrated is None

    def test_noticed_late_blanks_to_pending(self, monkeypatch):
        now = datetime.now(timezone.utc)
        # Boundary an hour ago — well past grace => widget wasn't watching.
        h = self._expired_handler(monkeypatch, now - timedelta(hours=1))
        assert h._roll_over_if_expired(now) is True
        assert h.session_pct is None        # '--', not a fabricated 0
        assert h.session_start is None

    def test_grace_boundary_is_inclusive(self, monkeypatch):
        now = datetime.now(timezone.utc)
        end = now - timedelta(seconds=widget_updater.ROLLOVER_GRACE_SECS)
        h = self._expired_handler(monkeypatch, end)   # exactly at the grace edge
        assert h._roll_over_if_expired(now) is True
        assert h.session_pct == 0           # still counts as live


@pytest.mark.skipif(not _IMPORT_OK, reason=f"import failed: {_IMPORT_ERROR}")
class TestLivenessInterval:
    """The poll interval is user-settable (env > config > default) and floored
    so a misconfiguration can't hammer claude.ai."""

    def test_default_is_20_min(self, monkeypatch):
        monkeypatch.delenv("CLAUDE_POLL_INTERVAL_MINUTES", raising=False)
        monkeypatch.setattr(widget_updater, "_read_config", lambda: {})
        assert widget_updater._liveness_interval_secs() == 1200

    def test_config_override(self, monkeypatch):
        monkeypatch.delenv("CLAUDE_POLL_INTERVAL_MINUTES", raising=False)
        monkeypatch.setattr(widget_updater, "_read_config",
                            lambda: {"poll_interval_minutes": 5})
        assert widget_updater._liveness_interval_secs() == 300

    def test_env_beats_config(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_POLL_INTERVAL_MINUTES", "30")
        monkeypatch.setattr(widget_updater, "_read_config",
                            lambda: {"poll_interval_minutes": 5})
        assert widget_updater._liveness_interval_secs() == 1800

    def test_floored_at_minimum(self, monkeypatch):
        # A too-aggressive value is clamped up to the floor, not honoured.
        monkeypatch.setenv("CLAUDE_POLL_INTERVAL_MINUTES", "0.1")  # 6s
        monkeypatch.setattr(widget_updater, "_read_config", lambda: {})
        assert widget_updater._liveness_interval_secs() == widget_updater.LIVENESS_MIN_SECS

    def test_garbage_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_POLL_INTERVAL_MINUTES", "soon")
        monkeypatch.setattr(widget_updater, "_read_config", lambda: {})
        assert widget_updater._liveness_interval_secs() == 1200

