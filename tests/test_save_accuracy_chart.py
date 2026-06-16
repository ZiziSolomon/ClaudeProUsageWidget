"""
Unit tests for save_accuracy_chart.horizontal_grid_ticks.

Pure function - no matplotlib rendering, no file I/O, no calibration data
required. Covers the gridline-interval logic backing the --hgrid CLI option (a horizontal
line every N percent on the accuracy chart's % axis).
"""

import os
import sys

# matplotlib is imported at module level by save_accuracy_chart, but importing
# it has no side effects (main() is guarded), so a plain import is safe.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from datetime import datetime, timedelta

from save_accuracy_chart import (horizontal_grid_ticks, trigger_style,
                                  estimate_at, DEFAULT_HGRID, DEFAULT_VGRID)


class TestHorizontalGridTicks:
    """horizontal_grid_ticks(ymax, step_pct): a tick every step_pct percent from
    0 to ymax inclusive. step_pct is an INTERVAL, not a count."""

    def test_every_25_pct(self):
        assert horizontal_grid_ticks(100, 25) == [0, 25, 50, 75, 100]

    def test_every_1_pct_gives_a_line_per_percent(self):
        # The headline of the interval semantics: 1 => 100 lines (0..100).
        ticks = horizontal_grid_ticks(100, 1)
        assert ticks[:3] == [0, 1, 2]
        assert ticks[-1] == 100
        assert len(ticks) == 101

    def test_zero_step_disables_gridlines(self):
        assert horizontal_grid_ticks(100, 0) == []

    def test_negative_step_disables_gridlines(self):
        assert horizontal_grid_ticks(100, -1) == []

    def test_zero_ymax_returns_empty(self):
        assert horizontal_grid_ticks(0, 25) == []

    def test_ticks_always_start_at_zero(self):
        assert horizontal_grid_ticks(87, 20)[0] == 0

    def test_final_tick_clamped_to_ymax(self):
        # ymax=87, step=20 -> 0,20,40,60,80, then ymax 87 (not 100).
        assert horizontal_grid_ticks(87, 20) == [0, 20, 40, 60, 80, 87]

    def test_step_larger_than_range_gives_endpoints(self):
        # A step bigger than the whole range -> just 0 and ymax.
        assert horizontal_grid_ticks(40, 25) == [0, 25, 40]

    def test_defaults_are_sane(self):
        # Defaults are intervals now (percent / minutes); both must be positive
        # (0 would silently disable an axis).
        assert DEFAULT_HGRID > 0
        assert DEFAULT_VGRID > 0


class TestTriggerStyle:
    """trigger_style(): maps a calibration trigger to (legend label, colour)."""

    def test_fixed_point_triggers_collapse_to_one_label(self):
        # 5/10/95% shots all read as "passed fixed point" (same label+colour),
        # so the legend shows one entry for them.
        labels = {trigger_style(t)[0]
                  for t in ("liveness_5pct", "liveness_10pct", "liveness_95pct")}
        assert labels == {"passed fixed point"}

    def test_named_reasons_distinct(self):
        assert trigger_style("liveness")[0] == "20m since last call"
        assert trigger_style("liveness_10ppdelta")[0] == "10pp since last call"

    def test_unknown_and_missing_use_fallback(self):
        # Old records may have no trigger field, or an unrecognised value.
        assert trigger_style(None)[0] == "other"
        assert trigger_style("brand_new_trigger")[0] == "other"

    def test_returns_hex_colour(self):
        label, colour = trigger_style("liveness")
        assert colour.startswith("#") and len(colour) == 7


class TestEstimateAt:
    """estimate_at(): linearly interpolate the local-estimate value at a time,
    backing the --jump-segments option (the segment jumps FROM this value)."""

    def _series(self):
        t0 = datetime(2026, 6, 16, 9, 0, 0)
        # 10% at 9:00, 20% at 9:10 -> 1%/min.
        return [{"ts": t0, "pct": 10.0},
                {"ts": t0 + timedelta(minutes=10), "pct": 20.0}]

    def test_interpolates_midpoint(self):
        pts = self._series()
        mid = pts[0]["ts"] + timedelta(minutes=5)
        assert estimate_at(pts, mid) == 15.0

    def test_exact_endpoints(self):
        pts = self._series()
        assert estimate_at(pts, pts[0]["ts"]) == 10.0
        assert estimate_at(pts, pts[-1]["ts"]) == 20.0

    def test_outside_series_returns_none(self):
        pts = self._series()
        assert estimate_at(pts, pts[0]["ts"] - timedelta(minutes=1)) is None
        assert estimate_at(pts, pts[-1]["ts"] + timedelta(minutes=1)) is None

    def test_empty_series_returns_none(self):
        assert estimate_at([], datetime(2026, 6, 16, 9, 0, 0)) is None
