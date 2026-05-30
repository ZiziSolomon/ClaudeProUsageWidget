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
    # Every _append_calibration call writes to CALIBRATION_FILE. Without the
    # monkeypatch these tests would (and did) pollute the real user log with
    # synthetic 2099 session_start records, confusing tools like
    # save_accuracy_chart.py. Pin to a tmp file in every test.
    @pytest.fixture(autouse=True)
    def _isolate_calibration_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(widget_updater, "CALIBRATION_FILE",
                            tmp_path / "calibration.jsonl")

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
        state["output_tokens"] = 1000  # 2k io total
        pct = widget_updater.CALIBRATION_PCT_FLOOR - 1  # 4%
        widget_updater._append_calibration(state, float(pct),
                                           datetime.now(timezone.utc))
        # No prior history -> blend falls back to X alone:
        # midpoint_pct = (3.5 + 4.5) / 2 = 4.0% => X = 2000 / 0.04 = 50000.
        assert state["implied_session_budget"] == 50000

    def test_at_floor_sets_budget(self):
        # At the floor exactly we DO trust it: 2k io at floor% => 2k/(floor/100).
        state = widget_updater._empty_state(
            datetime(2099, 1, 1, tzinfo=timezone.utc))
        state["input_tokens"] = 1000
        state["output_tokens"] = 1000
        floor = widget_updater.CALIBRATION_PCT_FLOOR
        widget_updater._append_calibration(state, float(floor), datetime.now(timezone.utc))
        assert state["implied_session_budget"] == round(2000 / (floor / 100))


class TestBlendedSubFloorBudget:
    """The sub-floor blend (X from live reading + M from history). Direct
    tests on the pure function so we can pin the math precisely without
    going through the calibration file dance."""

    def _x(self, total_io, pct):
        # Mirror the production midpoint: round-to-nearest at 1% means
        # true_pct in [N-0.5, N+0.5], so midpoint = N (for N>=0.5) or
        # 0.25% (for N=0).
        lower = max(pct - 0.5, 0.0)
        upper = pct + 0.5
        return total_io / (((lower + upper) / 2) / 100)

    def test_no_history_returns_x(self):
        # With no M to blend, we just get X back unchanged.
        total_io, pct = 2000, 4.0
        x = self._x(total_io, pct)
        b = widget_updater._blended_sub_floor_budget(total_io, pct, None)
        assert b == int(round(x))

    def test_weight_at_pct_zero(self):
        # pct=0 => w=0.2 => 0.2*X + 0.8*M, no clamp needed if M close to X.
        total_io, pct = 500, 0.0
        x = self._x(total_io, pct)             # 500 / 0.0025 = 200000
        m = 250000                              # close to X
        expected = 0.2 * x + 0.8 * m
        b = widget_updater._blended_sub_floor_budget(total_io, pct, m)
        assert b == int(round(expected))

    def test_weight_at_pct_four(self):
        # pct=4 (just under the floor) => w=0.8 => 0.8*X + 0.2*M.
        total_io, pct = 8000, 4.0
        x = self._x(total_io, pct)              # 8000 / 0.04 = 200000
        m = 250000
        expected = 0.8 * x + 0.2 * m
        b = widget_updater._blended_sub_floor_budget(total_io, pct, m)
        assert b == int(round(expected))

    def test_lower_clamp_when_m_too_small(self):
        # M < X/2 would drag the blend below the live reading's plausibility
        # floor. Clamp to X/2 - this is the asymmetric guard.
        total_io, pct = 8000, 2.0
        x = self._x(total_io, pct)              # 8000 / 0.02 = 400000
        m = 1                                    # absurdly small
        b = widget_updater._blended_sub_floor_budget(total_io, pct, m)
        assert b == int(round(x * 0.5))

    def test_no_upper_clamp_when_m_huge(self):
        # M >> X is the off-laptop-contamination signature (local total_io
        # undercounts, so X is too small). The blend should let M dominate
        # without a 2X ceiling yanking it back to wrong.
        total_io, pct = 500, 4.0
        x = self._x(total_io, pct)              # 500 / 0.04 = 12500
        m = 10_000_000                          # absurdly large vs X
        expected = 0.8 * x + 0.2 * m            # = 10000 + 2000000 = 2010000
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
        f.write_text("\n".join([
            json.dumps({"implied_session_budget": None,   "budget_source": "live"}),
            json.dumps({"implied_session_budget": 200000, "budget_source": "live"}),
            json.dumps({"implied_session_budget": 300000, "budget_source": "live"}),
        ]) + "\n", encoding="utf-8")
        monkeypatch.setattr(widget_updater, "CALIBRATION_FILE", f)
        assert widget_updater._load_prior_budget_median() == 250000

    def test_window_caps_to_recent(self, tmp_path, monkeypatch):
        # Older absurd value should drop out of the window and not skew the
        # median.
        f = tmp_path / "calibration.jsonl"
        lines = [json.dumps({"implied_session_budget": 999_999_999,
                              "budget_source": "live"})]
        lines += [json.dumps({"implied_session_budget": 200000,
                               "budget_source": "live"})
                  for _ in range(widget_updater.PRIOR_BUDGET_WINDOW)]
        f.write_text("\n".join(lines) + "\n", encoding="utf-8")
        monkeypatch.setattr(widget_updater, "CALIBRATION_FILE", f)
        assert widget_updater._load_prior_budget_median() == 200000

    def test_blended_entries_excluded(self, tmp_path, monkeypatch):
        # "blended" entries are partially derived from the prior itself —
        # including them creates a feedback loop. Only "live" entries count.
        f = tmp_path / "calibration.jsonl"
        lines = [
            json.dumps({"implied_session_budget": 50000,
                        "budget_source": "blended"}),  # should be ignored
            json.dumps({"implied_session_budget": 200000,
                        "budget_source": "live"}),
            json.dumps({"implied_session_budget": 300000,
                        "budget_source": "live"}),
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

    def test_anchor_snaps_to_api_pct(self):
        # With anchor_io == current io, estimate is exactly anchor_pct regardless
        # of what total_io / budget would yield (eliminates rounding drift).
        state = {
            "input_tokens": 30000, "output_tokens": 30000,
            "implied_session_budget": 200000,
            "anchor_pct": 28.5,   # API said 28.5% when io was 60k
            "anchor_io":  60000,  # same as current => delta = 0
        }
        assert widget_updater._estimate_session_pct(state) == 28.5

    def test_anchor_delta_adds_from_anchor(self):
        # Tokens written after the anchor grow estimate from anchor_pct, not zero.
        state = {
            "input_tokens": 30000, "output_tokens": 30000,
            "implied_session_budget": 200000,
            "anchor_pct": 28.5,
            "anchor_io":  60000,
        }
        state["output_tokens"] += 20000  # +20k delta => +10pp
        # 28.5 + 100 * (20000 / 200000) = 28.5 + 10.0 = 38.5
        assert widget_updater._estimate_session_pct(state) == 38.5

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

    def test_sets_anchor(self, monkeypatch):
        # _adopt_api_pct must record anchor_pct + anchor_io so subsequent
        # _local_estimate calls start from the API value, not total_io/budget.
        h = self._make_handler(monkeypatch)
        self._capture(monkeypatch)
        h.session_pct = 50
        h.state["implied_session_budget"] = 200000
        h.state["input_tokens"], h.state["output_tokens"] = 40000, 20000
        h._adopt_api_pct(53, datetime.now(timezone.utc))
        assert h.state["anchor_pct"] == 53
        assert h.state["anchor_io"]  == 60000   # 40k + 20k at time of call

    def test_local_estimate_uses_anchor_immediately(self, monkeypatch):
        # After _adopt_api_pct, _local_estimate returns exactly the API pct when
        # no new tokens have been written (delta = 0).
        h = self._make_handler(monkeypatch)
        self._capture(monkeypatch)
        h.session_pct = 50
        h.state["implied_session_budget"] = 200000
        h.state["input_tokens"], h.state["output_tokens"] = 40000, 20000
        h._adopt_api_pct(53, datetime.now(timezone.utc))
        # No new tokens: delta = 0, so estimate == anchor_pct exactly.
        assert h._local_estimate() == 53


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


# ---------------------------------------------------------------------------
# Incremental process_file - the watcher stalled on a 2.5MB transcript
# because each FS event re-read and re-parsed the whole file. The fix is
# to seek to a stored byte offset and only parse new bytes. These tests
# guard the seek+offset semantics and the multi-MB performance floor.
# ---------------------------------------------------------------------------

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
    @pytest.fixture(autouse=True)
    def _isolate_calibration_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(widget_updater, "CALIBRATION_FILE",
                            tmp_path / "calibration.jsonl")

    def _make_handler(self, monkeypatch):
        monkeypatch.setattr(widget_updater.TranscriptHandler, "_startup",
                            lambda self: None)
        monkeypatch.setattr(widget_updater, "_save_state", lambda *a, **k: None)
        return widget_updater.TranscriptHandler()

    def _seed_anchor(self, h, pct, io):
        """Set a clean anchor directly (bypasses lb computation for setup)."""
        h.state["session_anchors"] = (h.state.get("session_anchors") or []) + [[pct, io]]
        h.state["anchor_pct"] = pct
        h.state["anchor_io"]  = io
        h.state["input_tokens"]  = io
        h.state["output_tokens"] = 0

    def test_no_lb_on_first_anchor(self, monkeypatch):
        # First anchor: session_anchors is empty, no prior to diff against.
        h = self._make_handler(monkeypatch)
        h.state["session_anchors"] = []
        h.state["input_tokens"] = 10000
        h.state["output_tokens"] = 0
        h._set_anchor(5.0)
        assert h.state.get("session_budget_lb", 0) == 0

    def test_lb_computed_on_second_anchor(self, monkeypatch):
        # 10k tokens, Δpct=5 → denom=6 → lb = 100*10000/6 = 166666
        h = self._make_handler(monkeypatch)
        h.state["session_anchors"] = []
        self._seed_anchor(h, 5.0, 5000)          # anchor 1, no lb yet
        h.state["input_tokens"] = 15000           # +10k
        h._set_anchor(10.0)                       # Δpct=5, Δio=10k
        assert h.state["session_budget_lb"] == int(100 * 10000 / 6)

    def test_worst_case_rounding_uses_delta_plus_one(self, monkeypatch):
        # denom must be Δpct+1, not Δpct — the bound must hold even if the
        # true Δpct was Δpct_api + 0.99 pp (floor rounding worst case).
        h = self._make_handler(monkeypatch)
        h.state["session_anchors"] = []
        self._seed_anchor(h, 0.0, 0)
        h.state["input_tokens"] = 20000
        h._set_anchor(2.0)                        # Δpct=2 → denom=3
        assert h.state["session_budget_lb"] == int(100 * 20000 / 3)
        assert h.state["session_budget_lb"] < int(100 * 20000 / 2)  # not naive /2

    def test_zero_delta_pct_gives_lb(self, monkeypatch):
        # Δpct=0, Δio>0: pct didn't tick so true Δpct < 1 pp → denom=1.
        # lb = 100 * Δio / 1 = budget ≥ 100 × tokens_used.
        h = self._make_handler(monkeypatch)
        h.state["session_anchors"] = []
        self._seed_anchor(h, 5.0, 1000)
        h.state["input_tokens"] = 6000            # +5k, pct still 5
        h._set_anchor(5.0)                        # Δpct=0 → denom=1
        assert h.state["session_budget_lb"] == int(100 * 5000 / 1)

    def test_lb_is_running_maximum(self, monkeypatch):
        # lb grows when a new pair is tighter, stays put when it's looser.
        h = self._make_handler(monkeypatch)
        h.state["session_anchors"] = []
        self._seed_anchor(h, 0.0, 0)

        # Anchor 2: 20k tokens, Δpct=2 → pair(1,2): lb = 100*20000/3 ≈ 666k
        h.state["input_tokens"] = 20000
        h._set_anchor(2.0)
        lb1 = h.state["session_budget_lb"]
        assert lb1 == int(100 * 20000 / 3)

        # Anchor 3: contaminated (+2k local, Δpct=5). All pairs involving
        # anchor 3 have inflated Δpct → smaller lb. max() preserves lb1.
        h.state["input_tokens"] = 22000
        h._set_anchor(7.0)
        assert h.state["session_budget_lb"] == lb1

        # Anchor 4: 30k local tokens, Δpct=1.
        # pair(3,4): lb = 100*30000/2 = 1500000 — tighter, wins.
        h.state["input_tokens"] = 52000
        h._set_anchor(8.0)
        assert h.state["session_budget_lb"] == int(100 * 30000 / 2)

    def test_full_history_beats_consecutive(self, monkeypatch):
        # Two clean intervals each with Δpct=1. Consecutive lb = 100*Δio/2.
        # The full span (anchor1→anchor3) has Δpct=2 → lb = 100*(2*Δio)/3,
        # which is larger than 100*Δio/2 — the "+1" amortizes over more pcts.
        h = self._make_handler(monkeypatch)
        h.state["session_anchors"] = []
        self._seed_anchor(h, 0.0, 0)

        h.state["input_tokens"] = 10000
        h._set_anchor(1.0)                        # pair(1,2): lb=100*10k/2=500k
        lb_after_2 = h.state["session_budget_lb"]

        h.state["input_tokens"] = 20000
        h._set_anchor(2.0)
        # pair(1,3): Δpct=2, Δio=20k → lb=100*20k/3=666k  (full span wins)
        # pair(2,3): Δpct=1, Δio=10k → lb=100*10k/2=500k
        assert h.state["session_budget_lb"] == int(100 * 20000 / 3)
        assert h.state["session_budget_lb"] > lb_after_2

    def test_negative_delta_pct_skipped(self, monkeypatch):
        # A pct drop signals a session reset — skip to avoid a nonsensical lb.
        h = self._make_handler(monkeypatch)
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
        state["output_tokens"] = 50000   # 100k at 50% → 200k
        state["session_budget_lb"] = 100000
        widget_updater._append_calibration(state, 50.0, datetime.now(timezone.utc))
        assert state["implied_session_budget"] == 200000

    def test_empty_state_has_zero_lb(self):
        state = widget_updater._empty_state(
            datetime(2099, 1, 1, tzinfo=timezone.utc))
        assert state["session_budget_lb"] == 0

