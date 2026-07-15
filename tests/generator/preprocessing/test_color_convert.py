"""Color-conversion contract tests for the preprocessing slot (R2-E + R5-A).

These exercise the four domain pairs the slot runner is allowed to insert
between adjacent operators, plus the canonical srgb_f32 → srgb_u8 quantizer
that the preview hook routes through (R5-A).

The proper sRGB transfer (IEC 61966-2-1 piecewise, not a single-power
approximation) is required because sRGB↔linear round-trips inside the slot
must not bleed shadow precision (the project's T_floor=0.003 noise floor
is very close to the toe of the curve).
"""
from __future__ import annotations

import numpy as np
import pytest

from preprocessing.color_convert import (
    linear_rgb_f32_to_oklab_f32,
    linear_rgb_f32_to_srgb_f32,
    oklab_f32_to_linear_rgb_f32,
    oklab_f32_to_srgb_f32,
    srgb_f32_to_linear_rgb_f32,
    srgb_f32_to_oklab_f32,
    srgb_f32_to_srgb_u8,
    srgb_u8_to_srgb_f32,
)


def _checker_u8(rng: np.random.Generator) -> np.ndarray:
    """Deterministic 16x16x3 uint8 raster covering the full 0–255 range."""
    return rng.integers(0, 256, size=(16, 16, 3), dtype=np.uint8)


class TestSrgbU8AndSrgbF32:
    def test_u8_to_f32_normalizes_to_unit_interval(self):
        x = np.array([[[0, 127, 255]]], dtype=np.uint8)
        y = srgb_u8_to_srgb_f32(x)
        assert y.dtype == np.float32
        assert y.shape == x.shape
        np.testing.assert_allclose(y[0, 0], [0.0, 127.0 / 255.0, 1.0])

    def test_f32_to_u8_quantizes_with_clamp_and_scale(self):
        # R5-A quantization contract: clamp to [0,1], multiply by 255, round.
        x = np.array([[[-0.1, 0.5, 1.5]]], dtype=np.float32)
        y = srgb_f32_to_srgb_u8(x)
        assert y.dtype == np.uint8
        assert y.shape == x.shape
        np.testing.assert_array_equal(y[0, 0], [0, 128, 255])

    def test_round_trip_u8_f32_u8_is_identity(self):
        rng = np.random.default_rng(0)
        x = _checker_u8(rng)
        y = srgb_f32_to_srgb_u8(srgb_u8_to_srgb_f32(x))
        np.testing.assert_array_equal(y, x)


class TestSrgbF32AndLinearRgbF32:
    def test_round_trip_srgb_linear_srgb_is_close_to_identity(self):
        rng = np.random.default_rng(1)
        x = rng.random((8, 8, 3), dtype=np.float32)
        y = linear_rgb_f32_to_srgb_f32(srgb_f32_to_linear_rgb_f32(x))
        # IEC-61966-2-1 piecewise transfer is invertible to ~1e-6.
        np.testing.assert_allclose(y, x, atol=1e-5)

    def test_known_srgb_to_linear_values(self):
        # IEC standard reference: 0 -> 0, 1 -> 1; 0.5 -> ~0.21404.
        x = np.array([[[0.0, 0.5, 1.0]]], dtype=np.float32)
        y = srgb_f32_to_linear_rgb_f32(x)
        assert y.dtype == np.float32
        np.testing.assert_allclose(y[0, 0, 0], 0.0, atol=1e-6)
        np.testing.assert_allclose(y[0, 0, 1], 0.21404114, atol=1e-5)
        np.testing.assert_allclose(y[0, 0, 2], 1.0, atol=1e-6)

    def test_does_not_clamp_linear_above_unity(self):
        # R2-E: no clamp on linear_rgb_f32 (HDR ops may briefly exceed [0,1]).
        x = np.array([[[1.5, -0.2, 0.5]]], dtype=np.float32)
        y = linear_rgb_f32_to_srgb_f32(x)
        # Large linear values can map above 1.0 in sRGB; helper must not clamp.
        assert y[0, 0, 0] > 1.0
        # Negative inputs round-trip through their negative branch (or 0) but
        # are not silently clipped. Operator authors are responsible for sanity.
        assert y.dtype == np.float32


class TestLinearRgbAndOklab:
    def test_round_trip_linear_oklab_linear_is_close_to_identity(self):
        rng = np.random.default_rng(2)
        x = rng.random((8, 8, 3), dtype=np.float32)
        y = oklab_f32_to_linear_rgb_f32(linear_rgb_f32_to_oklab_f32(x))
        np.testing.assert_allclose(y, x, atol=1e-4)

    def test_oklab_dtype_is_f32(self):
        x = np.full((4, 4, 3), 0.5, dtype=np.float32)
        y = linear_rgb_f32_to_oklab_f32(x)
        assert y.dtype == np.float32
        assert y.shape == x.shape

    def test_neutral_gray_has_zero_chroma(self):
        # Linear-gray input has equal R=G=B; OKLab a/b should be ~0.
        x = np.full((4, 4, 3), 0.5, dtype=np.float32)
        y = linear_rgb_f32_to_oklab_f32(x)
        np.testing.assert_allclose(y[..., 1], 0.0, atol=1e-4)
        np.testing.assert_allclose(y[..., 2], 0.0, atol=1e-4)


class TestSrgbAndOklabConvenience:
    def test_srgb_oklab_round_trip(self):
        rng = np.random.default_rng(3)
        x = rng.random((6, 6, 3), dtype=np.float32)
        y = oklab_f32_to_srgb_f32(srgb_f32_to_oklab_f32(x))
        # Two-hop round trip; tolerate accumulated float error.
        np.testing.assert_allclose(y, x, atol=1e-4)
