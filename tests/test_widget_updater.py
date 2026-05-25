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
        assert after > before


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

