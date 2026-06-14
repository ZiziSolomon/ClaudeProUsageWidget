"""
Unit tests for save_accuracy_chart.horizontal_grid_ticks.

Pure function - no matplotlib rendering, no file I/O, no calibration data
required. Covers the gridline-count logic backing the --hgrid CLI option
(configurable horizontal gridlines on the accuracy chart's % axis).
"""

import os
import sys

# matplotlib is imported at module level by save_accuracy_chart, but importing
# it has no side effects (main() is guarded), so a plain import is safe.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from save_accuracy_chart import horizontal_grid_ticks, DEFAULT_HGRID, DEFAULT_VGRID


class TestHorizontalGridTicks:
    def test_default_count_splits_evenly(self):
        assert horizontal_grid_ticks(100, 4) == [0, 25, 50, 75, 100]

    def test_zero_count_disables_gridlines(self):
        assert horizontal_grid_ticks(100, 0) == []

    def test_negative_count_disables_gridlines(self):
        assert horizontal_grid_ticks(100, -1) == []

    def test_zero_ymax_returns_empty(self):
        assert horizontal_grid_ticks(0, 4) == []

    def test_single_gridline_spans_full_range(self):
        assert horizontal_grid_ticks(100, 1) == [0, 100]

    def test_non_round_ymax_rounds_each_tick(self):
        # ymax=63, count=4 -> step=15.75 -> rounded ticks
        assert horizontal_grid_ticks(63, 4) == [0, 16, 32, 47, 63]

    def test_ticks_always_start_at_zero(self):
        ticks = horizontal_grid_ticks(87, 3)
        assert ticks[0] == 0

    def test_ticks_always_end_at_ymax(self):
        ymax = 87
        ticks = horizontal_grid_ticks(ymax, 3)
        assert ticks[-1] == round(ymax)

    def test_tick_count_matches_request(self):
        for count in (1, 2, 4, 8):
            assert len(horizontal_grid_ticks(100, count)) == count + 1

    def test_defaults_are_sane(self):
        # Sanity check the module-level defaults haven't drifted to something
        # degenerate (0 or negative would silently disable gridlines).
        assert DEFAULT_HGRID > 0
        assert DEFAULT_VGRID > 0
