"""Tests for classify_mode — shared thickness-mode detection."""
import pytest
from models import classify_mode

class TestClassifyMode:
    def test_linear_equal_spacing(self):
        assert classify_mode([0.20, 0.28, 0.36, 0.44]) == "linear"

    def test_manual_unequal_spacing(self):
        assert classify_mode([0.00, 0.10, 0.25, 0.50]) == "manual"

    def test_single_value(self):
        assert classify_mode([0.5]) == "linear"

    def test_empty(self):
        assert classify_mode([]) == "linear"

    def test_two_values_always_linear(self):
        assert classify_mode([0.1, 0.5]) == "linear"

    def test_near_equal_spacing_is_linear(self):
        assert classify_mode([0.20, 0.28, 0.36]) == "linear"

    def test_long_list_all_linear(self):
        # 22 equally-spaced values: all diffs equal → "linear"
        values = [round(0.08 * i, 6) for i in range(22)]
        assert classify_mode(values) == "linear"

    def test_long_list_one_irregular_step(self):
        # 22 values, mostly equal spacing but one irregular step → "manual"
        values = [round(0.08 * i, 6) for i in range(22)]
        values[11] = values[10] + 0.09  # inject an irregular gap
        # Recalculate subsequent values to keep the rest equally spaced
        for j in range(12, 22):
            values[j] = values[11] + 0.08 * (j - 11)
        assert classify_mode(values) == "manual"

    def test_floating_point_precision_just_below_tolerance(self):
        # diffs that round to 0.0800 at 4dp should all be equal → "linear"
        # 0.079999999 rounds to 0.0800 at 4dp
        values = [0.0, 0.079999999, 0.159999998, 0.239999997]
        assert classify_mode(values) == "linear"

    def test_floating_point_precision_just_above_tolerance(self):
        # A diff that rounds to a different value at 4dp → "manual".
        # 0.2401 - 0.16 = 0.0801 (at 6dp), which rounds to 0.0801 at 4dp,
        # distinct from the first two diffs which round to 0.0800.
        values = [0.0, 0.08, 0.16, 0.2401]
        assert classify_mode(values) == "manual"

    def test_all_identical_values(self):
        # All diffs are 0.0 — set has one element → "linear"
        assert classify_mode([0.5, 0.5, 0.5]) == "linear"
        assert classify_mode([0.5, 0.5, 0.5, 0.5, 0.5]) == "linear"
