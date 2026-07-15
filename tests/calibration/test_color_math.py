"""Tests for unified_calibration.pipeline.color_math."""
from __future__ import annotations

import math
import pytest

from fitting.color_math import (
    interp_single,
    interpolate_pchip,
    linear_to_srgb,
    linear_to_hex,
    srgb_to_linear,
    linear_to_lab,
    compute_dE_from_linear,
)


# ---------------------------------------------------------------------------
# interp_single
# ---------------------------------------------------------------------------

class TestInterpSingle:
    def test_empty_inputs_returns_zero(self):
        assert interp_single([], [], 0.5) == 0.0

    def test_below_first_knot_returns_first_value(self):
        knots = [1.0, 2.0, 3.0]
        values = [0.9, 0.5, 0.2]
        assert interp_single(knots, values, 0.0) == 0.9

    def test_at_first_knot_returns_first_value(self):
        knots = [1.0, 2.0, 3.0]
        values = [0.9, 0.5, 0.2]
        assert interp_single(knots, values, 1.0) == 0.9

    def test_at_last_knot_returns_last_value(self):
        knots = [1.0, 2.0, 3.0]
        values = [0.9, 0.5, 0.2]
        # At exactly the last knot, falls into the d >= knots[-1] branch
        result = interp_single(knots, values, 3.0)
        # log-linear extrapolation from 3.0 with step 0.0 → same value
        assert abs(result - 0.2) < 1e-9

    def test_midpoint_interpolation(self):
        knots = [0.0, 2.0]
        values = [0.0, 1.0]
        assert abs(interp_single(knots, values, 1.0) - 0.5) < 1e-9

    def test_interpolation_between_knots(self):
        knots = [0.0, 1.0, 2.0]
        values = [1.0, 0.5, 0.25]
        # Between knots[1]=1.0 and knots[2]=2.0, d=1.5 → t=0.5 → 0.5 + 0.5*(0.25-0.5)=0.375
        assert abs(interp_single(knots, values, 1.5) - 0.375) < 1e-9

    def test_extrapolation_beyond_last_knot_decreases(self):
        # Exponentially decaying values: result beyond last knot should continue decay
        knots = [1.0, 2.0]
        values = [0.8, 0.4]  # halving → log_slope = ln(0.4/0.8)/1 = ln(0.5)
        result = interp_single(knots, values, 3.0)  # one unit beyond last knot
        expected = 0.4 * math.exp(math.log(0.5) * 1.0)  # = 0.2
        assert abs(result - expected) < 1e-9

    def test_extrapolation_floor_at_0001(self):
        # Very steep decay should floor at 0.001
        knots = [1.0, 2.0]
        values = [0.5, 0.001]
        result = interp_single(knots, values, 10.0)
        assert result >= 0.001

    def test_single_knot_returns_its_value(self):
        assert interp_single([1.0], [0.7], 5.0) == 0.7
        assert interp_single([1.0], [0.7], 0.0) == 0.7

    def test_single_knot_at_exact_position(self):
        # Length-1 list: d == knots[0] should return values[0]
        assert interp_single([2.5], [0.42], 2.5) == 0.42

    def test_equal_consecutive_knots_no_division_by_zero(self):
        # knots[i] == knots[i+1] — division-by-zero guard: t must default to 0
        knots = [1.0, 1.0, 2.0]
        values = [0.9, 0.5, 0.2]
        # d=1.0 hits the d<=knots[0] branch → first value
        assert interp_single(knots, values, 1.0) == 0.9
        # d between the duplicate knot and the next real knot should not raise
        result = interp_single(knots, values, 1.5)
        assert math.isfinite(result)


# ---------------------------------------------------------------------------
# interpolate_pchip
# ---------------------------------------------------------------------------

class TestInterpolatePchip:
    def test_returns_list_of_same_length(self):
        knots = [0.0, 1.0, 2.0]
        values = [1.0, 0.6, 0.3]
        d_dense = [0.0, 0.5, 1.0, 1.5, 2.0]
        result = interpolate_pchip(knots, values, d_dense)
        assert len(result) == len(d_dense)

    def test_at_knot_points_matches_values(self):
        knots = [0.0, 1.0, 2.0]
        values = [1.0, 0.6, 0.3]
        result = interpolate_pchip(knots, values, [0.0, 1.0, 2.0])
        for r, v in zip(result, values):
            assert abs(r - v) < 1e-4

    def test_extrapolation_fills_with_interp_single(self):
        # Values beyond the knot range should be filled (not NaN)
        knots = [1.0, 2.0]
        values = [0.8, 0.4]
        result = interpolate_pchip(knots, values, [0.0, 3.0])
        assert all(math.isfinite(v) for v in result)

    def test_empty_d_dense_returns_empty_list(self):
        result = interpolate_pchip([0.0, 1.0], [1.0, 0.5], [])
        assert result == []

    def test_empty_knots_falls_back_to_linear(self):
        # Empty knots: falls back to interp_single which returns 0.0
        result = interpolate_pchip([], [], [0.5])
        assert result == [0.0]

    def test_single_knot_falls_back_to_linear(self):
        # Single knot: PCHIP needs 2+, so falls back to interp_single
        result = interpolate_pchip([1.0], [0.7], [0.5, 1.0, 2.0])
        assert len(result) == 3
        assert result[0] == 0.7  # below single knot → clamp to value
        assert result[1] == 0.7  # at single knot
        assert result[2] == 0.7  # above single knot → clamp to value


# ---------------------------------------------------------------------------
# linear_to_srgb
# ---------------------------------------------------------------------------

class TestLinearToSrgb:
    def test_zero_maps_to_zero(self):
        assert linear_to_srgb(0.0) == 0

    def test_one_maps_to_255(self):
        assert linear_to_srgb(1.0) == 255

    def test_clamping_below_zero(self):
        assert linear_to_srgb(-0.5) == 0

    def test_clamping_above_one(self):
        assert linear_to_srgb(1.5) == 255

    def test_linear_region_near_zero(self):
        # c=0.003 is in the linear region (≤0.0031308)
        c = 0.003
        expected = max(0, min(255, int(round(12.92 * c * 255))))
        assert linear_to_srgb(c) == expected

    def test_gamma_region(self):
        # c=0.5 is in the gamma region
        c = 0.5
        s = 1.055 * (c ** (1.0 / 2.4)) - 0.055
        expected = max(0, min(255, int(round(s * 255))))
        assert linear_to_srgb(c) == expected

    def test_midpoint_reasonable_value(self):
        # Linear 0.5 should encode to ~188 in sRGB (gamma-compressed)
        result = linear_to_srgb(0.5)
        assert 185 <= result <= 190

    def test_exact_threshold_0031308_uses_linear_formula(self):
        # c == 0.0031308 is the upper boundary of the linear region
        c = 0.0031308
        expected_linear = int(round(12.92 * c * 255))
        assert linear_to_srgb(c) == expected_linear

    def test_just_above_threshold_uses_gamma_formula(self):
        # c slightly above 0.0031308 switches to the gamma branch
        c = 0.003131  # > 0.0031308
        s = 1.055 * (c ** (1.0 / 2.4)) - 0.055
        expected = max(0, min(255, int(round(s * 255))))
        assert linear_to_srgb(c) == expected


# ---------------------------------------------------------------------------
# linear_to_hex
# ---------------------------------------------------------------------------

class TestLinearToHex:
    def test_white(self):
        assert linear_to_hex(1.0, 1.0, 1.0) == "#FFFFFF"

    def test_black(self):
        assert linear_to_hex(0.0, 0.0, 0.0) == "#000000"

    def test_hex_format(self):
        result = linear_to_hex(0.5, 0.25, 0.75)
        assert result.startswith("#")
        assert len(result) == 7
        assert result[1:].upper() == result[1:]  # uppercase hex

    def test_pure_red(self):
        result = linear_to_hex(1.0, 0.0, 0.0)
        assert result == "#FF0000"

    def test_pure_green(self):
        result = linear_to_hex(0.0, 1.0, 0.0)
        assert result == "#00FF00"

    def test_pure_blue(self):
        result = linear_to_hex(0.0, 0.0, 1.0)
        assert result == "#0000FF"


# ---------------------------------------------------------------------------
# srgb_to_linear
# ---------------------------------------------------------------------------

class TestSrgbToLinear:
    def test_zero_maps_to_zero(self):
        assert srgb_to_linear(0) == 0.0

    def test_255_maps_to_one(self):
        assert abs(srgb_to_linear(255) - 1.0) < 1e-6

    def test_linear_region_near_zero(self):
        # sRGB value 10 → s=10/255≈0.0392 which is ≤0.04045 → linear region
        c = 10
        s = c / 255.0
        expected = s / 12.92
        assert abs(srgb_to_linear(c) - expected) < 1e-9

    def test_gamma_region(self):
        # sRGB value 128 → s≈0.502 which is in gamma region
        c = 128
        s = c / 255.0
        expected = ((s + 0.055) / 1.055) ** 2.4
        assert abs(srgb_to_linear(c) - expected) < 1e-9

    def test_exact_threshold_04045_uses_linear_formula(self):
        # s = 0.04045/255 corresponds to the upper boundary of the linear region
        # Find the sRGB byte whose normalized value is exactly 0.04045
        # 0.04045 * 255 ≈ 10.31, so byte 10 → s≈0.0392 (linear), byte 11 → s≈0.0431 (gamma)
        # Test that byte 10 stays in the linear branch
        c_linear = 10
        s = c_linear / 255.0
        assert s <= 0.04045
        expected = s / 12.92
        assert abs(srgb_to_linear(c_linear) - expected) < 1e-9

    def test_just_above_threshold_uses_gamma_formula(self):
        # sRGB byte 11 → s≈0.0431 which is > 0.04045 → gamma branch
        c_gamma = 11
        s = c_gamma / 255.0
        assert s > 0.04045
        expected = ((s + 0.055) / 1.055) ** 2.4
        assert abs(srgb_to_linear(c_gamma) - expected) < 1e-9


# ---------------------------------------------------------------------------
# Roundtrip: srgb_to_linear ↔ linear_to_srgb
# ---------------------------------------------------------------------------

class TestSrgbLinearRoundtrip:
    def test_srgb_roundtrip(self):
        """sRGB → linear → sRGB should recover original value (within rounding)."""
        for val in [0, 1, 10, 64, 128, 192, 254, 255]:
            linear = srgb_to_linear(val)
            recovered = linear_to_srgb(linear)
            assert recovered == val, f"Roundtrip failed for sRGB={val}: got {recovered}"

    def test_linear_roundtrip(self):
        """linear → sRGB → linear should be close (lossy due to 8-bit quantization)."""
        for c in [0.0, 0.1, 0.5, 0.9, 1.0]:
            srgb = linear_to_srgb(c)
            recovered = srgb_to_linear(srgb)
            # 8-bit quantization gives ~0.004 max error
            assert abs(recovered - c) < 0.01, f"Roundtrip failed for linear={c}"


# ---------------------------------------------------------------------------
# linear_to_lab
# ---------------------------------------------------------------------------

class TestLinearToLab:
    def test_white_gives_L_near_100(self):
        L, a, b = linear_to_lab([1.0, 1.0, 1.0])
        assert abs(L - 100.0) < 0.01

    def test_white_gives_near_zero_a_b(self):
        L, a, b = linear_to_lab([1.0, 1.0, 1.0])
        assert abs(a) < 0.01
        assert abs(b) < 0.01

    def test_black_gives_L_near_zero(self):
        L, a, b = linear_to_lab([0.0, 0.0, 0.0])
        assert abs(L) < 0.01

    def test_returns_tuple_of_three(self):
        result = linear_to_lab([0.5, 0.5, 0.5])
        assert len(result) == 3

    def test_gray_has_near_zero_a_b(self):
        # Any neutral gray should have a≈0, b≈0
        for level in [0.1, 0.3, 0.5, 0.8]:
            L, a, b = linear_to_lab([level, level, level])
            assert abs(a) < 0.01, f"Gray {level}: a={a}"
            assert abs(b) < 0.01, f"Gray {level}: b={b}"

    def test_red_has_positive_a(self):
        # Red should push toward +a (red-green axis)
        L, a, b = linear_to_lab([1.0, 0.0, 0.0])
        assert a > 0

    def test_blue_has_negative_b(self):
        # Blue should push toward -b (yellow-blue axis)
        L, a, b = linear_to_lab([0.0, 0.0, 1.0])
        assert b < 0

    def test_clamping_out_of_range_inputs(self):
        # Inputs beyond [0,1] should be clamped, not error
        L1, a1, b1 = linear_to_lab([1.5, -0.5, 0.5])
        L2, a2, b2 = linear_to_lab([1.0, 0.0, 0.5])
        assert abs(L1 - L2) < 0.01 and abs(a1 - a2) < 0.01 and abs(b1 - b2) < 0.01

    def test_pure_red_lab_ranges(self):
        # sRGB pure red: L roughly 53, a strongly positive (~80), b positive (~67)
        L, a, b = linear_to_lab([1.0, 0.0, 0.0])
        assert 45 < L < 60, f"Red L={L} out of expected range"
        assert a > 70, f"Red a={a} not strongly positive"
        assert b > 50, f"Red b={b} not positive"

    def test_pure_green_lab_ranges(self):
        # sRGB pure green: L roughly 87, a strongly negative (~-79), b positive (~80)
        L, a, b = linear_to_lab([0.0, 1.0, 0.0])
        assert 80 < L < 95, f"Green L={L} out of expected range"
        assert a < -60, f"Green a={a} not strongly negative"

    def test_pure_blue_lab_ranges(self):
        # sRGB pure blue: L roughly 32, a positive (~79), b strongly negative (~-107)
        L, a, b = linear_to_lab([0.0, 0.0, 1.0])
        assert 25 < L < 40, f"Blue L={L} out of expected range"
        assert b < -80, f"Blue b={b} not strongly negative"


# ---------------------------------------------------------------------------
# compute_dE_from_linear
# ---------------------------------------------------------------------------

class TestComputeDEFromLinear:
    def test_identical_inputs_give_zero(self):
        assert compute_dE_from_linear([0.5, 0.3, 0.1], [0.5, 0.3, 0.1]) == 0.0

    def test_white_vs_black_gives_large_dE(self):
        dE = compute_dE_from_linear([1.0, 1.0, 1.0], [0.0, 0.0, 0.0])
        assert dE > 90  # L goes from ~100 to ~0

    def test_symmetric(self):
        a = [0.8, 0.2, 0.5]
        b = [0.3, 0.7, 0.1]
        assert abs(compute_dE_from_linear(a, b) - compute_dE_from_linear(b, a)) < 1e-9

    def test_non_negative(self):
        for pair in [
            ([1.0, 0.0, 0.0], [0.0, 1.0, 0.0]),
            ([0.5, 0.5, 0.5], [0.5, 0.5, 0.5]),
            ([0.1, 0.2, 0.3], [0.9, 0.8, 0.7]),
        ]:
            assert compute_dE_from_linear(*pair) >= 0.0

    def test_similar_colors_give_small_dE(self):
        # Small tweak to one channel → small dE
        dE = compute_dE_from_linear([0.5, 0.5, 0.5], [0.51, 0.5, 0.5])
        assert dE < 1.0

    def test_very_different_colors_give_large_dE(self):
        # Pure red vs pure cyan (complementary)
        dE = compute_dE_from_linear([1.0, 0.0, 0.0], [0.0, 1.0, 1.0])
        assert dE > 50

    def test_all_zeros_both_inputs(self):
        # Black vs black should be 0
        dE = compute_dE_from_linear([0.0, 0.0, 0.0], [0.0, 0.0, 0.0])
        assert dE == 0.0

    def test_all_zeros_measured_nonzero_predicted(self):
        # Black vs mid-gray: should give a finite, positive dE
        dE = compute_dE_from_linear([0.0, 0.0, 0.0], [0.5, 0.5, 0.5])
        assert math.isfinite(dE)
        assert dE > 0
