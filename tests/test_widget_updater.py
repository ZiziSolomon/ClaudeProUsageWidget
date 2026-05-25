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
import importlib
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
