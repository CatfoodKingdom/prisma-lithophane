"""Shared Wing C tranche — circular hue interpolation utility.

Wing C C2 (`c2_soft_gamut_compress`) needs to query the hue-binned chroma
envelope at arbitrary OKLab hue angles. The bin centers in
`PaletteMetadata.hue_degrees` are sparse (>= 12 bins per § B.6 sanity), so
operators MUST interpolate — and the interpolation MUST wrap continuously
across the 360° → 0° seam, otherwise hues near red would see a synthetic
"valley" or "peak" depending on bin layout.

These tests assert the interpolation contract — not C2's mapping algorithm.
"""
from __future__ import annotations

import numpy as np
import pytest

from preprocessing.hue_interp import interpolate_circular_degrees


# ── Sample/query exactness ──────────────────────────────────────────────────

class TestExactSampleQueries:
    def test_query_at_sample_center_returns_sample_value(self):
        sample_hues = np.array([0.0, 90.0, 180.0, 270.0], dtype=np.float32)
        sample_vals = np.array([1.0, 2.0, 3.0, 4.0], dtype=np.float32)
        for h, v in zip(sample_hues, sample_vals):
            got = interpolate_circular_degrees(
                np.array([h], dtype=np.float32),
                sample_hues,
                sample_vals,
            )
            assert got.shape == (1,)
            np.testing.assert_allclose(got, [v], atol=1e-5)

    def test_query_returns_float32(self):
        sample_hues = np.array([0.0, 180.0], dtype=np.float32)
        sample_vals = np.array([0.5, 1.5], dtype=np.float32)
        got = interpolate_circular_degrees(
            np.array([45.0], dtype=np.float32),
            sample_hues,
            sample_vals,
        )
        assert got.dtype == np.float32


# ── Linear interpolation BETWEEN samples ────────────────────────────────────

class TestLinearBetweenSamples:
    def test_midpoint_between_adjacent_bins_is_average(self):
        # 4 bins evenly spaced: 0, 90, 180, 270
        sample_hues = np.array([0.0, 90.0, 180.0, 270.0], dtype=np.float32)
        sample_vals = np.array([1.0, 3.0, 5.0, 7.0], dtype=np.float32)
        got = interpolate_circular_degrees(
            np.array([45.0], dtype=np.float32),  # midpoint 0..90
            sample_hues,
            sample_vals,
        )
        np.testing.assert_allclose(got, [2.0], atol=1e-5)

    def test_quarter_point_between_bins_is_quarter_blend(self):
        sample_hues = np.array([0.0, 100.0], dtype=np.float32)
        sample_vals = np.array([0.0, 1.0], dtype=np.float32)
        got = interpolate_circular_degrees(
            np.array([25.0], dtype=np.float32),
            sample_hues,
            sample_vals,
        )
        # 25/100 of the way from 0 to 1
        np.testing.assert_allclose(got, [0.25], atol=1e-5)


# ── Circular wrap across 360° → 0° seam ─────────────────────────────────────

class TestCircularWrap:
    def test_wrap_seam_is_continuous(self):
        """Two bins at 350° and 10° (20° arc through 0°). Querying at 0°
        should fall halfway between them — NOT loop back through the
        long arc (340° away)."""
        sample_hues = np.array([10.0, 350.0], dtype=np.float32)
        sample_vals = np.array([2.0, 4.0], dtype=np.float32)
        got = interpolate_circular_degrees(
            np.array([0.0], dtype=np.float32),
            sample_hues,
            sample_vals,
        )
        # 0° is 10° away from 350° (going clockwise) and 10° away from
        # 10° (going counterclockwise) — exact midpoint.
        np.testing.assert_allclose(got, [3.0], atol=1e-5)

    def test_query_just_below_360_picks_nearest_seam_neighbors(self):
        """Bins at 0°, 90°, 180°, 270°. Querying at 359° should see bin
        at 0° as the close neighbor (1° away through wrap), not bin at
        270° (89° away)."""
        sample_hues = np.array([0.0, 90.0, 180.0, 270.0], dtype=np.float32)
        sample_vals = np.array([10.0, 20.0, 30.0, 40.0], dtype=np.float32)
        got = interpolate_circular_degrees(
            np.array([359.0], dtype=np.float32),
            sample_hues,
            sample_vals,
        )
        # 359° lies on the 270°→0° arc (90° apart). 359 is 89/90 of the
        # way through that arc, so value ≈ 40 + 89/90 * (10 - 40) = 10.333
        np.testing.assert_allclose(got, [10.333333], atol=1e-3)

    def test_negative_hue_input_normalizes_correctly(self):
        sample_hues = np.array([0.0, 180.0], dtype=np.float32)
        sample_vals = np.array([1.0, 5.0], dtype=np.float32)
        # -10° is equivalent to 350°
        got_neg = interpolate_circular_degrees(
            np.array([-10.0], dtype=np.float32),
            sample_hues,
            sample_vals,
        )
        got_pos = interpolate_circular_degrees(
            np.array([350.0], dtype=np.float32),
            sample_hues,
            sample_vals,
        )
        np.testing.assert_allclose(got_neg, got_pos, atol=1e-5)

    def test_hue_above_360_normalizes_correctly(self):
        sample_hues = np.array([0.0, 180.0], dtype=np.float32)
        sample_vals = np.array([1.0, 5.0], dtype=np.float32)
        got_big = interpolate_circular_degrees(
            np.array([370.0], dtype=np.float32),  # == 10°
            sample_hues,
            sample_vals,
        )
        got_small = interpolate_circular_degrees(
            np.array([10.0], dtype=np.float32),
            sample_hues,
            sample_vals,
        )
        np.testing.assert_allclose(got_big, got_small, atol=1e-5)


# ── Array shape preservation ────────────────────────────────────────────────

class TestArrayShape:
    def test_1d_query_preserves_shape(self):
        sample_hues = np.linspace(0, 330, 12, dtype=np.float32)
        sample_vals = np.arange(12, dtype=np.float32)
        query = np.array([5.0, 95.0, 185.0, 275.0], dtype=np.float32)
        got = interpolate_circular_degrees(query, sample_hues, sample_vals)
        assert got.shape == (4,)

    def test_2d_query_preserves_shape(self):
        """C2 evaluates the chroma envelope per pixel — the helper must
        accept H×W hue arrays and return H×W result."""
        sample_hues = np.linspace(0, 330, 12, dtype=np.float32)
        sample_vals = np.linspace(0, 1, 12, dtype=np.float32)
        query = np.linspace(0, 360, 16, dtype=np.float32).reshape(4, 4)
        got = interpolate_circular_degrees(query, sample_hues, sample_vals)
        assert got.shape == (4, 4)
        assert got.dtype == np.float32

    def test_unsorted_samples_are_handled(self):
        """The resolver might emit hue_degrees in arbitrary order
        (depends on which palette extreme produced each bin). The
        helper must sort internally rather than requiring callers
        to pre-sort.

        Sorted by hue: [0°→1, 90°→2, 180°→3, 270°→4]. Midpoint of
        the 0°/90° bins is (1+2)/2 = 1.5.
        """
        sample_hues = np.array([180.0, 0.0, 90.0, 270.0], dtype=np.float32)
        sample_vals = np.array([3.0, 1.0, 2.0, 4.0], dtype=np.float32)
        got = interpolate_circular_degrees(
            np.array([45.0], dtype=np.float32),
            sample_hues,
            sample_vals,
        )
        np.testing.assert_allclose(got, [1.5], atol=1e-5)


# ── Degenerate input handling ───────────────────────────────────────────────

class TestDegenerateInputs:
    def test_single_sample_returns_constant(self):
        """A palette that produces one chroma bin (degenerate-chroma per
        § B.5) should yield that constant value at any query angle."""
        sample_hues = np.array([42.0], dtype=np.float32)
        sample_vals = np.array([0.123], dtype=np.float32)
        got = interpolate_circular_degrees(
            np.array([0.0, 100.0, 200.0], dtype=np.float32),
            sample_hues,
            sample_vals,
        )
        np.testing.assert_allclose(got, [0.123, 0.123, 0.123], atol=1e-5)

    def test_mismatched_sample_lengths_raises(self):
        with pytest.raises((ValueError, AssertionError)):
            interpolate_circular_degrees(
                np.array([10.0], dtype=np.float32),
                np.array([0.0, 90.0], dtype=np.float32),
                np.array([1.0], dtype=np.float32),
            )
