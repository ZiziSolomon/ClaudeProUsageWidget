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
import unittest.mock as mock
from types import SimpleNamespace
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
# Global isolation from the real user data files
# ---------------------------------------------------------------------------
# Any test that builds a TranscriptHandler or calls _append_calibration will,
# unless redirected, read/write the user's real %LOCALAPPDATA% state and
# calibration logs. That leaked synthetic rows into calibration.jsonl (e.g. a
# (pct=7, io=12000) liveness sample from TestLivenessTriggers) which then
# showed up as a bogus mid-session dip in the accuracy chart. Pin BOTH files
# to tmp for every test, module-wide, so no single class can forget the guard.
#
# Redirecting STATE_FILE to a non-existent path also means _load_state() falls
# back to _empty_state(), so handlers start with an empty by_model instead of
# inheriting the live session's token tallies. Per-test overrides (e.g.
# TestPriorBudgetMedian pointing CALIBRATION_FILE at its own tmp file) still
# win because their monkeypatch.setattr runs after this fixture.
@pytest.fixture(autouse=True)
def _isolate_user_data_files(tmp_path, monkeypatch):
    if not _IMPORT_OK:
        return
    monkeypatch.setattr(widget_updater, "STATE_FILE",
                        tmp_path / "widget_state.json")
    monkeypatch.setattr(widget_updater, "CALIBRATION_FILE",
                        tmp_path / "calibration.jsonl")


@pytest.fixture
def make_handler(monkeypatch):
    """Canonical factory for TranscriptHandler in tests.

    Stubs all external I/O by default. Raise-on-unmocked network means tests
    that call the API must explicitly provide a return value — drift can't hide
    behind a silent None. Pass stub_maybe_liveness=False for classes that test
    _maybe_liveness directly. h._io exposes the mock objects for assertions.

    The io attribute names (fetch_usage, write_state, scan_projects, …) are
    intentionally the same names a future IOAdapter class would use, so the
    test fakes can be reused as-is when that refactor happens.
    """
    W = widget_updater

    def _make(*, stub_maybe_liveness=True):
        monkeypatch.setattr(W.TranscriptHandler, "_startup", lambda self: None)
        if stub_maybe_liveness:
            monkeypatch.setattr(W.TranscriptHandler, "_maybe_liveness",
                                lambda self: None)

        io = SimpleNamespace(
            write_state=mock.Mock(),
            fetch_usage=mock.Mock(
                side_effect=AssertionError(
                    "unmocked _fetch_usage_status — pass fetch_usage=mock.Mock(...)"
                )
            ),
            scan_projects=mock.Mock(),
            append_calibration=mock.Mock(),
            read_config=mock.Mock(return_value={}),
        )
        monkeypatch.setattr(W, "_save_state", io.write_state)
        monkeypatch.setattr(W, "_fetch_usage_status", io.fetch_usage)
        monkeypatch.setattr(W, "full_scan", io.scan_projects)
        monkeypatch.setattr(W, "_append_calibration", io.append_calibration)
        monkeypatch.setattr(W, "_read_config", io.read_config)

        h = W.TranscriptHandler()
        h._io = io
        return h

    return _make


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
        state["output_tokens"] = 60000  # 100k raw io
        # Budget is back-derived from the WEIGHTED token count now, not raw io.
        expected = round(widget_updater._weighted_io(state) / 0.5)
        widget_updater._append_calibration(state, 50.0, datetime.now(timezone.utc))
        assert state["implied_session_budget"] == expected

    def test_zero_pct_does_not_set_budget(self):
        state = widget_updater._empty_state(
            datetime(2099, 1, 1, tzinfo=timezone.utc))
        state["input_tokens"] = 0
        state["output_tokens"] = 0
        widget_updater._append_calibration(state, 0.0, datetime.now(timezone.utc))
        # No divide-by-zero, and no bogus budget recorded.
        assert not state.get("implied_session_budget")

    def test_below_floor_sets_blended_budget(self):
        # Below CALIBRATION_PCT_FLOOR we no longer leave budget=None (which
        # left the display stuck at the last API pct for hours - exactly the
        # bug that landed v0.1.0 in trouble). Instead we synthesize a budget
        # from a live-reading midpoint X blended with the user's historical
        # median M. See _blended_sub_floor_budget for the math.
        # _isolate_calibration_file fixture ensures we don't read real history.
        state = widget_updater._empty_state(
            datetime(2099, 1, 1, tzinfo=timezone.utc))
        state["input_tokens"] = 1000
        state["output_tokens"] = 1000
        wio = widget_updater._weighted_io(state)   # weighted, not raw 2k
        pct = widget_updater.CALIBRATION_PCT_FLOOR - 1  # 4%
        widget_updater._append_calibration(state, float(pct),
                                           datetime.now(timezone.utc))
        # No prior history -> blend falls back to X alone:
        # round-to-nearest: pct=4 midpoint = 4% => X = wio/0.04.
        assert state["implied_session_budget"] == int(round(wio / 0.04))

    def test_at_floor_sets_budget(self):
        # At the floor exactly we DO trust it: 2k io at floor% => 2k/(floor/100).
        state = widget_updater._empty_state(
            datetime(2099, 1, 1, tzinfo=timezone.utc))
        state["input_tokens"] = 1000
        state["output_tokens"] = 1000
        wio = widget_updater._weighted_io(state)
        floor = widget_updater.CALIBRATION_PCT_FLOOR
        widget_updater._append_calibration(state, float(floor), datetime.now(timezone.utc))
        assert state["implied_session_budget"] == round(wio / (floor / 100))


class TestBlendedSubFloorBudget:
    """The sub-floor blend (X from live reading + M from history). Direct
    tests on the pure function so we can pin the math precisely without
    going through the calibration file dance."""

    def _x(self, total_io, pct):
        # Mirror the production midpoint: round-to-nearest means true pct ∈
        # [N-0.5, N+0.5), so midpoint = N (and 0.25 at pct=0, where w=0 anyway).
        midpoint = pct if pct >= 1 else 0.25
        return total_io / (midpoint / 100)

    def test_no_history_returns_x(self):
        # With no M to blend, we just get X back unchanged.
        total_io, pct = 2000, 4.0
        x = self._x(total_io, pct)
        b = widget_updater._blended_sub_floor_budget(total_io, pct, None)
        assert b == int(round(x))

    def test_weight_at_pct_zero(self):
        # pct=0 => w=0 => entirely prior (local gives no upper bound).
        total_io, pct = 500, 0.0
        m = 250000
        b = widget_updater._blended_sub_floor_budget(total_io, pct, m)
        # x = 500/0.005 = 100000; clamp: max(50000, 250000)=250000
        assert b == 250000

    def test_weight_at_pct_one(self):
        # round model: pct=1 => w = 1/(1+0.5) = 2/3 (interval half as wide as floor).
        total_io, pct = 1500, 1.0
        x = self._x(total_io, pct)              # 1500 / 0.01 = 150000
        m = 200000
        w = pct / (pct + 0.5)                   # 0.6667
        expected = w * x + (1 - w) * m
        b = widget_updater._blended_sub_floor_budget(total_io, pct, m)
        assert b == int(round(expected))

    def test_weight_at_pct_four(self):
        # round model: pct=4 (just under the floor) => w = 4/4.5 ≈ 0.889.
        total_io, pct = 8000, 4.0
        x = self._x(total_io, pct)              # 8000 / 0.04 = 200000
        m = 250000
        w = pct / (pct + 0.5)
        expected = w * x + (1 - w) * m
        b = widget_updater._blended_sub_floor_budget(total_io, pct, m)
        assert b == int(round(expected))

    def test_lower_clamp_when_m_too_small(self):
        # At pct=0 (w=0), the blend IS the prior. If the prior is absurdly
        # low the X/2 clamp provides a floor: budget ≥ 100*io (lb math).
        total_io, pct = 1000, 0.0
        x = self._x(total_io, pct)              # 1000 / 0.005 = 200000
        m = 1                                    # absurdly small
        b = widget_updater._blended_sub_floor_budget(total_io, pct, m)
        assert b == int(round(x * 0.5))         # 100000

    def test_no_upper_clamp_when_m_huge(self):
        # M >> X is the off-laptop-contamination signature (local total_io
        # undercounts, so X is too small). The blend should let M dominate
        # without a 2X ceiling yanking it back to wrong.
        total_io, pct = 500, 4.0
        x = self._x(total_io, pct)              # 500 / 0.04 = 12500
        m = 10_000_000                          # absurdly large vs X
        w = pct / (pct + 0.5)                   # round model: 0.889
        expected = w * x + (1 - w) * m
        b = widget_updater._blended_sub_floor_budget(total_io, pct, m)
        assert b == int(round(expected))
        # And confirm we did NOT clamp at 2X.
        assert b > x * 2

    def test_zero_tokens_returns_none(self):
        # Before any local tokens are counted there's no X to form, so we
        # can't produce a budget even if we have a prior median.
        b = widget_updater._blended_sub_floor_budget(0, 2.0, 250000)
        assert b is None

    def test_above_floor_returns_none(self):
        # This function only handles sub-floor; above-floor is the simple
        # direct back-derivation, handled in _append_calibration itself.
        b = widget_updater._blended_sub_floor_budget(
            10000, float(widget_updater.CALIBRATION_PCT_FLOOR), 250000)
        assert b is None


class TestPriorBudgetMedian:
    """Loads the median budget from calibration.jsonl. Window cap, ignores
    None/0 entries, missing file => None."""

    def test_missing_file_returns_none(self, tmp_path, monkeypatch):
        monkeypatch.setattr(widget_updater, "CALIBRATION_FILE",
                            tmp_path / "nope.jsonl")
        assert widget_updater._load_prior_budget_median() is None

    def test_ignores_null_budget_entries(self, tmp_path, monkeypatch):
        f = tmp_path / "calibration.jsonl"
        u = widget_updater.IO_UNIT
        f.write_text("\n".join([
            json.dumps({"implied_session_budget": None,   "budget_source": "live", "budget_unit": u}),
            json.dumps({"implied_session_budget": 200000, "budget_source": "live", "budget_unit": u}),
            json.dumps({"implied_session_budget": 300000, "budget_source": "live", "budget_unit": u}),
        ]) + "\n", encoding="utf-8")
        monkeypatch.setattr(widget_updater, "CALIBRATION_FILE", f)
        assert widget_updater._load_prior_budget_median() == 250000

    def test_window_caps_to_recent(self, tmp_path, monkeypatch):
        # Older absurd value should drop out of the window and not skew the
        # median.
        f = tmp_path / "calibration.jsonl"
        u = widget_updater.IO_UNIT
        lines = [json.dumps({"implied_session_budget": 999_999_999,
                              "budget_source": "live", "budget_unit": u})]
        lines += [json.dumps({"implied_session_budget": 200000,
                               "budget_source": "live", "budget_unit": u})
                  for _ in range(widget_updater.PRIOR_BUDGET_WINDOW)]
        f.write_text("\n".join(lines) + "\n", encoding="utf-8")
        monkeypatch.setattr(widget_updater, "CALIBRATION_FILE", f)
        assert widget_updater._load_prior_budget_median() == 200000

    def test_blended_entries_excluded(self, tmp_path, monkeypatch):
        # "blended" entries are partially derived from the prior itself —
        # including them creates a feedback loop. Only "live" entries count.
        f = tmp_path / "calibration.jsonl"
        u = widget_updater.IO_UNIT
        lines = [
            json.dumps({"implied_session_budget": 50000,
                        "budget_source": "blended", "budget_unit": u}),  # ignored: blended
            json.dumps({"implied_session_budget": 200000,
                        "budget_source": "live", "budget_unit": u}),
            json.dumps({"implied_session_budget": 300000,
                        "budget_source": "live", "budget_unit": u}),
        ]
        f.write_text("\n".join(lines) + "\n", encoding="utf-8")
        monkeypatch.setattr(widget_updater, "CALIBRATION_FILE", f)
        assert widget_updater._load_prior_budget_median() == 250000

    def test_no_live_entries_returns_none(self, tmp_path, monkeypatch):
        # If every entry is blended (e.g. widget never reached floor pct),
        # return None rather than a corrupted prior.
        f = tmp_path / "calibration.jsonl"
        lines = [json.dumps({"implied_session_budget": 100000,
                              "budget_source": "blended"})
                 for _ in range(5)]
        f.write_text("\n".join(lines) + "\n", encoding="utf-8")
        monkeypatch.setattr(widget_updater, "CALIBRATION_FILE", f)
        assert widget_updater._load_prior_budget_median() is None


class TestLocalEstimate:
    def test_none_without_budget(self):
        state = {"input_tokens": 5000, "output_tokens": 5000}
        assert widget_updater._estimate_session_pct(state) is None

    def test_extrapolates_from_tokens(self):
        state = {"input_tokens": 30000, "output_tokens": 30000}
        # budget = 4x the weighted token count => estimate 25% (weight-agnostic).
        state["implied_session_budget"] = widget_updater._weighted_io(state) * 4
        assert widget_updater._estimate_session_pct(state) == 25

    def test_rises_as_tokens_grow(self):
        state = {"input_tokens": 50000, "output_tokens": 50000}
        state["implied_session_budget"] = widget_updater._weighted_io(state) * 4  # 25%
        before = widget_updater._estimate_session_pct(state)
        state["output_tokens"] += 40000                       # more weighted tokens
        after = widget_updater._estimate_session_pct(state)
        assert before == 25
        assert after > before

    def test_clamps_at_100(self):
        # A too-small budget (locked early or contaminated) must not overshoot.
        # 300k io against a 200k budget would be 150% unclamped.
        state = {"input_tokens": 150000, "output_tokens": 150000,
                 "implied_session_budget": 200000}
        assert widget_updater._estimate_session_pct(state) == 100

    def test_anchor_snaps_to_api_pct(self):
        # With anchor_io == current io, estimate is anchor_pct + the rounding-bias
        # midpoint correction (no delta from local tokens). Under the current
        # round-to-nearest presumption that bias is 0.0, so it snaps to anchor_pct.
        state = {
            "input_tokens": 30000, "output_tokens": 30000,
            "implied_session_budget": 200000,
            "anchor_pct": 28.5,   # API said 28.5% at this weighted io
        }
        state["anchor_io"] = widget_updater._weighted_io(state)  # == current => delta 0
        expected = round(28.5 + widget_updater.API_PCT_FLOOR_BIAS_PP, 1)
        assert widget_updater._estimate_session_pct(state) == expected

    def test_anchor_delta_adds_from_anchor(self):
        # Tokens written after the anchor grow estimate from anchor_pct, not zero.
        # Budget kept large so the delta-from-anchor math is what's exercised,
        # not the 100% clamp (the calibrated output weight is several x input).
        state = {
            "input_tokens": 30000, "output_tokens": 30000,
            "implied_session_budget": 2000000,
            "anchor_pct": 28.5,
        }
        state["anchor_io"] = widget_updater._weighted_io(state)
        state["output_tokens"] += 20000  # delta is weighted (output weight applies)
        delta_w = 20000 * widget_updater.TOKEN_WEIGHTS["output"]
        # +0.5 floor-bias is added to the anchor level.
        bias = widget_updater.API_PCT_FLOOR_BIAS_PP
        expected = round(28.5 + bias + 100 * delta_w / 2000000, 1)
        assert widget_updater._estimate_session_pct(state) == expected

    def test_anchor_clamps_at_100(self):
        state = {
            "input_tokens": 200000, "output_tokens": 0,
            "implied_session_budget": 100000,
            "anchor_pct": 80.0,
            "anchor_io":  80000,
        }
        # 80.0 + 100 * (120000 / 100000) = 80 + 120 = 200 → clamped
        assert widget_updater._estimate_session_pct(state) == 100


class TestEmergencyRecal:
    """When the local estimate goes somewhere that proves the budget is wrong
    (pegged at the 100% clamp, or sprinted FORCE_RECAL_GAP_PP past the last API
    truth), on_modified spends one cooldown-gated API call to re-anchor."""

    def test_clamp_hit_is_suspect(self, make_handler):
        h = make_handler()
        now = datetime.now(timezone.utc)
        h.state["implied_session_budget"] = 100000
        h.state["input_tokens"] = 80000
        h.state["output_tokens"] = 40000   # 120k/100k = 120% unclamped
        h.last_api_pct = 40
        assert h._estimate_is_suspect(100, now) is True

    def test_big_gap_is_suspect(self, make_handler):
        h = make_handler()
        now = datetime.now(timezone.utc)
        h.state["implied_session_budget"] = 200000
        h.state["input_tokens"] = 40000
        h.state["output_tokens"] = 30000   # 35%, not clamped
        h.last_api_pct = 5                 # 30pp ahead of truth >= 25
        assert h._estimate_is_suspect(35, now) is True

    def test_small_gap_not_suspect(self, make_handler):
        h = make_handler()
        now = datetime.now(timezone.utc)
        h.state["implied_session_budget"] = 200000
        h.state["input_tokens"] = 12000
        h.state["output_tokens"] = 12000   # 12%
        h.last_api_pct = 10                # 2pp gap < FORCE_RECAL_GAP_PP, not clamped
        assert h._estimate_is_suspect(12, now) is False

    def test_cooldown_suppresses(self, make_handler):
        h = make_handler()
        now = datetime.now(timezone.utc)
        h.state["implied_session_budget"] = 100000
        h.state["input_tokens"] = 120000
        h.state["output_tokens"] = 0       # clamped
        h.last_api_pct = 40
        h.last_forced_recal = now - timedelta(seconds=10)  # inside cooldown
        assert h._estimate_is_suspect(100, now) is False

    def test_no_budget_not_suspect(self, make_handler):
        h = make_handler()
        now = datetime.now(timezone.utc)
        h.state.pop("implied_session_budget", None)
        assert h._estimate_is_suspect(100, now) is False

    def test_clamp_hit_uses_weighted_io(self, make_handler):
        """clamp_hit must fire on WEIGHTED io reaching the budget, not raw
        input+output.  This guards the unit bug where raw io (much smaller than
        weighted) was compared against the weighted budget, making clamp_hit
        effectively dead (Task 3)."""
        h = make_handler()
        now = datetime.now(timezone.utc)
        # Budget expressed in weighted units.  Choose tokens so that:
        #   raw input+output << budget  (old code would NOT fire clamp_hit)
        #   weighted io       >= budget  (new code DOES fire clamp_hit)
        dw = widget_updater.DEFAULT_WEIGHTS
        # 10k output tokens: raw = 10k, weighted = 10k * dw["output"] ~ 75k
        h.state["input_tokens"]  = 0
        h.state["output_tokens"] = 10000
        weighted = widget_updater._weighted_io(h.state)   # ~75000
        h.state["implied_session_budget"] = int(weighted * 0.9)   # budget < weighted
        h.last_api_pct = 10
        # Raw io (10k) << budget, but weighted io > budget → clamp_hit must be True.
        assert h._estimate_is_suspect(100, now) is True
        # Sanity: raw io is well below the budget to confirm we're testing the fix.
        raw_io = h.state["input_tokens"] + h.state["output_tokens"]
        assert raw_io < h.state["implied_session_budget"]

    def test_on_modified_forces_recal_when_clamped(self, make_handler, monkeypatch, tmp_path):
        h = make_handler()
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

    def _capture(self, monkeypatch):
        # Stub _append_calibration so the test neither writes to the real
        # calibration log nor depends on it -- just records the gate decision
        # and applies the budget the same way the real one would.
        calls = {}
        def fake(state, pct, when, update_budget=True,
                 stale_pct=None, trigger="scheduled"):
            calls["update_budget"] = update_budget
            if update_budget and pct >= widget_updater.CALIBRATION_PCT_FLOOR:
                io = state["input_tokens"] + state["output_tokens"]
                state["implied_session_budget"] = round(io / (pct / 100))
        monkeypatch.setattr(widget_updater, "_append_calibration", fake)
        # Keep the recalibrate path off the real disk: the rescan is covered by
        # TestWatcherStuck; here we only care about the budget gate decision.
        monkeypatch.setattr(widget_updater, "full_scan", lambda *a, **k: None)
        return calls

    def test_big_diff_recalibrates(self, make_handler, monkeypatch):
        h = make_handler()
        calls = self._capture(monkeypatch)
        h.session_pct = 50
        h.state["implied_session_budget"] = 200000
        h.state["input_tokens"], h.state["output_tokens"] = 40000, 0
        assert h._adopt_api_pct(80, datetime.now(timezone.utc)) is True
        assert calls["update_budget"] is True
        assert h.session_pct == 80 and h.last_api_pct == 80
        assert h.state["implied_session_budget"] == 50000   # 40k / (80/100)

    def test_small_diff_keeps_budget(self, make_handler, monkeypatch):
        h = make_handler()
        calls = self._capture(monkeypatch)
        h.session_pct = 50
        h.state["implied_session_budget"] = 200000
        h.state["input_tokens"], h.state["output_tokens"] = 40000, 0
        assert h._adopt_api_pct(53, datetime.now(timezone.utc)) is False  # 3pp
        assert calls["update_budget"] is False
        assert h.session_pct == 53                            # display adopts
        assert h.state["implied_session_budget"] == 200000    # budget untouched

    def test_no_budget_recalibrates(self, make_handler, monkeypatch):
        h = make_handler()
        calls = self._capture(monkeypatch)
        h.session_pct = None
        h.state.pop("implied_session_budget", None)
        h.state["input_tokens"], h.state["output_tokens"] = 20000, 0
        assert h._adopt_api_pct(10, datetime.now(timezone.utc)) is True
        assert calls["update_budget"] is True

    def test_below_floor_no_recalibrate(self, make_handler, monkeypatch):
        h = make_handler()
        calls = self._capture(monkeypatch)
        h.session_pct = 50
        h.state["implied_session_budget"] = 200000
        assert h._adopt_api_pct(3, datetime.now(timezone.utc)) is False  # < floor
        assert calls["update_budget"] is False
        assert h.session_pct == 3                             # display still adopts

    def test_below_floor_no_budget_bootstraps(self, make_handler, monkeypatch):
        # With no budget yet, a sub-floor reading still derives one (blended)
        # so the live estimate can display early instead of freezing.
        h = make_handler()
        calls = self._capture(monkeypatch)
        h.session_pct = None
        h.state.pop("implied_session_budget", None)
        h.state["input_tokens"], h.state["output_tokens"] = 2000, 0
        h._adopt_api_pct(3, datetime.now(timezone.utc))   # below floor
        assert calls["update_budget"] is True

    def test_provisional_budget_upgrades_at_floor(self, make_handler, monkeypatch):
        # A provisional sub-floor budget is replaced by a live derivation the
        # first time the API reports at/above the floor, even with a small diff.
        h = make_handler()
        calls = self._capture(monkeypatch)
        h.session_pct = 5
        h.state["implied_session_budget"] = 180000
        h.state["budget_source"] = "blended"          # provisional
        h.state["input_tokens"], h.state["output_tokens"] = 12000, 0
        assert h._adopt_api_pct(6, datetime.now(timezone.utc)) is True  # 1pp diff
        assert calls["update_budget"] is True
        assert h.state["implied_session_budget"] == round(12000 / 0.06)

    def test_live_budget_small_diff_not_upgraded(self, make_handler, monkeypatch):
        # A non-provisional (live) budget is left alone on a small diff, even
        # at/above the floor -- provisional upgrade must not weaken that gate.
        h = make_handler()
        calls = self._capture(monkeypatch)
        h.session_pct = 50
        h.state["implied_session_budget"] = 200000
        h.state["budget_source"] = "live"
        assert h._adopt_api_pct(52, datetime.now(timezone.utc)) is False  # 2pp
        assert calls["update_budget"] is False
        assert h.state["implied_session_budget"] == 200000

    def test_sets_anchor(self, make_handler, monkeypatch):
        # _adopt_api_pct must record anchor_pct + anchor_io so subsequent
        # _local_estimate calls start from the API value, not total_io/budget.
        h = make_handler()
        self._capture(monkeypatch)
        h.session_pct = 50
        h.state["implied_session_budget"] = 200000
        h.state["input_tokens"], h.state["output_tokens"] = 40000, 20000
        h._adopt_api_pct(53, datetime.now(timezone.utc))
        assert h.state["anchor_pct"] == 53
        # anchor_io is the WEIGHTED total at call time, not raw 40k+20k.
        assert h.state["anchor_io"] == widget_updater._weighted_io(h.state)

    def test_local_estimate_uses_anchor_immediately(self, make_handler, monkeypatch):
        # After _adopt_api_pct with no new tokens, _local_estimate returns the
        # anchor pct + the rounding midpoint correction (API_PCT_FLOOR_BIAS_PP,
        # 0.0 under the current round-to-nearest presumption).
        h = make_handler()
        self._capture(monkeypatch)
        h.session_pct = 50
        h.state["implied_session_budget"] = 200000
        h.state["input_tokens"], h.state["output_tokens"] = 40000, 20000
        h._adopt_api_pct(53, datetime.now(timezone.utc))
        # delta = 0, so estimate == anchor_pct + API_PCT_FLOOR_BIAS_PP.
        assert h._local_estimate() == round(53 + widget_updater.API_PCT_FLOOR_BIAS_PP, 1)


class TestWatcherStuck:
    """On recalibration we re-scan the folder from disk. Tokens recovered that
    no on_modified event reported mean the watcher missed events; if it's also
    been silent for WATCHER_STUCK_SILENCE_SECS, it's stuck -> prompt restart.
    Off-laptop usage leaves no local-disk tokens, so it never trips this."""

    def _make_seeded_handler(self, make_handler):
        h = make_handler()
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

    def test_silent_with_missed_tokens_arms_then_warns(self, make_handler, monkeypatch):
        h = self._make_seeded_handler(make_handler)
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

    def test_ping_in_grace_window_cancels_warning(self, make_handler, monkeypatch):
        h = self._make_seeded_handler(make_handler)
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

    def test_recent_events_no_recheck(self, make_handler, monkeypatch):
        h = self._make_seeded_handler(make_handler)
        now = datetime.now(timezone.utc)
        h.last_event_at = now - timedelta(seconds=30)     # events flowing
        self._scan_adds(monkeypatch, 5000)
        armed = self._no_real_timer(monkeypatch)
        assert h._rescan_and_check_watcher(now) == 5000   # still healed
        assert not armed                                  # no re-check armed

    def test_no_missed_tokens_no_recheck(self, make_handler, monkeypatch):
        h = self._make_seeded_handler(make_handler)
        now = datetime.now(timezone.utc)
        h.last_event_at = now - timedelta(
            seconds=widget_updater.WATCHER_STUCK_SILENCE_SECS + 60)
        self._scan_adds(monkeypatch, 0)
        armed = self._no_real_timer(monkeypatch)
        assert h._rescan_and_check_watcher(now) == 0
        assert not armed

    def test_never_saw_event_no_recheck(self, make_handler, monkeypatch):
        h = self._make_seeded_handler(make_handler)
        now = datetime.now(timezone.utc)
        h.last_event_at = None
        self._scan_adds(monkeypatch, 5000)
        armed = self._no_real_timer(monkeypatch)
        assert h._rescan_and_check_watcher(now) == 5000   # healed silently
        assert not armed


class TestOnModifiedAdvancesPct:
    """Integration guard: on_modified must move session_pct from local token
    growth when no calibration fires. This is the exact path that froze."""

    def test_pct_advances_without_api_call(self, make_handler, monkeypatch, tmp_path):
        h = make_handler()
        now = datetime.now(timezone.utc)
        # Live session window, calibrated already, budget known, budget spent so
        # _maybe_calibrate won't fire; liveness recently done so it won't ping.
        h.session_start = now - timedelta(hours=1)
        h.session_end = now + timedelta(hours=4)
        h.last_calibrated = now
        h.last_liveness = now
        h.state["calibration_calls_remaining"] = 0
        h.state["input_tokens"] = 40000
        h.state["output_tokens"] = 60000
        # budget = 2x the seed weighted count => seed reads 50%.
        h.state["implied_session_budget"] = widget_updater._weighted_io(h.state) * 2
        # Clear any real-state anchor so the fallback (total_io/budget) path
        # runs cleanly rather than using a live anchor that happens to be loaded
        # from the user's real widget_state.json via _load_state().
        h.state.pop("anchor_pct", None)
        h.state.pop("anchor_io", None)
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

        # msg_new adds weighted tokens, so pct must RISE above the 50% seed
        # (not freeze); exact value follows the weighted formula.
        exp = min(100, round(100 * widget_updater._weighted_io(h.state)
                             / h.state["implied_session_budget"], 1))
        assert h.session_pct == exp
        assert h.session_pct > 50


class TestSessionRollover:
    """Time-based session expiry must reset local state WITHOUT a file event or
    an API call, so a window that ends while the widget is idle or closed can't
    keep showing the dead session's % (or fire a false 'stuck' discrepancy
    against it when the API next reports the fresh window at 0%).

    Contract: caught live (within ROLLOVER_GRACE_SECS of the boundary) => fresh
    0%; noticed late => pending '--' (None) until the API/transcript confirms.
    """

    def _expired_handler(self, make_handler, end):
        """Handler holding a non-trivial reading for a window ending at `end`."""
        h = make_handler()
        h.session_start = end - timedelta(hours=widget_updater.SESSION_HOURS)
        h.session_end = end
        h.session_pct = 74
        h.last_calibrated = datetime.now(timezone.utc)
        h.state["implied_session_budget"] = 100000
        h.state["input_tokens"] = 60000
        h.state["output_tokens"] = 80000
        h.state["seen_ids"] = {"msg_old"}
        return h

    def test_no_rollover_before_expiry(self, make_handler):
        now = datetime.now(timezone.utc)
        h = self._expired_handler(make_handler, now + timedelta(hours=1))
        assert h._roll_over_if_expired(now) is False
        assert h.session_pct == 74          # untouched
        assert h.session_end is not None

    def test_caught_live_snaps_to_zero(self, make_handler):
        now = datetime.now(timezone.utc)
        # Boundary 5s ago — inside the grace window => we were watching.
        h = self._expired_handler(make_handler, now - timedelta(seconds=5))
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

    def test_noticed_late_blanks_to_pending(self, make_handler):
        now = datetime.now(timezone.utc)
        # Boundary an hour ago — well past grace => widget wasn't watching.
        h = self._expired_handler(make_handler, now - timedelta(hours=1))
        assert h._roll_over_if_expired(now) is True
        assert h.session_pct is None        # '--', not a fabricated 0
        assert h.session_start is None

    def test_grace_boundary_is_inclusive(self, make_handler):
        now = datetime.now(timezone.utc)
        end = now - timedelta(seconds=widget_updater.ROLLOVER_GRACE_SECS)
        h = self._expired_handler(make_handler, end)   # exactly at the grace edge
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


# ---------------------------------------------------------------------------
# Incremental process_file - the watcher stalled on a 2.5MB transcript
# because each FS event re-read and re-parsed the whole file. The fix is
# to seek to a stored byte offset and only parse new bytes. These tests
# guard the seek+offset semantics and the multi-MB performance floor.
# ---------------------------------------------------------------------------

class TestLivenessTriggers:
    """_maybe_liveness fires on: 20-min heartbeat, 10pp local-estimate delta,
    and one-shot thresholds at 5%, 10%, 95% (first crossing only)."""

    def _make_seeded_handler(self, make_handler):
        # stub_maybe_liveness=False: these tests exercise the real _maybe_liveness.
        h = make_handler(stub_maybe_liveness=False)
        now = datetime.now(timezone.utc)
        h.session_start = now - timedelta(hours=1)
        h.session_end   = now + timedelta(hours=4)
        h.state["implied_session_budget"] = 200000
        h.state["anchor_pct"] = 0.0
        h.state["anchor_io"]  = 0
        h.state["input_tokens"]  = 0
        h.state["output_tokens"] = 0
        h.session_pct = 0
        h.last_liveness = now   # suppress time trigger
        return h

    def _stub_fetch(self, monkeypatch, h):
        """Stub _fetch_with_tracking to record calls and return None (enough
        to exercise trigger logic without network)."""
        calls = []
        monkeypatch.setattr(h, "_fetch_with_tracking",
                            lambda: calls.append(True) or None)
        return calls

    def test_delta_trigger_fires_at_10pp(self, make_handler, monkeypatch):
        h = self._make_seeded_handler(make_handler)
        calls = self._stub_fetch(monkeypatch, h)
        h._liveness_anchor_pct = 20.0
        h._triggered_thresholds = widget_updater.LIVENESS_ONE_SHOT_PCTS.copy()
        # want est = 30 (exactly 10pp past the anchor of 20) to hit the trigger
        # boundary. input weight applies: 40000 * 1.5 = 60000 weighted =>
        # est = 100 * 60000 / 200000 = 30.
        h.state["input_tokens"]  = 40000
        h._maybe_liveness()
        assert len(calls) == 1

    def test_delta_trigger_no_fire_below_10pp(self, make_handler, monkeypatch):
        h = self._make_seeded_handler(make_handler)
        calls = self._stub_fetch(monkeypatch, h)
        h._liveness_anchor_pct = 20.0
        h._triggered_thresholds = widget_updater.LIVENESS_ONE_SHOT_PCTS.copy()
        # input weight applies: 37000 * 1.5 = 55500 weighted => est=27.75,
        # ~7.75pp above the 20.0 anchor, still < the 10pp delta trigger.
        h.state["input_tokens"] = 37000
        h._maybe_liveness()
        assert len(calls) == 0

    def test_one_shot_fires_at_5pct(self, make_handler, monkeypatch):
        h = self._make_seeded_handler(make_handler)
        calls = self._stub_fetch(monkeypatch, h)
        h._triggered_thresholds = set()
        h.state["input_tokens"] = 12000    # est = 6% (above 5)
        h._maybe_liveness()
        assert len(calls) == 1
        assert 5 in h._triggered_thresholds

    def test_one_shot_fires_at_10pct(self, make_handler, monkeypatch):
        h = self._make_seeded_handler(make_handler)
        calls = self._stub_fetch(monkeypatch, h)
        h._triggered_thresholds = {5}      # 5% already done
        h.state["input_tokens"] = 22000    # est = 11% (above 10)
        h._maybe_liveness()
        assert len(calls) == 1
        assert 10 in h._triggered_thresholds

    def test_one_shot_fires_at_95pct(self, make_handler, monkeypatch):
        h = self._make_seeded_handler(make_handler)
        calls = self._stub_fetch(monkeypatch, h)
        h._triggered_thresholds = {5, 10}
        h.state["input_tokens"] = 192000   # est = 96%
        h._maybe_liveness()
        assert len(calls) == 1
        assert 95 in h._triggered_thresholds

    def test_one_shot_not_refired(self, make_handler, monkeypatch):
        h = self._make_seeded_handler(make_handler)
        calls = self._stub_fetch(monkeypatch, h)
        h._triggered_thresholds = widget_updater.LIVENESS_ONE_SHOT_PCTS.copy()
        h._liveness_anchor_pct = 50.0
        h.state["input_tokens"] = 192000   # est=96%, all one-shots done, delta=46pp
        # Only the delta trigger should fire (96-50=46 >= 10)
        h._maybe_liveness()
        assert len(calls) == 1
        # Firing again shouldn't re-trigger (anchor is now ~96)
        calls.clear()
        h._maybe_liveness()
        assert len(calls) == 0

    def test_anchor_updated_after_successful_poll(self, make_handler, monkeypatch):
        # _set_anchor is called inside _adopt_api_pct on a successful fetch;
        # the new baseline should be the API-returned pct, not the local est.
        h = self._make_seeded_handler(make_handler)
        h._triggered_thresholds = set()
        h.state["input_tokens"] = 12000    # local est = 6%
        # Successful fetch returns pct=7 (API and local may differ slightly)
        now = datetime.now(timezone.utc)
        raw = {"five_hour": {"utilization": 7.0,
                             "resets_at": (now + timedelta(hours=4)).isoformat()},
               "seven_day": {"utilization": 10.0,
                             "resets_at": (now + timedelta(days=7)).isoformat()}}
        monkeypatch.setattr(h, "_fetch_with_tracking", lambda: raw)
        monkeypatch.setattr(widget_updater, "full_scan", lambda *a, **k: None)
        h._maybe_liveness()
        assert h._liveness_anchor_pct == 7.0

    def test_calibration_call_resets_baseline(self, make_handler, monkeypatch):
        # Any API call resets the baseline, not just liveness calls.
        # After a calibration call at pct=30, a liveness delta trigger should
        # measure from 30, not from the old liveness anchor.
        h = self._make_seeded_handler(make_handler)
        h._liveness_anchor_pct = 10.0
        h._triggered_thresholds = set(widget_updater.LIVENESS_ONE_SHOT_PCTS)
        # Simulate _set_anchor being called by a calibration at pct=30
        h.state["input_tokens"] = 60000
        h._set_anchor(30.0)
        assert h._liveness_anchor_pct == 30.0
        # Now est=30, anchor=30 → delta=0 → no trigger
        calls = self._stub_fetch(monkeypatch, h)
        h._maybe_liveness()
        assert len(calls) == 0

    def test_rollover_resets_triggers(self, make_handler, monkeypatch):
        h = self._make_seeded_handler(make_handler)
        h._triggered_thresholds = set(widget_updater.LIVENESS_ONE_SHOT_PCTS)
        h._liveness_anchor_pct = 50.0
        # Expire the session so _roll_over_if_expired actually fires.
        h.session_end = datetime.now(timezone.utc) - timedelta(seconds=200)
        h._roll_over_if_expired(datetime.now(timezone.utc))
        assert h._triggered_thresholds == set()
        assert h._liveness_anchor_pct is None

    def test_no_trigger_without_estimate(self, make_handler, monkeypatch):
        # No budget => est is None => only time trigger can fire.
        h = self._make_seeded_handler(make_handler)
        calls = self._stub_fetch(monkeypatch, h)
        h.state.pop("implied_session_budget", None)
        h._triggered_thresholds = set()
        h._liveness_anchor_pct  = 0.0
        h._maybe_liveness()   # no time due, no est → nothing
        assert len(calls) == 0

    def test_one_shot_not_refired_when_api_returns_below_threshold(
            self, make_handler, monkeypatch):
        # Regression: if the 10% one-shot fires and the API returns 5%,
        # _set_anchor only marks thresholds <= 5 as triggered. Without
        # also marking due_shots, the 10% one-shot would fire again on
        # the next poll because est is still above 10%.
        h = self._make_seeded_handler(make_handler)
        h._triggered_thresholds = set()         # 10% not yet triggered
        h.state["input_tokens"] = 22000         # est = 11% → crosses 10%

        now = datetime.now(timezone.utc)
        raw = {"five_hour": {"utilization": 5.0,  # API says only 5%
                             "resets_at": (now + timedelta(hours=4)).isoformat()},
               "seven_day": {"utilization": 10.0,
                             "resets_at": (now + timedelta(days=7)).isoformat()}}
        monkeypatch.setattr(h, "_fetch_with_tracking", lambda: raw)
        monkeypatch.setattr(widget_updater, "full_scan", lambda *a, **k: None)

        h._maybe_liveness()
        assert 10 in h._triggered_thresholds    # must be marked even though api < 10%

        # Second call: est is still above 10% but threshold already marked → no refetch.
        fetch_calls = []
        monkeypatch.setattr(h, "_fetch_with_tracking",
                            lambda: fetch_calls.append(True) or None)
        h._maybe_liveness()
        assert len(fetch_calls) == 0


class TestIncrementalProcessFile:
    def _window(self):
        # A generous window so synthesized timestamps always fall inside.
        now = datetime.now(timezone.utc)
        return now - timedelta(hours=1), now + timedelta(hours=4), now

    def test_append_only_reads_new_bytes(self, tmp_path):
        start, end, now = self._window()
        state = widget_updater._empty_state(start)
        f = tmp_path / "t.jsonl"
        f.write_text(_assistant_line("m1", 100, 200, now) + "\n",
                     encoding="utf-8")

        assert widget_updater.process_file(f, state, start, end) is True
        first_off = state["offsets"][str(f)]
        assert state["input_tokens"] == 100
        assert state["output_tokens"] == 200

        # Second call with no changes is a no-op and doesn't re-parse.
        assert widget_updater.process_file(f, state, start, end) is False
        assert state["offsets"][str(f)] == first_off

        # Append a new record. Only the new bytes should be parsed.
        with f.open("a", encoding="utf-8") as h:
            h.write(_assistant_line("m2", 50, 75, now) + "\n")
        assert widget_updater.process_file(f, state, start, end) is True
        assert state["input_tokens"] == 150
        assert state["output_tokens"] == 275
        assert state["offsets"][str(f)] > first_off

    def _cache_line(self, msg_id, ts, *, inp=0, out=0, c1h=0, c5m=0, cread=0,
                    nested=True):
        usage = {"input_tokens": inp, "output_tokens": out,
                 "cache_read_input_tokens": cread}
        if nested:
            usage["cache_creation"] = {"ephemeral_1h_input_tokens": c1h,
                                       "ephemeral_5m_input_tokens": c5m}
        else:
            # Older record shape: flat total, no 1h/5m breakdown.
            usage["cache_creation_input_tokens"] = c1h
        return json.dumps({"type": "assistant", "timestamp": ts.isoformat(),
                           "message": {"id": msg_id, "model": "claude-opus-4-8",
                                       "usage": usage}})

    def test_cache_tokens_accumulated_and_logged(self, tmp_path, monkeypatch):
        start, end, now = self._window()
        state = widget_updater._empty_state(start)
        # Fresh state starts the cache counters at zero.
        assert (state["cache_write_1h"], state["cache_write_5m"],
                state["cache_read"]) == (0, 0, 0)

        f = tmp_path / "t.jsonl"
        f.write_text(self._cache_line("m1", now, inp=2, out=500,
                                      c1h=3958, c5m=0, cread=13707) + "\n",
                     encoding="utf-8")
        widget_updater.process_file(f, state, start, end)
        assert state["cache_write_1h"] == 3958
        assert state["cache_read"] == 13707

        # A record without the nested split falls back to the flat total -> 1h.
        with f.open("a", encoding="utf-8") as h:
            h.write(self._cache_line("m2", now, c1h=1000, cread=200,
                                     nested=False) + "\n")
        widget_updater.process_file(f, state, start, end)
        assert state["cache_write_1h"] == 4958
        assert state["cache_read"] == 13907

        # The calibration record carries the cache vector for later weight fits.
        calib = tmp_path / "calibration.jsonl"
        monkeypatch.setattr(widget_updater, "CALIBRATION_FILE", calib)
        widget_updater._append_calibration(state, 50.0, now, update_budget=False)
        rec = json.loads(calib.read_text(encoding="utf-8").splitlines()[-1])
        assert rec["transcript_cache_write_1h"] == 4958
        assert rec["transcript_cache_write_5m"] == 0
        assert rec["transcript_cache_read"] == 13907

    def test_partial_trailing_line_held_for_next_read(self, tmp_path):
        start, end, now = self._window()
        state = widget_updater._empty_state(start)
        f = tmp_path / "t.jsonl"
        full = _assistant_line("m1", 100, 200, now) + "\n"
        partial = _assistant_line("m2", 50, 50, now)  # NO trailing newline
        f.write_bytes((full + partial).encode("utf-8"))

        widget_updater.process_file(f, state, start, end)
        # Only m1 should be counted; the partial m2 line is held back.
        assert state["input_tokens"] == 100
        assert "m1" in state["seen_ids"]
        assert "m2" not in state["seen_ids"]

        # Complete the line, run again - now m2 is picked up.
        with f.open("ab") as h:
            h.write(b"\n")
        widget_updater.process_file(f, state, start, end)
        assert state["input_tokens"] == 150
        assert "m2" in state["seen_ids"]

    def test_truncation_rescans_from_zero(self, tmp_path, capsys):
        start, end, now = self._window()
        state = widget_updater._empty_state(start)
        f = tmp_path / "t.jsonl"
        f.write_text(_assistant_line("m1", 100, 200, now) + "\n",
                     encoding="utf-8")
        widget_updater.process_file(f, state, start, end)

        # Replace with shorter content (rotation/truncation).
        f.write_text(_assistant_line("m2", 10, 20, now) + "\n",
                     encoding="utf-8")
        widget_updater.process_file(f, state, start, end)

        # Loud failure - the shrink should print a warning.
        assert "shrank" in capsys.readouterr().out
        # m1 still counted (seen_ids dedupes), m2 also counted.
        assert state["input_tokens"] == 110
        assert {"m1", "m2"} <= state["seen_ids"]

    def test_invalid_json_after_prefilter_logs_loudly(self, tmp_path, capsys):
        start, end, _ = self._window()
        state = widget_updater._empty_state(start)
        f = tmp_path / "t.jsonl"
        # A line that passes the byte prefilter but isn't valid JSON.
        f.write_bytes(b'{"type":"assistant","usage":BROKEN\n')
        widget_updater.process_file(f, state, start, end)
        out = capsys.readouterr().out
        assert "JSON decode failed" in out

    def test_large_file_incremental_is_fast(self, tmp_path):
        """The bug: process_file re-read and re-parsed a 2.5MB file on every
        FS event, stalling the watcher. After the fix, the second call (with
        one new record appended) should be effectively instant - it only
        parses the delta, not the whole file."""
        import time
        start, end, now = self._window()
        state = widget_updater._empty_state(start)
        f = tmp_path / "big.jsonl"

        # Build a ~3MB transcript: mostly user/tool-result noise the
        # prefilter throws away, plus a few assistant records. This mirrors
        # the real shape of a long Claude Code session.
        noise = json.dumps({"type": "user",
                            "message": {"content": "x" * 500}})
        lines = []
        for i in range(5000):
            lines.append(noise)
            if i % 500 == 0:
                lines.append(_assistant_line(f"m{i}", 10, 20, now))
        f.write_text("\n".join(lines) + "\n", encoding="utf-8")
        size_mb = f.stat().st_size / (1024 * 1024)
        assert size_mb > 2, f"test fixture too small: {size_mb:.2f}MB"

        # First (cold) scan - expected to be the slow one. We don't assert on
        # this; we just want a baseline budget consumed once.
        t0 = time.perf_counter()
        widget_updater.process_file(f, state, start, end)
        cold = time.perf_counter() - t0
        cold_in = state["input_tokens"]
        assert cold_in > 0

        # Append one new assistant record and time the incremental call.
        with f.open("a", encoding="utf-8") as h:
            h.write(_assistant_line("m_new", 7, 11, now) + "\n")
        t0 = time.perf_counter()
        widget_updater.process_file(f, state, start, end)
        warm = time.perf_counter() - t0

        # The whole point: warm path doesn't pay the cold cost. 50ms is
        # generous - on a developer machine it's typically <1ms - but loose
        # enough to survive CI jitter.
        assert warm < 0.05, (
            f"incremental read should be <50ms, was {warm*1000:.1f}ms "
            f"(cold was {cold*1000:.1f}ms, file {size_mb:.2f}MB)"
        )
        assert state["input_tokens"] == cold_in + 7

    def test_no_trailing_newline_returns_false(self, tmp_path):
        """A file with content but no completed line yet must NOT advance
        the offset - otherwise the first record gets lost forever."""
        start, end, now = self._window()
        state = widget_updater._empty_state(start)
        f = tmp_path / "t.jsonl"
        f.write_bytes(_assistant_line("m1", 100, 200, now).encode("utf-8"))

        assert widget_updater.process_file(f, state, start, end) is False
        assert state["offsets"].get(str(f), 0) == 0
        assert state["input_tokens"] == 0


# ---------------------------------------------------------------------------
# Budget lower bound (delta-calibration)
#
# Every API-to-API interval gives a guaranteed lower bound on the true budget:
#   lb = 100 * Δio_local / (Δpct_api + 1)
# The "+1" accounts for worst-case floor rounding (true Δpct could be up to
# observed + 1 pp). Off-laptop contamination inflates Δpct, which makes lb
# smaller (more conservative) — the max() in _set_anchor preserves the
# tightest bound ever seen this session.
# ---------------------------------------------------------------------------

class TestBudgetLowerBound:
    # _isolate_user_data_files (autouse) redirects STATE_FILE to a non-existent
    # tmp path, so _load_state() falls back to _empty_state() automatically —
    # no explicit _load_state stub needed here.

    def _seed_anchor(self, h, pct, io):
        """Set a clean anchor directly (bypasses lb computation for setup).
        `io` is a raw input-token count; anchors are stored in WEIGHTED-io units
        (the input weight applies) to match what _set_anchor records live."""
        h.state["input_tokens"]  = io
        h.state["output_tokens"] = 0
        wio = widget_updater._weighted_io(h.state)
        h.state["session_anchors"] = (h.state.get("session_anchors") or []) + [[pct, wio]]
        h.state["anchor_pct"] = pct
        h.state["anchor_io"]  = wio

    def test_no_lb_on_first_anchor(self, make_handler):
        # First anchor: session_anchors is empty, no prior to diff against.
        h = make_handler()
        h.state["session_anchors"] = []
        h.state["input_tokens"] = 10000
        h.state["output_tokens"] = 0
        h._set_anchor(5.0)
        assert h.state.get("session_budget_lb", 0) == 0

    def test_lb_computed_on_second_anchor(self, make_handler):
        # 10k tokens, Δpct=5 → denom=6 → lb = 100*10000/6 = 166666
        h = make_handler()
        h.state["session_anchors"] = []
        self._seed_anchor(h, 5.0, 5000)          # anchor 1, no lb yet
        h.state["input_tokens"] = 15000           # +10k
        h._set_anchor(10.0)                       # Δpct=5, Δio=10k
        W = widget_updater.DEFAULT_WEIGHTS["input"]   # lb is on WEIGHTED io
        assert h.state["session_budget_lb"] == int(100 * 10000 * W / 6)

    def test_worst_case_rounding_uses_delta_plus_one(self, make_handler):
        # denom must be Δpct+1, not Δpct — the bound must hold even if the
        # true Δpct was Δpct_api + 0.99 pp (floor rounding worst case).
        h = make_handler()
        h.state["session_anchors"] = []
        self._seed_anchor(h, 0.0, 0)
        h.state["input_tokens"] = 20000
        h._set_anchor(2.0)                        # Δpct=2 → denom=3
        W = widget_updater.DEFAULT_WEIGHTS["input"]   # lb is on WEIGHTED io
        assert h.state["session_budget_lb"] == int(100 * 20000 * W / 3)
        assert h.state["session_budget_lb"] < int(100 * 20000 * W / 2)  # not naive /2

    def test_zero_delta_pct_gives_lb(self, make_handler):
        # Δpct=0, Δio>0: pct didn't tick so true Δpct < 1 pp → denom=1.
        # lb = 100 * Δio / 1 = budget ≥ 100 × tokens_used.
        h = make_handler()
        h.state["session_anchors"] = []
        self._seed_anchor(h, 5.0, 1000)
        h.state["input_tokens"] = 6000            # +5k, pct still 5
        h._set_anchor(5.0)                        # Δpct=0 → denom=1
        W = widget_updater.DEFAULT_WEIGHTS["input"]   # lb is on WEIGHTED io
        assert h.state["session_budget_lb"] == int(100 * 5000 * W / 1)

    def test_lb_is_running_maximum(self, make_handler):
        # lb grows when a new pair is tighter, stays put when it's looser.
        h = make_handler()
        h.state["session_anchors"] = []
        self._seed_anchor(h, 0.0, 0)

        W = widget_updater.DEFAULT_WEIGHTS["input"]   # lb is on WEIGHTED io
        # Anchor 2: 20k tokens, Δpct=2 → pair(1,2): lb = 100*20000*W/3
        h.state["input_tokens"] = 20000
        h._set_anchor(2.0)
        lb1 = h.state["session_budget_lb"]
        assert lb1 == int(100 * 20000 * W / 3)

        # Anchor 3: contaminated (+2k local, Δpct=5). All pairs involving
        # anchor 3 have inflated Δpct → smaller lb. max() preserves lb1.
        h.state["input_tokens"] = 22000
        h._set_anchor(7.0)
        assert h.state["session_budget_lb"] == lb1

        # Anchor 4: 30k local tokens, Δpct=1.
        # pair(3,4): lb = 100*30000/2 = 1500000 — tighter, wins.
        h.state["input_tokens"] = 52000
        h._set_anchor(8.0)
        assert h.state["session_budget_lb"] == int(100 * 30000 * W / 2)

    def test_full_history_beats_consecutive(self, make_handler):
        # Two clean intervals each with Δpct=1. Consecutive lb = 100*Δio/2.
        # The full span (anchor1→anchor3) has Δpct=2 → lb = 100*(2*Δio)/3,
        # which is larger than 100*Δio/2 — the "+1" amortizes over more pcts.
        h = make_handler()
        h.state["session_anchors"] = []
        self._seed_anchor(h, 0.0, 0)

        h.state["input_tokens"] = 10000
        h._set_anchor(1.0)                        # pair(1,2): lb=100*10k/2=500k
        lb_after_2 = h.state["session_budget_lb"]

        h.state["input_tokens"] = 20000
        h._set_anchor(2.0)
        W = widget_updater.DEFAULT_WEIGHTS["input"]   # lb is on WEIGHTED io
        # pair(1,3): Δpct=2, Δio=20k → lb=100*20k*W/3  (full span wins)
        # pair(2,3): Δpct=1, Δio=10k → lb=100*10k*W/2
        assert h.state["session_budget_lb"] == int(100 * 20000 * W / 3)
        assert h.state["session_budget_lb"] > lb_after_2

    def test_negative_delta_pct_skipped(self, make_handler):
        # A pct drop signals a session reset — skip to avoid a nonsensical lb.
        h = make_handler()
        h.state["session_anchors"] = []
        self._seed_anchor(h, 40.0, 50000)
        h.state["input_tokens"] = 60000
        h._set_anchor(5.0)                        # pct dropped — skip
        assert h.state.get("session_budget_lb", 0) == 0

    def test_append_calibration_clamped_up_to_lb(self, monkeypatch):
        # If the absolute back-derivation yields a budget below the lb,
        # _append_calibration must clamp it up.
        state = widget_updater._empty_state(
            datetime(2099, 1, 1, tzinfo=timezone.utc))
        state["input_tokens"] = 10000
        state["output_tokens"] = 0
        # lb says budget ≥ 300k, but 10k tokens at 5% implies only 200k.
        state["session_budget_lb"] = 300000
        widget_updater._append_calibration(state, 5.0, datetime.now(timezone.utc))
        assert state["implied_session_budget"] == 300000

    def test_append_calibration_not_clamped_when_above_lb(self, monkeypatch):
        # When the derived budget already exceeds lb, no clamping occurs.
        state = widget_updater._empty_state(
            datetime(2099, 1, 1, tzinfo=timezone.utc))
        state["input_tokens"] = 50000
        state["output_tokens"] = 50000
        expected = round(widget_updater._weighted_io(state) / 0.5)
        state["session_budget_lb"] = 100000   # below expected => no clamp
        widget_updater._append_calibration(state, 50.0, datetime.now(timezone.utc))
        assert state["implied_session_budget"] == expected

    def test_empty_state_has_zero_lb(self):
        state = widget_updater._empty_state(
            datetime(2099, 1, 1, tzinfo=timezone.utc))
        assert state["session_budget_lb"] == 0



# ---------------------------------------------------------------------------
# Dashboard Settings: poll-interval config write + read-back
# ---------------------------------------------------------------------------

class TestPollIntervalSetting:
    """The dashboard's "Check usage every" control writes
    poll_interval_minutes to the per-user config; _liveness_interval_secs()
    (and the _poll_interval_minutes display helper) must read it back live."""

    @pytest.fixture(autouse=True)
    def _isolate_config(self, tmp_path, monkeypatch):
        # Point the per-user config at a temp file and ensure no env override
        # masks the config value during these tests.
        cfg = tmp_path / "config.json"
        monkeypatch.setattr(widget_updater, "_config_path", lambda: cfg)
        monkeypatch.delenv("CLAUDE_POLL_INTERVAL_MINUTES", raising=False)
        self._cfg = cfg

    def test_write_then_read_minutes(self):
        widget_updater._write_config_value("poll_interval_minutes", 10)
        assert json.loads(self._cfg.read_text())["poll_interval_minutes"] == 10
        assert widget_updater._poll_interval_minutes() == 10

    def test_write_merges_with_existing_keys(self):
        self._cfg.write_text(json.dumps({"org_id": "abc"}))
        widget_updater._write_config_value("poll_interval_minutes", 5)
        data = json.loads(self._cfg.read_text())
        assert data["org_id"] == "abc"  # not clobbered
        assert data["poll_interval_minutes"] == 5

    def test_default_when_unset(self):
        # No config, no env -> default LIVENESS_INTERVAL_SECS (1200s = 20m).
        assert widget_updater._poll_interval_minutes() == 20

    def test_floor_clamp_reflected_in_minutes(self):
        # Below the 120s floor, the effective value is clamped up to 2m.
        widget_updater._write_config_value("poll_interval_minutes", 0.5)
        assert widget_updater._poll_interval_minutes() == 2


class TestLivenessTriggerSettings:
    """Config-driven one-shot pct list + delta-pct trigger, mirroring the
    poll-interval pattern."""

    @pytest.fixture(autouse=True)
    def _isolate_config(self, tmp_path, monkeypatch):
        cfg = tmp_path / "config.json"
        monkeypatch.setattr(widget_updater, "_config_path", lambda: cfg)
        monkeypatch.delenv("CLAUDE_LIVENESS_ONESHOT_PCTS", raising=False)
        monkeypatch.delenv("CLAUDE_LIVENESS_DELTA_PCT", raising=False)
        self._cfg = cfg

    def test_parse_pct_list_string(self):
        assert widget_updater._parse_pct_list("5, 10 ,95") == frozenset({5, 10, 95})

    def test_parse_pct_list_drops_out_of_range_and_garbage(self):
        assert widget_updater._parse_pct_list("0,5,abc,100,50") == frozenset({5, 50})

    def test_oneshot_default_when_unset(self):
        assert widget_updater._liveness_oneshot_pcts() == widget_updater.LIVENESS_ONE_SHOT_PCTS

    def test_oneshot_from_config(self):
        widget_updater._write_config_value("liveness_oneshot_pcts", [3, 50])
        assert widget_updater._liveness_oneshot_pcts() == frozenset({3, 50})

    def test_oneshot_empty_config_falls_back_to_default(self):
        widget_updater._write_config_value("liveness_oneshot_pcts", "garbage,nope")
        assert widget_updater._liveness_oneshot_pcts() == widget_updater.LIVENESS_ONE_SHOT_PCTS

    def test_delta_default_when_unset(self):
        assert widget_updater._liveness_delta_pct() == widget_updater.LIVENESS_PCT_DELTA_TRIGGER

    def test_delta_from_config(self):
        widget_updater._write_config_value("liveness_delta_pct", 4)
        assert widget_updater._liveness_delta_pct() == 4.0

    def test_delta_floored_at_one(self):
        widget_updater._write_config_value("liveness_delta_pct", 0.1)
        assert widget_updater._liveness_delta_pct() == 1.0


# ---------------------------------------------------------------------------
# Per-widget colour configuration
# ---------------------------------------------------------------------------

class TestColorStopsParser:
    """_parse_color_stops: CSV parsing, validation, ordering, garbage tolerance."""

    def test_single_stop(self):
        stops = widget_updater._parse_color_stops("90:#D64E2A")
        assert stops == [(90, "#D64E2A")]

    def test_multiple_stops_sorted(self):
        stops = widget_updater._parse_color_stops("90:#D64E2A,50:#ffcc00")
        assert stops == [(50, "#ffcc00"), (90, "#D64E2A")]

    def test_spaces_tolerated(self):
        stops = widget_updater._parse_color_stops("  90 : #D64E2A , 50 : #ffcc00 ")
        assert stops == [(50, "#ffcc00"), (90, "#D64E2A")]

    def test_three_char_hex_accepted(self):
        stops = widget_updater._parse_color_stops("50:#abc")
        assert stops == [(50, "#abc")]

    def test_invalid_hex_dropped(self):
        stops = widget_updater._parse_color_stops("50:notahex,90:#D64E2A")
        assert stops == [(90, "#D64E2A")]

    def test_out_of_range_pct_dropped(self):
        stops = widget_updater._parse_color_stops("-1:#aabbcc,101:#aabbcc,50:#2A78D6")
        assert stops == [(50, "#2A78D6")]

    def test_missing_colon_dropped(self):
        stops = widget_updater._parse_color_stops("90:#D64E2A,nodivider")
        assert stops == [(90, "#D64E2A")]

    def test_empty_string_returns_empty(self):
        assert widget_updater._parse_color_stops("") == []

    def test_none_returns_empty(self):
        assert widget_updater._parse_color_stops(None) == []

    def test_all_garbage_returns_empty(self):
        assert widget_updater._parse_color_stops("abc,xyz,!!") == []


class TestResolveWidgetColor:
    """resolve_widget_color: step resolution semantics, back-compat defaults."""

    def test_below_first_stop_returns_base(self):
        # pct=30 < 50 → no stop qualifies → base_color
        color = widget_updater.resolve_widget_color(30, "#2A78D6", "50:#ffcc00,90:#D64E2A")
        assert color == "#2A78D6"

    def test_at_first_stop_returns_stop_color(self):
        # pct=50 >= 50 → 50 qualifies, 90 does not → "#ffcc00"
        color = widget_updater.resolve_widget_color(50, "#2A78D6", "50:#ffcc00,90:#D64E2A")
        assert color == "#ffcc00"

    def test_between_stops_uses_lower_stop(self):
        # pct=75 → 50 qualifies, 90 does not → "#ffcc00"
        color = widget_updater.resolve_widget_color(75, "#2A78D6", "50:#ffcc00,90:#D64E2A")
        assert color == "#ffcc00"

    def test_at_second_stop_returns_second(self):
        # pct=90 → both qualify → highest qualifying stop is 90 → "#D64E2A"
        color = widget_updater.resolve_widget_color(90, "#2A78D6", "50:#ffcc00,90:#D64E2A")
        assert color == "#D64E2A"

    def test_above_all_stops_returns_highest(self):
        # pct=100 → all stops qualify → last/highest → "#D64E2A"
        color = widget_updater.resolve_widget_color(100, "#2A78D6", "50:#ffcc00,90:#D64E2A")
        assert color == "#D64E2A"

    def test_no_stops_returns_base(self):
        # Empty stops → base_color always
        color = widget_updater.resolve_widget_color(95, "#F5A623", "")
        assert color == "#F5A623"

    def test_none_stops_returns_base(self):
        color = widget_updater.resolve_widget_color(95, "#F5A623", None)
        assert color == "#F5A623"

    def test_garbage_stops_returns_base(self):
        color = widget_updater.resolve_widget_color(95, "#F5A623", "garbage,junk")
        assert color == "#F5A623"

    def test_single_stop_session_default(self):
        # Matches the built-in default: 90:#D64E2A threshold, base #2A78D6
        assert widget_updater.resolve_widget_color(89, "#2A78D6", "90:#D64E2A") == "#2A78D6"
        assert widget_updater.resolve_widget_color(90, "#2A78D6", "90:#D64E2A") == "#D64E2A"
        assert widget_updater.resolve_widget_color(99, "#2A78D6", "90:#D64E2A") == "#D64E2A"

    def test_pct_zero_returns_base(self):
        color = widget_updater.resolve_widget_color(0, "#2A78D6", "90:#D64E2A")
        assert color == "#2A78D6"


class TestWidgetConfigDefaults:
    """Back-compat: _widget_config returns built-in defaults when config absent."""

    @pytest.fixture(autouse=True)
    def _isolate_config(self, tmp_path, monkeypatch):
        # Point at a non-existent config so _read_config returns {}.
        monkeypatch.setattr(widget_updater, "_config_path",
                            lambda: tmp_path / "config.json")
        monkeypatch.setattr(widget_updater, "_bundled_config_path",
                            lambda: tmp_path / "bundled_config.json")

    def test_session_defaults(self):
        cfg = widget_updater._widget_config("session")
        assert cfg["base_color"]   == "#2A78D6"
        assert cfg["color_stops"]  == "90:#D64E2A"
        assert cfg["fill_mode"]    == "level"

    def test_weekly_defaults(self):
        cfg = widget_updater._widget_config("weekly")
        assert cfg["base_color"]   == "#F5A623"
        assert cfg["color_stops"]  == "90:#D64E2A"
        assert cfg["fill_mode"]    == "level"

    def test_clock_defaults(self):
        cfg = widget_updater._widget_config("clock")
        assert cfg["base_color"]   == "#2A78D6"
        assert cfg["color_stops"]  == "90:#D64E2A"
        assert cfg["fill_mode"]    == "angular"

    def test_partial_override_merges(self, tmp_path, monkeypatch):
        # When the user has only set base_color, the other keys stay as defaults.
        p = tmp_path / "config.json"
        p.write_text(json.dumps({"widgets": {"session": {"base_color": "#ff0000"}}}),
                     encoding="utf-8")
        monkeypatch.setattr(widget_updater, "_config_path", lambda: p)
        cfg = widget_updater._widget_config("session")
        assert cfg["base_color"]  == "#ff0000"
        assert cfg["color_stops"] == "90:#D64E2A"   # default preserved
        assert cfg["fill_mode"]   == "level"         # default preserved

    def test_all_three_widgets_returned(self, tmp_path):
        result = widget_updater._read_widget_config_all()
        assert set(result.keys()) == {"session", "weekly", "clock"}


class TestWriteWidgetColor:
    """_write_widget_color: persists individual fields; merges with existing config."""

    @pytest.fixture(autouse=True)
    def _isolate_config(self, tmp_path, monkeypatch):
        cfg = tmp_path / "config.json"
        monkeypatch.setattr(widget_updater, "_config_path", lambda: cfg)
        self._cfg = cfg

    def test_write_base_color(self):
        widget_updater._write_widget_color("session", "base_color", "#ff0000")
        data = json.loads(self._cfg.read_text())
        assert data["widgets"]["session"]["base_color"] == "#ff0000"

    def test_write_color_stops(self):
        widget_updater._write_widget_color("weekly", "color_stops", "80:#ff8800,95:#ff0000")
        data = json.loads(self._cfg.read_text())
        assert data["widgets"]["weekly"]["color_stops"] == "80:#ff8800,95:#ff0000"

    def test_write_fill_mode(self):
        widget_updater._write_widget_color("clock", "fill_mode", "angular")
        data = json.loads(self._cfg.read_text())
        assert data["widgets"]["clock"]["fill_mode"] == "angular"

    def test_merges_with_existing_config_keys(self):
        # Existing non-widget keys must not be clobbered.
        self._cfg.write_text(json.dumps({"org_id": "abc"}), encoding="utf-8")
        widget_updater._write_widget_color("session", "base_color", "#123456")
        data = json.loads(self._cfg.read_text())
        assert data["org_id"] == "abc"
        assert data["widgets"]["session"]["base_color"] == "#123456"

    def test_unknown_widget_raises(self):
        with pytest.raises(ValueError, match="unknown widget"):
            widget_updater._write_widget_color("bogus", "base_color", "#ff0000")

    def test_unknown_field_raises(self):
        with pytest.raises(ValueError, match="unknown field"):
            widget_updater._write_widget_color("session", "border_radius", "5px")


# ---------------------------------------------------------------------------
# Custom shapes + per-widget show_text  (feature: upload + show-text)
# ---------------------------------------------------------------------------

# These tests exercise the pure shape-processing module and the config
# read/write of the new `shape` / `show_text` fields. They import widget_shapes
# directly (Pillow-only, no network/GUI) so they run anywhere the other tests do.
import io as _io

try:
    import widget_shapes
    from PIL import Image
    _SHAPES_OK = True
except Exception as _e:  # pragma: no cover - only when Pillow is missing
    _SHAPES_OK = False


def _png_bytes(img) -> bytes:
    buf = _io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


@pytest.mark.skipif(not _SHAPES_OK, reason="widget_shapes/Pillow unavailable")
class TestRasterToMask:
    """raster_bytes_to_mask: non-black pixels traced, black dropped."""

    def test_white_square_kept(self):
        # A white block on black: the block becomes the (normalised) silhouette.
        img = Image.new("RGB", (40, 40), (0, 0, 0))
        for x in range(10, 30):
            for y in range(10, 30):
                img.putpixel((x, y), (255, 255, 255))
        mask = widget_shapes.raster_bytes_to_mask(_png_bytes(img))
        assert mask.mode == "L"
        assert mask.size == (widget_shapes.REF_SIZE, widget_shapes.REF_SIZE)
        # The silhouette is trimmed-then-centred to fill the box, so the centre
        # pixel must be fully opaque.
        c = widget_shapes.REF_SIZE // 2
        assert mask.getpixel((c, c)) == 255

    def test_all_black_raises(self):
        img = Image.new("RGB", (20, 20), (0, 0, 0))
        with pytest.raises(widget_shapes.ShapeError):
            widget_shapes.raster_bytes_to_mask(_png_bytes(img))

    def test_near_black_dropped_colour_kept(self):
        # Left half near-black (below threshold) is dropped; right half blue is
        # kept. After trim+centre the silhouette is just the right half, so the
        # mask's content bbox must be non-empty and roughly fill the canvas.
        img = Image.new("RGB", (40, 40), (0, 0, 0))
        for x in range(20, 40):
            for y in range(0, 40):
                img.putpixel((x, y), (0, 120, 255))
        mask = widget_shapes.raster_bytes_to_mask(_png_bytes(img))
        assert mask.getbbox() is not None

    def test_transparent_background_ignored(self):
        # A black silhouette on a TRANSPARENT background must still trace: the
        # alpha gate keeps the opaque (even if black) pixels and drops the
        # transparent ones.
        img = Image.new("RGBA", (40, 40), (0, 0, 0, 0))
        for x in range(12, 28):
            for y in range(12, 28):
                img.putpixel((x, y), (0, 0, 0, 255))  # opaque black
        mask = widget_shapes.raster_bytes_to_mask(_png_bytes(img))
        assert mask.getbbox() is not None


@pytest.mark.skipif(not _SHAPES_OK, reason="widget_shapes/Pillow unavailable")
class TestSvgToMask:
    """svg_bytes_to_mask: simple paths/shapes flatten; junk SVG errors."""

    def test_triangle_path(self):
        svg = b'<svg viewBox="0 0 100 100"><path d="M 10 10 L 90 10 L 50 90 Z"/></svg>'
        mask = widget_shapes.svg_bytes_to_mask(svg)
        assert mask.mode == "L"
        assert mask.getbbox() is not None

    def test_rect_shape(self):
        svg = b'<svg viewBox="0 0 100 100"><rect x="20" y="20" width="60" height="60"/></svg>'
        mask = widget_shapes.svg_bytes_to_mask(svg)
        assert mask.getbbox() is not None

    def test_empty_svg_raises(self):
        svg = b'<svg viewBox="0 0 100 100"></svg>'
        with pytest.raises(widget_shapes.ShapeError):
            widget_shapes.svg_bytes_to_mask(svg)

    def test_invalid_xml_raises(self):
        with pytest.raises(widget_shapes.ShapeError):
            widget_shapes.svg_bytes_to_mask(b'<svg><path d="M 0 0')


@pytest.mark.skipif(not _SHAPES_OK, reason="widget_shapes/Pillow unavailable")
class TestProcessUpload:
    """process_upload: dispatch + validation (oversize / wrong type / empty)."""

    def test_dispatch_png(self):
        img = Image.new("RGB", (20, 20), (255, 255, 255))
        mask = widget_shapes.process_upload(_png_bytes(img), "shape.png")
        assert mask.size == (widget_shapes.REF_SIZE, widget_shapes.REF_SIZE)

    def test_dispatch_svg_by_extension(self):
        svg = b'<svg viewBox="0 0 10 10"><rect x="1" y="1" width="8" height="8"/></svg>'
        mask = widget_shapes.process_upload(svg, "shape.svg")
        assert mask.getbbox() is not None

    def test_dispatch_svg_by_content_sniff(self):
        # No filename, but the body starts with <svg => routed to the flattener.
        svg = b'<svg viewBox="0 0 10 10"><rect x="1" y="1" width="8" height="8"/></svg>'
        mask = widget_shapes.process_upload(svg, None, "application/octet-stream")
        assert mask.getbbox() is not None

    def test_oversize_rejected(self):
        with pytest.raises(widget_shapes.ShapeError, match="too large"):
            widget_shapes.process_upload(b"x" * (widget_shapes.MAX_UPLOAD_BYTES + 1),
                                         "big.png")

    def test_empty_rejected(self):
        with pytest.raises(widget_shapes.ShapeError, match="empty"):
            widget_shapes.process_upload(b"", "x.png")

    def test_wrong_type_rejected(self):
        with pytest.raises(widget_shapes.ShapeError, match="unsupported"):
            widget_shapes.process_upload(b"%PDF-1.4 ...", "doc.pdf")

    def test_save_and_load_roundtrip(self, tmp_path):
        img = Image.new("RGB", (30, 30), (0, 0, 0))
        for x in range(8, 22):
            for y in range(4, 26):
                img.putpixel((x, y), (0, 200, 255))
        mask = widget_shapes.raster_bytes_to_mask(_png_bytes(img))
        fname = widget_shapes.save_mask(mask, tmp_path, "session")
        assert (tmp_path / fname).exists()
        loaded = widget_shapes.load_mask(tmp_path / fname, 64)
        assert loaded.size == (64, 64)
        # load_mask re-binarises to a crisp 0/255 mask for the fill pipeline.
        assert set(loaded.get_flattened_data()) <= {0, 255}


@pytest.mark.skipif(not _SHAPES_OK, reason="widget_shapes/Pillow unavailable")
class TestShapeConfigDefaults:
    """Back-compat: shape/show_text default to ''/True when config is absent."""

    @pytest.fixture(autouse=True)
    def _isolate_config(self, tmp_path, monkeypatch):
        missing = tmp_path / "nope.json"
        monkeypatch.setattr(widget_updater, "_config_path", lambda: missing)
        monkeypatch.setattr(widget_updater, "_bundled_config_path", lambda: missing)

    def test_shape_default_empty(self):
        for name in ("session", "weekly", "clock"):
            assert widget_updater._widget_config(name)["shape"] == ""

    def test_show_text_default_true(self):
        for name in ("session", "weekly", "clock"):
            assert widget_updater._widget_show_text(name) is True

    def test_shape_path_none_when_unset(self):
        assert widget_updater._widget_shape_path("session") is None


class TestShapeShowTextWrite:
    """_write_widget_field persists shape/show_text; helpers read them back."""

    @pytest.fixture(autouse=True)
    def _isolate_config(self, tmp_path, monkeypatch):
        cfg = tmp_path / "config.json"
        monkeypatch.setattr(widget_updater, "_config_path", lambda: cfg)
        monkeypatch.setattr(widget_updater, "_bundled_config_path",
                            lambda: tmp_path / "nope.json")
        self._cfg = cfg

    def test_write_show_text_false(self):
        widget_updater._write_widget_field("session", "show_text", False)
        data = json.loads(self._cfg.read_text())
        assert data["widgets"]["session"]["show_text"] is False
        assert widget_updater._widget_show_text("session") is False

    def test_write_shape_filename(self):
        widget_updater._write_widget_field("weekly", "shape", "shape_weekly.png")
        data = json.loads(self._cfg.read_text())
        assert data["widgets"]["weekly"]["shape"] == "shape_weekly.png"

    def test_shape_path_none_when_file_missing(self):
        # A recorded filename whose file doesn't exist resolves to None, so a
        # deleted mask cleanly reverts to the built-in shape.
        widget_updater._write_widget_field("clock", "shape", "ghost_missing.png")
        assert widget_updater._widget_shape_path("clock") is None

    def test_write_field_unknown_widget_raises(self):
        with pytest.raises(ValueError, match="unknown widget"):
            widget_updater._write_widget_field("bogus", "shape", "x.png")

    def test_show_text_reflects_config(self):
        # When shapes dir has the file, _widget_shape_path returns it.
        import widget_shapes
        from PIL import Image
        shapes_dir = self._cfg.parent / "shapes"
        shapes_dir.mkdir(parents=True, exist_ok=True)
        Image.new("L", (10, 10), 255).save(shapes_dir / "shape_session.png")
        # Point SHAPES_DIR at our temp dir for the duration of this assertion.
        import unittest.mock as mock
        with mock.patch.object(widget_updater, "SHAPES_DIR", shapes_dir):
            widget_updater._write_widget_field("session", "shape", "shape_session.png")
            p = widget_updater._widget_shape_path("session")
            assert p is not None and p.name == "shape_session.png"


# ---------------------------------------------------------------------------
# _snap_session_start — jitter tolerance
# ---------------------------------------------------------------------------

class TestSnapSessionStart:
    """_snap_session_start returns stored start when within 30s, new start otherwise."""

    def _make(self, iso: str) -> "datetime":
        from datetime import datetime
        return datetime.fromisoformat(iso)

    def test_exact_match_returns_stored(self):
        stored = "2026-05-31T10:00:00+00:00"
        new = self._make(stored)
        assert widget_updater._snap_session_start(stored, new).isoformat() == stored

    def test_within_jitter_returns_stored(self):
        stored = "2026-05-31T10:00:00+00:00"
        new = self._make("2026-05-31T10:00:01.500000+00:00")  # 1.5s later
        assert widget_updater._snap_session_start(stored, new).isoformat() == stored

    def test_at_boundary_returns_stored(self):
        stored = "2026-05-31T10:00:00+00:00"
        new = self._make("2026-05-31T10:00:30+00:00")  # exactly 30s
        assert widget_updater._snap_session_start(stored, new).isoformat() == stored

    def test_beyond_boundary_returns_new(self):
        stored = "2026-05-31T10:00:00+00:00"
        new = self._make("2026-05-31T10:00:31+00:00")  # 31s — genuinely new session
        result = widget_updater._snap_session_start(stored, new)
        assert result == new

    def test_no_stored_returns_new(self):
        new = self._make("2026-05-31T10:00:00+00:00")
        assert widget_updater._snap_session_start(None, new) == new

    def test_bad_stored_returns_new(self):
        new = self._make("2026-05-31T10:00:00+00:00")
        assert widget_updater._snap_session_start("not-a-date", new) == new


# ---------------------------------------------------------------------------
# API response contract
#
# Pins _make_raw (used by test fakes) to the shape the real parsers accept.
# If the fake drifts from the real API format, these tests fail here rather
# than silently returning None through the parsers.
# ---------------------------------------------------------------------------

class TestAPIResponseContract:
    def test_make_raw_parseable_by_session_parser(self):
        raw = _make_raw()
        start, end, pct = widget_updater._parse_session(raw)
        assert pct is not None, "_parse_session returned None — _make_raw shape drifted"
        assert start is not None
        assert end is not None

    def test_make_raw_parseable_by_weekly_parser(self):
        raw = _make_raw()
        pct, end = widget_updater._parse_weekly(raw)
        assert pct is not None, "_parse_weekly returned None — _make_raw shape drifted"


# ---------------------------------------------------------------------------
# Per-model weighting (weighted_v3): a session mixes models on one meter
# (e.g. Opus chat + Sonnet/Haiku agents); _weighted_io sums each model's
# components at its own calibrated weights. Falls back to global counters x
# default (Opus) weights when by_model can't be trusted.
# ---------------------------------------------------------------------------
class TestPerModelWeighting:
    def test_model_class_mapping(self):
        mc = widget_updater._model_class
        assert mc("claude-opus-4-8") == "opus"
        assert mc("claude-sonnet-4-6") == "sonnet"
        assert mc("claude-haiku-4-5-20251001") == "haiku"
        assert mc("some-unknown-model") == "opus"   # unknown -> Opus default
        assert mc("") == "opus"
        assert mc(None) == "opus"

    def _entry(self, i, o, c1, c5, cr):
        return {"input": i, "output": o, "cache_write_1h": c1,
                "cache_write_5m": c5, "cache_read": cr}

    def test_weighted_io_sums_per_model(self):
        # 1000 of each component per model, consistent with the global counters.
        state = widget_updater._empty_state()
        state["by_model"] = {
            "claude-opus-4-8":   self._entry(1000, 1000, 1000, 0, 1000),
            "claude-sonnet-4-6": self._entry(1000, 1000, 1000, 0, 1000),
            "claude-haiku-4-5":  self._entry(1000, 1000, 1000, 0, 1000),
        }
        state["input_tokens"]  = 3000   # == Σ by_model input  (consistency gate)
        state["output_tokens"] = 3000   # == Σ by_model output
        W = widget_updater.MODEL_WEIGHTS
        exp = 0
        for cls, m in (("opus", "claude-opus-4-8"),
                       ("sonnet", "claude-sonnet-4-6"),
                       ("haiku", "claude-haiku-4-5")):
            w = W[cls]
            exp += (1000 * w["input"] + 1000 * w["output"] + 1000 * w["cache_write_1h"]
                    + 0 * w["cache_write_5m"] + 1000 * w["cache_read"])
        assert widget_updater._weighted_io(state) == round(exp)
        # Sanity: Sonnet output cheaper than Opus, so the sum is below 3x Opus-only.
        assert exp < 3 * (1000 * W["opus"]["input"] + 1000 * W["opus"]["output"]
                          + 1000 * W["opus"]["cache_write_1h"])

    def test_falls_back_when_by_model_inconsistent_with_counters(self):
        # by_model present (with cache) but does NOT sum to the global counters
        # (e.g. counters seeded directly) -> must use the global fallback, not
        # silently drop the unaccounted tokens.
        state = widget_updater._empty_state()
        state["by_model"] = {"claude-opus-4-8": self._entry(2000, 2000, 0, 0, 0)}
        state["input_tokens"]  = 10000   # != by_model's 2000
        state["output_tokens"] = 0
        # fallback = global x default(Opus) weights
        exp = 10000 * widget_updater.DEFAULT_WEIGHTS["input"]
        assert widget_updater._weighted_io(state) == round(exp)

    def test_falls_back_when_by_model_has_no_cache(self):
        # Old-style by_model (input/output only) -> use global fallback so cache
        # (tracked only globally pre-migration) isn't dropped.
        state = widget_updater._empty_state()
        state["by_model"] = {"claude-opus-4-8": {"input": 2000, "output": 0}}
        state["input_tokens"]  = 2000    # consistent, but no cache keys present
        state["output_tokens"] = 0
        state["cache_write_1h"] = 4000   # only in the global counter
        d = widget_updater.DEFAULT_WEIGHTS
        exp = 2000 * d["input"] + 4000 * d["cache_write_1h"]
        assert widget_updater._weighted_io(state) == round(exp)

    def test_io_unit_migration_resets_accumulation(self, tmp_path):
        # Loading a pre-v3 state must drop the old-basis budget AND reset the
        # token accumulation (so a re-scan repopulates by_model WITH the cache
        # split), while preserving session_start.
        import json as _json
        old = {
            "io_unit": "weighted_v1",
            "seen_ids": ["msg_a", "msg_b"],
            "offsets": {"/x.jsonl": 123},
            "input_tokens": 5000, "output_tokens": 4000,
            "cache_write_1h": 2000, "cache_write_5m": 0, "cache_read": 9000,
            "by_model": {"claude-opus-4-8": {"input": 5000, "output": 4000}},
            "implied_session_budget": 999999,
            "anchor_pct": 42.0, "anchor_io": 12345,
            "session_start": "2099-01-01T00:00:00+00:00",
        }
        widget_updater.STATE_FILE.write_text(_json.dumps(old), encoding="utf-8")
        s = widget_updater._load_state()
        assert s["io_unit"] == widget_updater.IO_UNIT
        # accumulation reset for a clean per-model re-scan
        assert s["input_tokens"] == 0 and s["output_tokens"] == 0
        assert s["cache_write_1h"] == 0 and s["cache_read"] == 0
        assert s["by_model"] == {}
        assert s["seen_ids"] == set()
        assert s["offsets"] == {}
        # old-basis budget/anchors dropped
        assert "implied_session_budget" not in s
        assert "anchor_pct" not in s
        # but session identity preserved
        assert s["session_start"] == "2099-01-01T00:00:00+00:00"

    def test_io_unit_migration_clears_session_factor(self, tmp_path):
        import json as _json
        old = {
            "io_unit": "weighted_v1",
            "seen_ids": [],
            "session_factor": 0.000042,
            "session_sf_grabs": [{"pct": 10, "by_model": {}}],
            "session_start": "2099-01-01T00:00:00+00:00",
        }
        widget_updater.STATE_FILE.write_text(_json.dumps(old), encoding="utf-8")
        s = widget_updater._load_state()
        assert s["session_factor"] is None
        assert s["session_sf_grabs"] == []


# ---------------------------------------------------------------------------
# Piece 1 — SessionFactor: _set_anchor + _estimate_session_pct
# ---------------------------------------------------------------------------

class TestSessionFactor:
    """session_factor = min(pct/io) over above-floor grabs; used by estimate."""

    def _state_with_tokens(self, inp, out):
        s = widget_updater._empty_state(datetime(2099, 1, 1, tzinfo=timezone.utc))
        s["input_tokens"] = inp
        s["output_tokens"] = out
        return s

    def test_empty_state_has_no_session_factor(self):
        s = widget_updater._empty_state()
        assert s["session_factor"] is None
        assert s["session_sf_grabs"] == []

    def test_set_anchor_sets_session_factor_above_floor(self, make_handler, monkeypatch):
        monkeypatch.setattr(widget_updater, "_append_calibration", mock.Mock())
        monkeypatch.setattr(widget_updater, "full_scan", mock.Mock())
        h = make_handler()
        h.state["input_tokens"] = 50000
        h.state["output_tokens"] = 100000
        pct = widget_updater.CALIBRATION_PCT_FLOOR + 5  # above floor
        h._set_anchor(float(pct))
        io_now = widget_updater._weighted_io(h.state)
        # s_g uses the floor-rounding midpoint: (pct + 0.5) / io
        expected_sf = (pct + widget_updater.API_PCT_FLOOR_BIAS_PP) / io_now
        assert h.state["session_factor"] == pytest.approx(expected_sf)

    def test_set_anchor_below_floor_does_not_set_session_factor(self, make_handler):
        h = make_handler()
        h.state["input_tokens"] = 10000
        h.state["output_tokens"] = 5000
        pct = widget_updater.CALIBRATION_PCT_FLOOR - 1  # below floor
        h._set_anchor(float(pct))
        assert h.state["session_factor"] is None

    def test_set_anchor_tracks_minimum_over_multiple_grabs(self, make_handler):
        h = make_handler()
        # First grab: pct=10, io determined by tokens
        h.state["input_tokens"] = 100000
        h.state["output_tokens"] = 50000
        io1 = widget_updater._weighted_io(h.state)
        h._set_anchor(10.0)
        sf_after_1 = h.state["session_factor"]
        # s_g uses floor-rounding midpoint: (pct + 0.5) / io
        assert sf_after_1 == pytest.approx((10.0 + widget_updater.API_PCT_FLOOR_BIAS_PP) / io1)

        # Second grab: add more tokens, higher pct -> higher s_g (contaminated)
        h.state["input_tokens"] += 100000
        h.state["output_tokens"] += 100000
        h._set_anchor(50.0)  # s_g2 = 50 / io2 > s_g1 (pct grew disproportionately)
        # session_factor must stay at the LOWER value (from grab 1)
        assert h.state["session_factor"] == pytest.approx(sf_after_1)

    def test_set_anchor_updates_sf_grabs_list(self, make_handler):
        h = make_handler()
        h.state["input_tokens"] = 80000
        h.state["output_tokens"] = 40000
        h.state["by_model"] = {
            "claude-opus-4-8": {"input": 80000, "output": 40000,
                                "cache_write_1h": 0, "cache_write_5m": 0,
                                "cache_read": 0},
        }
        h._set_anchor(15.0)
        grabs = h.state["session_sf_grabs"]
        assert len(grabs) == 1
        assert grabs[0]["pct"] == 15.0
        assert "claude-opus-4-8" in grabs[0]["by_model"]

    def test_estimate_uses_session_factor_when_set(self):
        s = widget_updater._empty_state(datetime(2099, 1, 1, tzinfo=timezone.utc))
        s["input_tokens"] = 100000
        s["output_tokens"] = 50000
        io = widget_updater._weighted_io(s)
        sf = 20.0 / io  # implies budget = io / 0.20
        s["session_factor"] = sf
        s["implied_session_budget"] = 999999  # would give a different answer
        assert widget_updater._estimate_session_pct(s) == pytest.approx(20.0, abs=0.1)

    def test_estimate_fallback_to_budget_when_no_session_factor(self):
        s = widget_updater._empty_state(datetime(2099, 1, 1, tzinfo=timezone.utc))
        s["input_tokens"] = 40000
        s["output_tokens"] = 60000
        s["implied_session_budget"] = widget_updater._weighted_io(s) * 4  # 25%
        # session_factor is None (not set)
        assert widget_updater._estimate_session_pct(s) == 25.0

    def test_estimate_clamps_at_100_with_session_factor(self):
        s = widget_updater._empty_state(datetime(2099, 1, 1, tzinfo=timezone.utc))
        s["input_tokens"] = 500000
        s["output_tokens"] = 200000
        io = widget_updater._weighted_io(s)
        s["session_factor"] = 200.0 / io  # would predict 200% unclamped
        assert widget_updater._estimate_session_pct(s) == 100

    def test_off_laptop_spike_does_not_lower_session_factor(self, make_handler):
        # Simulate: 2 clean grabs, then 1 off-laptop spike (pct inflated).
        h = make_handler()
        h.state["input_tokens"] = 100000
        h.state["output_tokens"] = 50000
        io1 = widget_updater._weighted_io(h.state)
        h._set_anchor(10.0)   # clean grab 1: s_g = 10/io1
        sf_clean = h.state["session_factor"]

        h.state["input_tokens"] += 50000   # some more local work
        h._set_anchor(40.0)               # off-laptop spike: pct jumped 30pp but only ~50k new tokens
        # The spike's s_g is much higher, so session_factor should NOT decrease.
        assert h.state["session_factor"] == pytest.approx(sf_clean)

    def test_set_anchor_records_n_messages_in_sf_grabs(self, make_handler):
        """n_messages (Task 4b) is recorded in each sf_grabs snapshot so
        per-turn fixed cost F can be estimated offline later."""
        h = make_handler()
        h.state["input_tokens"] = 100000
        h.state["output_tokens"] = 50000
        # Seed seen_ids with a known count so we can assert against it.
        h.state["seen_ids"] = {"msg1", "msg2", "msg3"}
        pct = float(widget_updater.CALIBRATION_PCT_FLOOR + 2)
        h._set_anchor(pct)
        grabs = h.state["session_sf_grabs"]
        assert len(grabs) == 1
        assert grabs[0]["n_messages"] == 3

    def test_s_g_uses_midpoint_at_floor(self, make_handler):
        """s_g = (pct + API_PCT_FLOOR_BIAS_PP) / io at the CALIBRATION_PCT_FLOOR.

        Under the current round-to-nearest presumption the bias is 0.0, so the
        midpoint IS pct and s_g == pct/io. The test pins s_g to the bias constant
        so it stays correct whichever rounding presumption is in force (it would
        be (pct+0.5)/io again if we ever revert to floor)."""
        h = make_handler()
        h.state["input_tokens"] = 100000
        h.state["output_tokens"] = 0
        io = widget_updater._weighted_io(h.state)
        pct = float(widget_updater.CALIBRATION_PCT_FLOOR)   # e.g. 5.0
        h._set_anchor(pct)
        expected_sf = (pct + widget_updater.API_PCT_FLOOR_BIAS_PP) / io
        assert h.state["session_factor"] == pytest.approx(expected_sf)

    def test_estimate_never_falls_below_anchor_plus_bias(self, make_handler):
        """After an API grab at pct P, the local estimate must never display
        LESS than P + API_PCT_FLOOR_BIAS_PP, even when session_factor is very
        small (snap-down regression guard, Task 2 part i)."""
        h = make_handler()
        # Seed a tiny session_factor so s * io_total would be well below P.
        h.state["input_tokens"] = 10000
        h.state["output_tokens"] = 0
        io_at_anchor = widget_updater._weighted_io(h.state)
        anchor_p = 30.0
        # Give it a session_factor derived at the anchor.
        h.state["session_factor"] = (anchor_p + widget_updater.API_PCT_FLOOR_BIAS_PP) / io_at_anchor
        h.state["anchor_pct"] = anchor_p
        h.state["anchor_io"] = io_at_anchor
        # Now simulate off-laptop usage: the meter jumped to 60% but we only have the
        # same local tokens, so s * io_total would be tiny relative to the new anchor.
        h.state["anchor_pct"] = 60.0   # off-laptop drove the API reading up
        h.state["anchor_io"] = io_at_anchor  # our local io hasn't grown
        est = widget_updater._estimate_session_pct(h.state)
        floor_value = 60.0 + widget_updater.API_PCT_FLOOR_BIAS_PP   # = 60.5
        assert est is not None
        assert est >= floor_value, (
            f"estimate {est} fell below anchor level {floor_value}"
        )

    def test_local_estimate_uses_sf_immediately_after_anchor(self, make_handler, monkeypatch):
        # After _adopt_api_pct sets an above-floor anchor, _local_estimate uses SF.
        monkeypatch.setattr(widget_updater, "_append_calibration", mock.Mock())
        monkeypatch.setattr(widget_updater, "full_scan", mock.Mock())
        h = make_handler()
        h.state["input_tokens"] = 120000
        h.state["output_tokens"] = 80000
        h.state["by_model"] = {
            "claude-opus-4-8": {"input": 120000, "output": 80000,
                                "cache_write_1h": 0, "cache_write_5m": 0,
                                "cache_read": 0}
        }
        h.state["implied_session_budget"] = 999999
        pct = 20.0
        h._adopt_api_pct(pct, datetime.now(timezone.utc))
        # No new tokens — estimate is anchor_pct + floor bias (0.5pp), not anchor_pct itself.
        expected = pct + widget_updater.API_PCT_FLOOR_BIAS_PP
        assert h._local_estimate() == pytest.approx(expected, abs=0.1)


# ---------------------------------------------------------------------------
# Piece 2 — _compute_weight_nudge
# ---------------------------------------------------------------------------

def _make_grabs(pct_list, output_list, cw1h_list, model="claude-opus-4-8"):
    """Build session_sf_grabs entries with controlled composition."""
    grabs = []
    for pct, out, cw in zip(pct_list, output_list, cw1h_list):
        grabs.append({
            "pct": pct,
            "by_model": {
                model: {
                    "input": 50000,
                    "output": out,
                    "cache_write_1h": cw,
                    "cache_write_5m": 0,
                    "cache_read": 0,
                }
            },
        })
    return grabs


class TestWeightNudge:
    """_compute_weight_nudge correctness, guards, and off-laptop robustness."""

    W = property(lambda self: widget_updater.MODEL_WEIGHTS)

    def test_returns_none_when_too_few_grabs(self):
        # Fewer than WEIGHT_NUDGE_MIN_GRABS → no signal.
        grabs = _make_grabs([10], [10000], [50000])
        assert widget_updater._compute_weight_nudge(grabs, widget_updater.MODEL_WEIGHTS) is None

    def test_returns_none_when_composition_constant(self):
        # Identical composition in every grab → no identifiable direction.
        grabs = _make_grabs([10, 20, 30], [10000, 20000, 30000], [50000, 100000, 150000])
        # All grabs have output:cw1h ratio = 1:5; no variation in cost-fraction.
        result = widget_updater._compute_weight_nudge(grabs, widget_updater.MODEL_WEIGHTS)
        # Should return None (nothing identifiable) or the SAME weights (no nudge).
        # We just verify it doesn't crash and doesn't inflate weights wildly.
        if result is not None:
            for mc, wv in result.items():
                for k, v in wv.items():
                    orig = widget_updater.MODEL_WEIGHTS.get(mc, {}).get(k, v)
                    assert abs(v - orig) / max(orig, 0.01) < 0.5  # <50% change

    def test_nudge_increases_output_weight_when_output_heavy_grabs_high_s(self):
        # Output-heavy grabs should have HIGHER s_g if output weight is too low.
        # This tests gradient direction: should push output weight up.
        W = dict(widget_updater.MODEL_WEIGHTS)
        # Deliberately underweight output so output-heavy grabs have higher s_g.
        W_low = {"opus": dict(W["opus"]), "sonnet": dict(W["sonnet"]), "haiku": dict(W["haiku"])}
        W_low["opus"]["output"] = 1.0  # far below true ~7.5

        # 5 grabs: alternating output-heavy (high pct) and cw1h-heavy (lower pct)
        grabs = [
            {"pct": 30, "by_model": {"claude-opus-4-8": {
                "input": 20000, "output": 100000, "cache_write_1h": 10000,
                "cache_write_5m": 0, "cache_read": 0}}},
            {"pct": 10, "by_model": {"claude-opus-4-8": {
                "input": 20000, "output": 5000, "cache_write_1h": 100000,
                "cache_write_5m": 0, "cache_read": 0}}},
            {"pct": 28, "by_model": {"claude-opus-4-8": {
                "input": 20000, "output": 90000, "cache_write_1h": 10000,
                "cache_write_5m": 0, "cache_read": 0}}},
            {"pct": 11, "by_model": {"claude-opus-4-8": {
                "input": 20000, "output": 5000, "cache_write_1h": 110000,
                "cache_write_5m": 0, "cache_read": 0}}},
            {"pct": 32, "by_model": {"claude-opus-4-8": {
                "input": 20000, "output": 110000, "cache_write_1h": 10000,
                "cache_write_5m": 0, "cache_read": 0}}},
        ]
        result = widget_updater._compute_weight_nudge(grabs, W_low)
        if result is not None:
            # Output weight should be nudged UP relative to W_low (gradient points up).
            orig_out = W_low["opus"]["output"]
            new_out  = result.get("opus", {}).get("output", orig_out)
            assert new_out >= orig_out  # nudged up or stayed the same

    def test_gauge_renorm_preserves_cw1h_opus_eq_1(self):
        grabs = [
            {"pct": 20, "by_model": {"claude-opus-4-8": {
                "input": 30000, "output": 80000, "cache_write_1h": 50000,
                "cache_write_5m": 0, "cache_read": 0}}},
            {"pct": 10, "by_model": {"claude-opus-4-8": {
                "input": 30000, "output": 10000, "cache_write_1h": 200000,
                "cache_write_5m": 0, "cache_read": 0}}},
            {"pct": 25, "by_model": {"claude-opus-4-8": {
                "input": 30000, "output": 100000, "cache_write_1h": 30000,
                "cache_write_5m": 0, "cache_read": 0}}},
        ]
        result = widget_updater._compute_weight_nudge(grabs, widget_updater.MODEL_WEIGHTS)
        if result is not None and "opus" in result:
            assert result["opus"]["cache_write_1h"] == pytest.approx(1.0, rel=0.01)

    def test_off_laptop_spike_excluded_by_lower_envelope(self):
        # 4 grabs: 3 clean + 1 off-laptop spike (same local tokens, much higher pct).
        clean = [
            {"pct": 10, "by_model": {"claude-opus-4-8": {
                "input": 20000, "output": 50000, "cache_write_1h": 100000,
                "cache_write_5m": 0, "cache_read": 0}}},
            {"pct": 15, "by_model": {"claude-opus-4-8": {
                "input": 30000, "output": 70000, "cache_write_1h": 150000,
                "cache_write_5m": 0, "cache_read": 0}}},
            {"pct": 12, "by_model": {"claude-opus-4-8": {
                "input": 25000, "output": 55000, "cache_write_1h": 120000,
                "cache_write_5m": 0, "cache_read": 0}}},
        ]
        spike = {"pct": 80, "by_model": {"claude-opus-4-8": {
            "input": 25000, "output": 55000, "cache_write_1h": 120000,
            "cache_write_5m": 0, "cache_read": 0}}}
        grabs_with_spike = clean + [spike]

        result_clean = widget_updater._compute_weight_nudge(clean, widget_updater.MODEL_WEIGHTS)
        result_spike = widget_updater._compute_weight_nudge(grabs_with_spike,
                                                            widget_updater.MODEL_WEIGHTS)
        # Adding an off-laptop spike should NOT materially change the nudge,
        # because the spike is in the upper s_g range and excluded.
        if result_clean is not None and result_spike is not None:
            for mc in result_clean:
                for k in result_clean[mc]:
                    r_clean = result_clean[mc][k]
                    r_spike = result_spike.get(mc, {}).get(k, r_clean)
                    # The two results should be similar (spike excluded).
                    assert abs(r_clean - r_spike) < 0.5 * max(abs(r_clean), 0.01)

    def test_weights_all_positive_after_nudge(self):
        grabs = [
            {"pct": 20, "by_model": {"claude-opus-4-8": {
                "input": 30000, "output": 90000, "cache_write_1h": 30000,
                "cache_write_5m": 0, "cache_read": 0}}},
            {"pct": 8, "by_model": {"claude-opus-4-8": {
                "input": 30000, "output": 5000, "cache_write_1h": 200000,
                "cache_write_5m": 0, "cache_read": 0}}},
            {"pct": 18, "by_model": {"claude-opus-4-8": {
                "input": 30000, "output": 80000, "cache_write_1h": 35000,
                "cache_write_5m": 0, "cache_read": 0}}},
        ]
        result = widget_updater._compute_weight_nudge(grabs, widget_updater.MODEL_WEIGHTS)
        if result is not None:
            for mc, wv in result.items():
                for k, v in wv.items():
                    assert v >= 0, f"weight {mc}.{k}={v} went non-positive"

    def test_nudge_slope_corrects_wrong_output_weight(self):
        """Regression slope implementation: synthetic session where the assumed
        output weight is 2x the true value.  Because s_g = pct / c_g and c_g
        overestimates the true cost for output-heavy grabs, those grabs end up
        with LOWER s_g — so the slope estimator returns a negative δ̂ (output
        overweighted) and the nudge reduces the output weight.  Conversely if
        the assumed weight is 0.5x the true (underweighted), output-heavy grabs
        have higher s_g and the nudge increases the weight.

        Here we set w_out = 2 * TRUE and verify the nudge moves it DOWN.
        The true session_factor is uniform (no contamination), so variation in
        s_g is caused solely by the misweight.  Tolerance: ±50% of the
        expected step size (other collinear weights absorb some signal)."""
        import copy
        true_out = widget_updater.MODEL_WEIGHTS["opus"]["output"]   # e.g. 7.5
        W_high = copy.deepcopy(widget_updater.MODEL_WEIGHTS)
        W_high["opus"]["output"] = true_out * 2.0  # 2x overweight

        # Build grabs with varying output/cw1h composition but a FIXED true
        # session_factor.  Because W_high overestimates output cost, grabs that
        # are output-heavy will appear to have LOWER s_g (c_g inflated), so
        # cov(s_norm, frac_output) is negative → δ̂ < 0 → weight goes DOWN.
        true_sf = 1e-5   # arbitrary; kept identical across grabs
        grabs = []
        compositions = [
            (80000, 20000),   # output-heavy
            (20000, 80000),   # cw1h-heavy
            (70000, 30000),   # output-heavy
            (30000, 70000),   # cw1h-heavy
            (60000, 40000),   # moderate
        ]
        for out_tok, cw1h_tok in compositions:
            # True cost using true weights; pct derived from true_sf * true_cost.
            true_w = widget_updater.MODEL_WEIGHTS["opus"]
            true_cost = (50000 * true_w["input"] + out_tok * true_w["output"]
                         + cw1h_tok * true_w["cache_write_1h"])
            pct = true_sf * true_cost
            grabs.append({
                "pct": pct,
                "by_model": {"claude-opus-4-8": {
                    "input": 50000, "output": out_tok,
                    "cache_write_1h": cw1h_tok,
                    "cache_write_5m": 0, "cache_read": 0,
                }},
            })

        result = widget_updater._compute_weight_nudge(grabs, W_high)
        assert result is not None, "nudge returned None — not enough signal"
        old_out = W_high["opus"]["output"]
        new_out = result["opus"]["output"]
        # Nudge must move the output weight DOWN (toward the true value).
        assert new_out < old_out, (
            f"expected output weight to decrease from {old_out}, got {new_out}"
        )
        # The step should be approximately ALPHA * min(|δ̂|, CLAMP) * w_old
        # — within a factor of 2 (other weights absorb some collinear signal).
        alpha = widget_updater.WEIGHT_NUDGE_ALPHA
        clamp = widget_updater.WEIGHT_NUDGE_SLOPE_CLAMP
        # δ̂ is clamped at CLAMP=0.5 if the regression slope exceeds it.
        expected_min_step = alpha * min(0.1, clamp) * old_out * 0.5  # very loose lower bound
        actual_step = old_out - new_out
        assert actual_step > expected_min_step, (
            f"step {actual_step:.4f} too small vs expected minimum {expected_min_step:.4f}"
        )

    def test_nudge_ignores_unknown_grab_keys(self):
        """n_messages and other future keys in grabs must not break the nudge."""
        grabs = [
            {"pct": 20, "n_messages": 10, "by_model": {"claude-opus-4-8": {
                "input": 30000, "output": 80000, "cache_write_1h": 30000,
                "cache_write_5m": 0, "cache_read": 0}}},
            {"pct": 8,  "n_messages": 4,  "by_model": {"claude-opus-4-8": {
                "input": 30000, "output": 5000, "cache_write_1h": 200000,
                "cache_write_5m": 0, "cache_read": 0}}},
            {"pct": 18, "n_messages": 8,  "by_model": {"claude-opus-4-8": {
                "input": 30000, "output": 75000, "cache_write_1h": 35000,
                "cache_write_5m": 0, "cache_read": 0}}},
        ]
        # Should not raise; result may be None or a valid weights dict.
        result = widget_updater._compute_weight_nudge(grabs, widget_updater.MODEL_WEIGHTS)
        if result is not None:
            for mc, wv in result.items():
                for k, v in wv.items():
                    assert v >= 0   # cache_read is legitimately 0.0


# ---------------------------------------------------------------------------
# Learned weights — persistence round-trip
# ---------------------------------------------------------------------------

class TestLearnedWeights:
    def test_load_save_roundtrip(self, tmp_path, monkeypatch):
        lw_file = tmp_path / "learned_weights.json"
        monkeypatch.setattr(widget_updater, "LEARNED_WEIGHTS_FILE", lw_file)
        monkeypatch.setattr(widget_updater, "_learned_weights", None)

        weights = {
            "opus":   {"input": 1.5, "output": 7.8, "cache_write_1h": 1.0,
                       "cache_write_5m": 0.625, "cache_read": 0.0},
            "sonnet": {"input": 1.5, "output": 6.5, "cache_write_1h": 1.0,
                       "cache_write_5m": 0.625, "cache_read": 0.0},
            "haiku":  {"input": 0.75, "output": 9.0, "cache_write_1h": 0.5,
                       "cache_write_5m": 0.31,  "cache_read": 0.0},
        }
        widget_updater._save_learned_weights(weights)
        assert lw_file.exists()

        # Reset and reload.
        monkeypatch.setattr(widget_updater, "_learned_weights", None)
        widget_updater._load_learned_weights()
        assert widget_updater._learned_weights == weights

    def test_load_missing_file_leaves_none(self, tmp_path, monkeypatch):
        monkeypatch.setattr(widget_updater, "LEARNED_WEIGHTS_FILE",
                            tmp_path / "nope.json")
        monkeypatch.setattr(widget_updater, "_learned_weights", None)
        widget_updater._load_learned_weights()
        assert widget_updater._learned_weights is None

    def test_load_malformed_file_leaves_none(self, tmp_path, monkeypatch):
        lw_file = tmp_path / "bad.json"
        lw_file.write_text('{"opus": "not-a-dict"}', encoding="utf-8")
        monkeypatch.setattr(widget_updater, "LEARNED_WEIGHTS_FILE", lw_file)
        monkeypatch.setattr(widget_updater, "_learned_weights", None)
        widget_updater._load_learned_weights()
        assert widget_updater._learned_weights is None

    def test_effective_weights_returns_learned_when_loaded(self, monkeypatch):
        learned = {"opus": {"output": 99.0}}
        monkeypatch.setattr(widget_updater, "_learned_weights", learned)
        assert widget_updater._effective_weights() is learned

    def test_effective_weights_falls_back_to_model_weights(self, monkeypatch):
        monkeypatch.setattr(widget_updater, "_learned_weights", None)
        assert widget_updater._effective_weights() is widget_updater.MODEL_WEIGHTS


class TestCompactionCharge:
    """Auto/manual compaction fires a summarization request that never appears
    as an assistant/usage entry, but the meter charges ~1.0x preTokens at the
    model's cw1h weight (measured 2026-06-09). process_file must charge it
    synthetically as cache_write_1h on the file's current model."""

    def _window(self):
        now = datetime.now(timezone.utc)
        return now - timedelta(hours=1), now + timedelta(hours=4), now

    def _assistant(self, msg_id, ts, model="claude-haiku-4-5-20251001"):
        return json.dumps({"type": "assistant", "timestamp": ts.isoformat(),
                           "message": {"id": msg_id, "model": model,
                                       "usage": {"input_tokens": 10,
                                                 "output_tokens": 5}}})

    def _compact(self, uuid, ts, pre, trigger="auto"):
        return json.dumps({"type": "system", "subtype": "compact_boundary",
                           "content": "Conversation compacted", "uuid": uuid,
                           "timestamp": ts.isoformat(),
                           "compactMetadata": {"trigger": trigger,
                                               "preTokens": pre}})

    def test_compaction_charged_to_file_model(self, tmp_path):
        start, end, now = self._window()
        state = widget_updater._empty_state(start)
        f = tmp_path / "t.jsonl"
        f.write_text(self._assistant("m1", now) + "\n"
                     + self._compact("u1", now, 200_317) + "\n",
                     encoding="utf-8")
        assert widget_updater.process_file(f, state, start, end) is True
        bm = state["by_model"]["claude-haiku-4-5-20251001"]
        assert bm["cache_write_1h"] == 200_317
        assert state["cache_write_1h"] == 200_317
        # input/output from the assistant entry still accumulate normally
        assert bm["input"] == 10 and bm["output"] == 5

    def test_compaction_deduped_on_rescan(self, tmp_path):
        start, end, now = self._window()
        state = widget_updater._empty_state(start)
        f = tmp_path / "t.jsonl"
        f.write_text(self._assistant("m1", now) + "\n"
                     + self._compact("u1", now, 50_000) + "\n",
                     encoding="utf-8")
        widget_updater.process_file(f, state, start, end)
        state["offsets"] = {}          # force a full re-read (seen_ids dedup)
        widget_updater.process_file(f, state, start, end)
        assert state["cache_write_1h"] == 50_000

    def test_compaction_without_model_context_goes_to_unknown(self, tmp_path):
        # Boundary as the first parsed line of a file (e.g. watcher started
        # mid-conversation): no assistant entry seen, no model to attribute.
        start, end, now = self._window()
        state = widget_updater._empty_state(start)
        f = tmp_path / "t.jsonl"
        f.write_text(self._compact("u1", now, 80_000) + "\n", encoding="utf-8")
        assert widget_updater.process_file(f, state, start, end) is True
        assert state["by_model"]["unknown"]["cache_write_1h"] == 80_000

    def test_compaction_outside_window_or_empty_ignored(self, tmp_path):
        start, end, now = self._window()
        state = widget_updater._empty_state(start)
        f = tmp_path / "t.jsonl"
        stale = now - timedelta(hours=3)
        f.write_text(self._compact("u1", stale, 90_000) + "\n"
                     + self._compact("u2", now, 0) + "\n",
                     encoding="utf-8")
        assert widget_updater.process_file(f, state, start, end) is False
        assert state["cache_write_1h"] == 0

    def test_manual_compact_also_charged(self, tmp_path):
        start, end, now = self._window()
        state = widget_updater._empty_state(start)
        f = tmp_path / "t.jsonl"
        f.write_text(self._assistant("m1", now, model="claude-opus-4-8") + "\n"
                     + self._compact("u1", now, 120_000, trigger="manual") + "\n",
                     encoding="utf-8")
        widget_updater.process_file(f, state, start, end)
        assert state["by_model"]["claude-opus-4-8"]["cache_write_1h"] == 120_000
