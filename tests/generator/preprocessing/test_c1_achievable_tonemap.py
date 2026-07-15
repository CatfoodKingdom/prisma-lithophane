from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

from preprocessing.color_convert import (
    oklab_f32_to_srgb_f32,
    srgb_f32_to_oklab_f32,
)
from preprocessing.operators.c1_achievable_tonemap import C1AchievableTonemap
from preprocessing.palette_metadata import PaletteMetadata
from preprocessing.types import PreprocessingContext


_BASE_EXPECTED_METRICS = {
    "achievable_black_L",
    "achievable_white_L",
    "effective_shadow_L",
    "effective_highlight_L",
    "mean_abs_delta_L",
    "pct_shadow_clamped",
    "pct_highlight_clamped",
    "achievable_range_occupancy",
}
def _make_palette_metadata(
    *,
    achievable_black_L: float = 0.20,
    achievable_white_L: float = 0.80,
) -> PaletteMetadata:
    hue_degrees = np.linspace(15.0, 345.0, 12, dtype=np.float32)
    l_bin_centers = np.linspace(
        achievable_black_L,
        achievable_white_L,
        8,
        dtype=np.float32,
    )
    return PaletteMetadata(
        achievable_black_oklab=np.array(
            [achievable_black_L, 0.0, 0.0],
            dtype=np.float32,
        ),
        achievable_white_oklab=np.array(
            [achievable_white_L, 0.0, 0.0],
            dtype=np.float32,
        ),
        max_chroma_by_hue=np.zeros_like(hue_degrees),
        hue_degrees=hue_degrees,
        l_bin_centers=l_bin_centers,
        max_chroma_by_hue_l=np.zeros(
            (hue_degrees.size, l_bin_centers.size),
            dtype=np.float32,
        ),
        request_fingerprint="c1-test-palette",
    )


def _make_context(
    *,
    palette_metadata: PaletteMetadata | None = None,
) -> PreprocessingContext:
    return PreprocessingContext(
        config=SimpleNamespace(),
        image_fingerprint="c1-test-image",
        source_path=None,
        source_image=None,
        palette_metadata=palette_metadata or _make_palette_metadata(),
    )


def _neutral_oklab_image(l_values: np.ndarray, *, height: int = 8) -> np.ndarray:
    l_values = np.asarray(l_values, dtype=np.float32)
    tiled = np.tile(l_values[np.newaxis, :], (height, 1))
    oklab = np.zeros((height, l_values.shape[0], 3), dtype=np.float32)
    oklab[..., 0] = tiled
    return oklab_f32_to_srgb_f32(oklab).astype(np.float32, copy=False)


def _output_L(result) -> np.ndarray:
    return srgb_f32_to_oklab_f32(result.image)[..., 0].astype(np.float32)


def _apply(
    image: np.ndarray,
    *,
    palette_metadata: PaletteMetadata | None = None,
    **kwargs,
):
    op = C1AchievableTonemap(**kwargs)
    return op.apply(
        image.copy(),
        context=_make_context(
            palette_metadata=palette_metadata,
        ),
        progress=None,
    )


def test_neutral_ramp_stays_monotonic_in_output_L():
    image = _neutral_oklab_image(np.linspace(0.02, 0.98, 256, dtype=np.float32))

    result = _apply(image)
    output_L = _output_L(result)[0]

    assert np.all(np.diff(output_L) >= -1e-6)


def test_output_L_stays_within_achievable_interval_under_default_params():
    palette = _make_palette_metadata(
        achievable_black_L=0.20,
        achievable_white_L=0.80,
    )
    image = _neutral_oklab_image(np.linspace(0.01, 0.99, 512, dtype=np.float32))

    result = _apply(image, palette_metadata=palette)
    output_L = _output_L(result)

    assert float(output_L.min()) >= (0.20 - 2e-5)
    assert float(output_L.max()) <= (0.80 + 2e-5)
    assert result.metrics["achievable_range_occupancy"] == pytest.approx(1.0, abs=1e-6)


def test_coincident_anchor_fixture_is_near_identity_at_default_strength():
    black_L = 0.20
    white_L = 0.80
    l_values = np.concatenate(
        [
            np.full(10, black_L, dtype=np.float32),
            np.linspace(black_L, white_L, 980, dtype=np.float32),
            np.full(10, white_L, dtype=np.float32),
        ]
    )
    image = _neutral_oklab_image(l_values, height=4)

    result = _apply(
        image,
        palette_metadata=_make_palette_metadata(
            achievable_black_L=black_L,
            achievable_white_L=white_L,
        ),
        midtone_contrast=1.0,
    )

    np.testing.assert_allclose(result.image, image, atol=2e-6, rtol=0.0)
    assert result.metrics["effective_shadow_L"] == pytest.approx(black_L, abs=1e-6)
    assert result.metrics["effective_highlight_L"] == pytest.approx(white_L, abs=1e-6)


def test_shape_dtype_contract_and_exact_surface():
    rng = np.random.default_rng(42)
    image = rng.random((33, 29, 3), dtype=np.float32)
    op = C1AchievableTonemap(
        strength=0.7,
        shadow_percentile=1.0,
        highlight_percentile=98.0,
        midtone_contrast=1.3,
    )

    result = op.apply(image, context=_make_context(), progress=None)

    assert op.name == "c1_achievable_tonemap"
    assert op.preview_key == "preprocess/c1_achievable_tonemap"
    assert op.default_enabled is False
    assert op.input_domain == "srgb_f32"
    assert op.output_domain == "srgb_f32"
    assert op.required_context == frozenset({"palette_metadata"})
    assert op.order == 310.0
    assert list(op.params) == [
        "strength",
        "shadow_percentile",
        "highlight_percentile",
        "midtone_contrast",
    ]

    assert op.params["strength"].type == "float"
    assert op.params["strength"].default == 1.0
    assert op.params["strength"].min == 0.0
    assert op.params["strength"].max == 1.0

    assert op.params["shadow_percentile"].type == "float"
    assert op.params["shadow_percentile"].default == 0.5
    assert op.params["shadow_percentile"].min == 0.0
    assert op.params["shadow_percentile"].max == 10.0

    assert op.params["highlight_percentile"].type == "float"
    assert op.params["highlight_percentile"].default == 99.5
    assert op.params["highlight_percentile"].min == 90.0
    assert op.params["highlight_percentile"].max == 100.0

    assert op.params["midtone_contrast"].type == "float"
    assert op.params["midtone_contrast"].default == 1.0
    assert op.params["midtone_contrast"].min == 0.5
    assert op.params["midtone_contrast"].max == 2.0

    assert result.image.shape == image.shape
    assert result.image.dtype == np.float32
    assert np.isfinite(result.image).all()
    assert result.output_domain == "srgb_f32"
    assert set(result.metrics) == _BASE_EXPECTED_METRICS
    assert set(result.debug_maps) == {"delta_L", "clamp_mask"}
    assert result.debug_maps["delta_L"].shape == image.shape[:2]
    assert result.debug_maps["delta_L"].dtype == np.float32
    assert result.debug_maps["clamp_mask"].shape == image.shape[:2]
    assert result.debug_maps["clamp_mask"].dtype == np.uint8


def test_degenerate_palette_noops_and_sets_metric():
    image = _neutral_oklab_image(np.linspace(0.05, 0.95, 128, dtype=np.float32))
    palette = _make_palette_metadata(
        achievable_black_L=0.50,
        achievable_white_L=0.50005,
    )

    result = _apply(
        image,
        palette_metadata=palette,
    )

    np.testing.assert_allclose(result.image, image, atol=0.0, rtol=0.0)
    assert set(result.metrics) == (_BASE_EXPECTED_METRICS | {"degenerate_palette"})
    assert result.metrics["degenerate_palette"] is True
    np.testing.assert_array_equal(
        result.debug_maps["clamp_mask"],
        np.zeros(image.shape[:2], dtype=np.uint8),
    )


def test_strength_zero_is_identity_within_float_epsilon():
    image = _neutral_oklab_image(np.linspace(0.03, 0.97, 192, dtype=np.float32))

    result = _apply(image, strength=0.0)

    np.testing.assert_allclose(result.image, image, atol=2e-6, rtol=0.0)
    assert result.metrics["mean_abs_delta_L"] == pytest.approx(0.0, abs=1e-6)


def test_midtone_contrast_one_is_linear_through_mid_region():
    image = _neutral_oklab_image(np.linspace(0.01, 0.99, 1024, dtype=np.float32))
    result = _apply(image, midtone_contrast=1.0)

    source_L = srgb_f32_to_oklab_f32(image)[..., 0].astype(np.float32)
    output_L = _output_L(result)
    shadow_L = float(result.metrics["effective_shadow_L"])
    highlight_L = float(result.metrics["effective_highlight_L"])
    black_L = float(result.metrics["achievable_black_L"])
    white_L = float(result.metrics["achievable_white_L"])

    mid_mask = (source_L >= shadow_L) & (source_L <= highlight_L)
    expected_mid = black_L + (
        (source_L[mid_mask] - shadow_L)
        * ((white_L - black_L) / (highlight_L - shadow_L))
    )

    np.testing.assert_allclose(output_L[mid_mask], expected_mid, atol=2e-5, rtol=0.0)
