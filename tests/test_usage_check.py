"""
Unit tests for usage_check._fmt_eta.

Pure function — no network, no browser, no config required.

_fmt_eta takes a reset *datetime* (or None) and renders the delta to now:
  None        -> "?"
  in the past -> "now"
  otherwise   -> "{h}h {m}m" if there are whole hours, else "{m}m"

Cushions of a few seconds are added above each minute/hour boundary so the
sub-second wall-clock elapsed inside _fmt_eta can't drop a unit and flake.
"""

import os
import sys
from datetime import datetime, timedelta, timezone

# usage_check has no import-time side-effects beyond stdlib, so a plain import
# is safe everywhere.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from usage_check import _fmt_eta


def _in(seconds: float) -> datetime:
    """A UTC reset time `seconds` from now."""
    return datetime.now(timezone.utc) + timedelta(seconds=seconds)


class TestFmtEta:
    def test_none_returns_question_mark(self):
        assert _fmt_eta(None) == "?"

    def test_past_returns_now(self):
        assert _fmt_eta(_in(-60)) == "now"

    def test_exactly_now_returns_now(self):
        assert _fmt_eta(_in(0)) == "now"

    def test_under_one_minute_is_zero_minutes(self):
        assert _fmt_eta(_in(30)) == "0m"

    def test_minutes_only(self):
        assert _fmt_eta(_in(95)) == "1m"
        assert _fmt_eta(_in(59 * 60 + 5)) == "59m"

    def test_one_hour(self):
        assert _fmt_eta(_in(3600 + 5)) == "1h 0m"

    def test_hours_and_minutes(self):
        assert _fmt_eta(_in(3660 + 5)) == "1h 1m"
        assert _fmt_eta(_in(2 * 3600 + 2 * 60 + 5)) == "2h 2m"

    def test_large_value(self):
        assert _fmt_eta(_in(24 * 3600 + 5)) == "24h 0m"
