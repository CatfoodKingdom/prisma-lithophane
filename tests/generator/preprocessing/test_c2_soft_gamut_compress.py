from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

from preprocessing.color_convert import (
    oklab_f32_to_srgb_f32,
    srgb_f32_to_oklab_f32,
)
from preprocessing.operators.c2_soft_gamut_compress import (
    C2SoftGamutCompress,
    _query_chroma_bound,
)
from preprocessing.palette_metadata import PaletteMetadata
from preprocessing.types import PreprocessingContext


def _make_palette_metadata(
    *,
    hue_degrees: np.ndarray | None = None,
    max_chroma_by_hue: np.ndarray | None = None,
    l_bin_centers: np.ndarray | None = None,
    max_chroma_by_hue_l: np.ndarray | None = None,
) -> PaletteMetadata:
    if hue_degrees is None:
        hue_degrees = np.linspace(0.0, 330.0, 12, dtype=np.float32)
    if max_chroma_by_hue is None:
        max_chroma_by_hue = np.full(hue_degrees.shape, 0.12, dtype=np.float32)
    if l_bin_centers is None:
        l_bin_centers = np.linspace(0.0, 1.0, 8, dtype=np.float32)
    if max_chroma_by_hue_l is None:
        max_chroma_by_hue_l = np.repeat(
            np.asarray(max_chroma_by_hue, dtype=np.float32)[:, np.newaxis],
            len(l_bin_centers),
            axis=1,
        )
    return PaletteMetadata(
        achievable_black_oklab=np.array([0.0, 0.0, 0.0], dtype=np.float32),
        achievable_white_oklab=np.array([1.0, 0.0, 0.0], dtype=np.float32),
        max_chroma_by_hue=np.asarray(max_chroma_by_hue, dtype=np.float32),
        hue_degrees=np.asarray(hue_degrees, dtype=np.float32),
        l_bin_centers=np.asarray(l_bin_centers, dtype=np.float32),
        max_chroma_by_hue_l=np.asarray(max_chroma_by_hue_l, dtype=np.float32),
        request_fingerprint="c2-test-palette-fp",
    )


def _make_context(
    palette_metadata: PaletteMetadata,
) -> PreprocessingContext:
    return PreprocessingContext(
        config=SimpleNamespace(),
        image_fingerprint="c2-test-fp",
        source_path=None,
        source_image=None,
        palette_metadata=palette_metadata,
    )


def _oklab_row_from_lch(
    chroma_values: np.ndarray,
    *,
    lightness: float = 0.68,
    hue_degrees: float,
) -> np.ndarray:
    chroma_values = np.asarray(chroma_values, dtype=np.float32)
    hue_radians = np.deg2rad(np.float32(hue_degrees))
    a = (chroma_values * np.cos(hue_radians)).astype(np.float32)
    b = (chroma_values * np.sin(hue_radians)).astype(np.float32)
    l = np.full_like(chroma_values, np.float32(lightness))
    return np.stack([l, a, b], axis=-1).reshape(1, chroma_values.size, 3)


def _oklab_image_to_srgb(oklab: np.ndarray) -> np.ndarray:
    return np.clip(oklab_f32_to_srgb_f32(oklab), 0.0, 1.0).astype(np.float32)


def _oklab_chroma(oklab: np.ndarray) -> np.ndarray:
    return np.sqrt(
        np.square(oklab[..., 1], dtype=np.float32)
        + np.square(oklab[..., 2], dtype=np.float32)
    ).astype(np.float32)


def _apply_c2(
    image: np.ndarray,
    *,
    palette_metadata: PaletteMetadata,
    knee_start_ratio: float = 0.85,
    knee_softness: float = 0.50,
):
    op = C2SoftGamutCompress(
        knee_start_ratio=knee_start_ratio,
        knee_softness=knee_softness,
    )
    result = op.apply(
        image,
        context=_make_context(palette_metadata),
        progress=None,
    )
    output_oklab = srgb_f32_to_oklab_f32(result.image)
    return op, result, output_oklab


def test_fixed_hue_chroma_ramp_stays_monotonic_in_output_chroma():
    chroma_values = np.linspace(0.0, 0.18, 11, dtype=np.float32)
    image = _oklab_image_to_srgb(
        _oklab_row_from_lch(chroma_values, hue_degrees=32.0)
    )
    input_oklab = srgb_f32_to_oklab_f32(image)
    bound = float(_oklab_chroma(input_oklab).max() * 0.70)
    metadata = _make_palette_metadata(
        max_chroma_by_hue=np.full(12, bound, dtype=np.float32)
    )

    _, result, output_oklab = _apply_c2(image, palette_metadata=metadata)

    assert np.any(_oklab_chroma(input_oklab) > (0.85 * bound))
    output_chroma = _oklab_chroma(output_oklab)[0]
    assert np.all(np.diff(output_chroma) >= -1e-6)
    assert result.metrics["pixels_over_bound_before"] >= 1


def test_pixels_below_the_knee_are_unchanged_within_float_epsilon():
    chroma_values = np.array([0.01, 0.03, 0.05, 0.07], dtype=np.float32)
    image = _oklab_image_to_srgb(
        _oklab_row_from_lch(chroma_values, hue_degrees=75.0)
    )
    input_oklab = srgb_f32_to_oklab_f32(image)
    input_chroma = _oklab_chroma(input_oklab)
    bound = float(input_chroma.max() / 0.60)
    metadata = _make_palette_metadata(
        max_chroma_by_hue=np.full(12, bound, dtype=np.float32)
    )

    _, result, _ = _apply_c2(image, palette_metadata=metadata)

    assert np.all(input_chroma <= (0.85 * bound))
    np.testing.assert_allclose(result.image, image, atol=5e-6, rtol=0.0)
    np.testing.assert_allclose(
        result.debug_maps["chroma_scale"],
        np.ones_like(result.debug_maps["chroma_scale"], dtype=np.float32),
        atol=1e-6,
        rtol=0.0,
    )


def test_hue_interpolation_is_continuous_across_the_359_to_0_wrap():
    query = np.array([359.0, 0.0, 1.0], dtype=np.float32)
    sample_hues = np.array([350.0, 10.0, 120.0], dtype=np.float32)
    sample_bounds = np.array([0.20, 0.30, 0.05], dtype=np.float32)

    bound = _query_chroma_bound(query, sample_hues, sample_bounds)

    np.testing.assert_allclose(
        bound,
        np.array([0.245, 0.25, 0.255], dtype=np.float32),
        atol=1e-6,
        rtol=0.0,
    )


def test_constant_lightness_grid_matches_scalar_bound_lookup_exactly():
    query = np.array([[359.0, 45.0, 181.0]], dtype=np.float32)
    query_l = np.array([[0.05, 0.55, 0.95]], dtype=np.float32)
    sample_hues = np.linspace(0.0, 330.0, 12, dtype=np.float32)
    sample_l = np.linspace(0.0, 1.0, 8, dtype=np.float32)
    scalar_bounds = np.linspace(0.10, 0.21, 12, dtype=np.float32)
    grid_bounds = np.repeat(scalar_bounds[:, np.newaxis], sample_l.size, axis=1)

    scalar = _query_chroma_bound(query, sample_hues, scalar_bounds)
    lightness_aware = _query_chroma_bound(
        query,
        sample_hues,
        grid_bounds,
        query_lightness=query_l,
        sample_lightness=sample_l,
    )

    np.testing.assert_allclose(lightness_aware, scalar, rtol=0.0, atol=0.0)


def test_lightness_aware_bound_compresses_high_lightness_cusp_more():
    hue_degrees = 30.0
    chroma = np.float32(0.10)
    hue_radians = np.deg2rad(np.float32(hue_degrees))
    a = np.float32(chroma * np.cos(hue_radians))
    b = np.float32(chroma * np.sin(hue_radians))
    oklab = np.asarray(
        [[[0.58, a, b], [0.82, a, b]]],
        dtype=np.float32,
    )
    image = _oklab_image_to_srgb(oklab)
    input_chroma = _oklab_chroma(srgb_f32_to_oklab_f32(image))[0]

    hue_samples = np.linspace(0.0, 330.0, 12, dtype=np.float32)
    l_samples = np.linspace(0.45, 0.85, 8, dtype=np.float32)
    bounds_by_l = np.full((hue_samples.size, l_samples.size), 0.18, dtype=np.float32)
    bounds_by_l[:, l_samples >= 0.75] = np.float32(0.06)
    metadata = _make_palette_metadata(
        hue_degrees=hue_samples,
        max_chroma_by_hue=bounds_by_l.max(axis=1),
        l_bin_centers=l_samples,
        max_chroma_by_hue_l=bounds_by_l,
    )

    _, _result, output_oklab = _apply_c2(image, palette_metadata=metadata)

    output_chroma = _oklab_chroma(output_oklab)[0]
    delta = input_chroma - output_chroma
    assert delta[1] > delta[0] + 1e-4


def test_near_neutral_pixels_do_not_pick_up_spurious_hue():
    oklab = np.array(
        [
            [[0.62, 1.0e-6, -2.0e-6], [0.62, -3.0e-6, 1.0e-6]],
            [[0.62, 0.0, 0.0], [0.62, 2.0e-6, 2.0e-6]],
        ],
        dtype=np.float32,
    )
    image = _oklab_image_to_srgb(oklab)
    metadata = _make_palette_metadata(
        max_chroma_by_hue=np.full(12, 0.16, dtype=np.float32)
    )

    _, result, output_oklab = _apply_c2(
        image,
        palette_metadata=metadata,
    )

    assert np.max(_oklab_chroma(output_oklab)) < 1e-5
    np.testing.assert_allclose(
        result.debug_maps["chroma_scale"],
        np.ones_like(result.debug_maps["chroma_scale"], dtype=np.float32),
        atol=1e-6,
        rtol=0.0,
    )
    assert result.metrics["mean_delta_chroma"] == pytest.approx(0.0, abs=5e-6)


def test_degenerate_palette_noops_and_emits_flag():
    chroma_values = np.array([0.02, 0.06, 0.10, 0.14], dtype=np.float32)
    image = _oklab_image_to_srgb(
        _oklab_row_from_lch(chroma_values, hue_degrees=18.0)
    )
    metadata = _make_palette_metadata(
        max_chroma_by_hue=np.full(12, 5.0e-5, dtype=np.float32)
    )

    _, result, _ = _apply_c2(
        image,
        palette_metadata=metadata,
    )

    np.testing.assert_allclose(result.image, image, atol=5e-6, rtol=0.0)
    assert result.metrics["degenerate_palette"] is True
    assert set(result.debug_maps) == {"chroma_scale", "over_bound_mask"}
    assert np.count_nonzero(result.debug_maps["over_bound_mask"]) == 0
    assert result.metrics["mean_delta_chroma"] == pytest.approx(0.0, abs=1e-6)


def test_shape_dtype_contract_surface_and_determinism_are_preserved_without_nans():
    op = C2SoftGamutCompress(knee_start_ratio=0.90, knee_softness=0.70)
    assert op.name == "c2_soft_gamut_compress"
    assert op.preview_key == "preprocess/c2_soft_gamut_compress"
    assert op.input_domain == "srgb_f32"
    assert op.output_domain == "srgb_f32"
    assert op.order == 330.0
    assert op.default_enabled is False
    assert op.required_context == frozenset({"palette_metadata"})
    assert list(op.params) == ["knee_start_ratio", "knee_softness"]
    assert op.params["knee_start_ratio"].type == "float"
    assert op.params["knee_start_ratio"].default == 0.85
    assert op.params["knee_start_ratio"].min == 0.50
    assert op.params["knee_start_ratio"].max == 1.00
    assert op.params["knee_softness"].type == "float"
    assert op.params["knee_softness"].default == 0.50
    assert op.params["knee_softness"].min == 0.05
    assert op.params["knee_softness"].max == 1.50

    rng = np.random.default_rng(7)
    image = rng.random((19, 23, 3), dtype=np.float32)
    metadata = _make_palette_metadata(
        max_chroma_by_hue=np.full(12, 0.14, dtype=np.float32)
    )

    context = _make_context(metadata)
    result = op.apply(image, context=context, progress=None)
    result_repeat = op.apply(image, context=context, progress=None)

    assert result.output_domain == "srgb_f32"
    assert result.image.shape == image.shape
    assert result.image.dtype == np.float32
    assert np.isfinite(result.image).all()
    assert set(result.metrics) == {
        "pixels_over_bound_before",
        "pixels_over_bound_after",
        "mean_delta_chroma",
    }
    assert set(result.debug_maps) == {"chroma_scale", "over_bound_mask"}
    assert result.debug_maps["chroma_scale"].shape == image.shape[:2]
    assert result.debug_maps["chroma_scale"].dtype == np.float32
    assert result.debug_maps["over_bound_mask"].shape == image.shape[:2]
    assert result.debug_maps["over_bound_mask"].dtype == np.uint8
    np.testing.assert_allclose(result.image, result_repeat.image, atol=0.0, rtol=0.0)
    assert result.metrics == result_repeat.metrics
    np.testing.assert_allclose(
        result.debug_maps["chroma_scale"],
        result_repeat.debug_maps["chroma_scale"],
        atol=0.0,
        rtol=0.0,
    )
    np.testing.assert_array_equal(
        result.debug_maps["over_bound_mask"],
        result_repeat.debug_maps["over_bound_mask"],
    )
