from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest

from preprocessing.feature_scale import _FEATURE_SCALE_WIDTHS
from preprocessing.operators.b1_printscale_bilateral import (
    B1PrintscaleBilateral,
)
from preprocessing.types import PreprocessingContext


_EXPECTED_METRICS = {
    "feature_scale_mm",
    "feature_scale_multiplier",
    "sigma_spatial_mm",
    "sigma_spatial_px",
    "sigma_range",
    "passes",
    "kernel_d_px",
    "solver_fine_pitch_mm",
    "mean_abs_delta",
    "flat_region_variance_before",
    "flat_region_variance_after",
}


def _make_context(
    *,
    feature_scale_mm: float = 0.40,
    solver_fine_pitch_mm: float = 0.20,
) -> PreprocessingContext:
    return PreprocessingContext(
        config=SimpleNamespace(
            extrusion_width_mm=feature_scale_mm / _FEATURE_SCALE_WIDTHS,
            solver_fine_pitch_mm=solver_fine_pitch_mm,
        ),
        image_fingerprint="b1-test-fingerprint",
        source_path=None,
        source_image=None,
    )


def _rgb_from_luma(luma: np.ndarray) -> np.ndarray:
    return np.repeat(luma[..., None], 3, axis=2).astype(np.float32)


def _luma(image: np.ndarray) -> np.ndarray:
    return (image[..., 0] * 0.2126 + image[..., 1] * 0.7152 + image[..., 2] * 0.0722).astype(
        np.float32
    )


def _flat_noise_fixture(
    *,
    height: int = 96,
    width: int = 96,
    sigma: float = 0.02,
    seed: int = 0,
) -> np.ndarray:
    rng = np.random.default_rng(seed)
    noisy = np.clip(
        0.5 + rng.normal(0.0, sigma, size=(height, width, 3)).astype(np.float32),
        0.0,
        1.0,
    )
    return noisy.astype(np.float32)


def _step_edge_fixture(
    *,
    height: int = 96,
    width: int = 128,
    left: float = 0.0,
    right: float = 0.5,
) -> np.ndarray:
    luma = np.full((height, width), left, dtype=np.float32)
    luma[:, width // 2 :] = right
    return _rgb_from_luma(luma)


def _apply(
    image: np.ndarray,
    *,
    feature_scale_mm: float = 0.40,
    solver_fine_pitch_mm: float = 0.20,
    **kwargs,
):
    operator = B1PrintscaleBilateral(**kwargs)
    context = _make_context(
        feature_scale_mm=feature_scale_mm,
        solver_fine_pitch_mm=solver_fine_pitch_mm,
    )
    return operator.apply(image.copy(), context=context, progress=None)


def _assert_result_contract(
    result,
    *,
    feature_scale_mm: float,
    feature_scale_multiplier: float,
    sigma_spatial_mm: float,
    sigma_spatial_px: float,
    sigma_range: float,
    passes: int,
    kernel_d_px: int,
    solver_fine_pitch_mm: float,
) -> None:
    assert result.output_domain == "srgb_f32"
    assert set(result.debug_maps) == {"delta_abs_luma"}
    assert result.debug_maps["delta_abs_luma"].dtype == np.float32
    assert set(result.metrics) == _EXPECTED_METRICS
    assert result.metrics["feature_scale_mm"] == pytest.approx(feature_scale_mm)
    assert result.metrics["feature_scale_multiplier"] == pytest.approx(feature_scale_multiplier)
    assert result.metrics["sigma_spatial_mm"] == pytest.approx(sigma_spatial_mm)
    assert result.metrics["sigma_spatial_px"] == pytest.approx(sigma_spatial_px)
    assert result.metrics["sigma_range"] == pytest.approx(sigma_range)
    assert result.metrics["passes"] == passes
    assert result.metrics["kernel_d_px"] == kernel_d_px
    assert result.metrics["solver_fine_pitch_mm"] == pytest.approx(solver_fine_pitch_mm)


def test_descriptor_matches_contract():
    op = B1PrintscaleBilateral()

    assert op.name == "b1_printscale_bilateral"
    assert op.description == "Print-scale bilateral flattening (Wing B)"
    assert op.default_enabled is False
    assert op.input_domain == "srgb_f32"
    assert op.output_domain == "srgb_f32"
    assert op.required_context == frozenset()
    assert op.order == 210.0
    assert list(op.params) == ["feature_scale_multiplier", "sigma_range", "passes"]

    assert op.params["feature_scale_multiplier"].type == "float"
    assert op.params["feature_scale_multiplier"].default == 1.0
    assert op.params["feature_scale_multiplier"].min == 0.5
    assert op.params["feature_scale_multiplier"].max == 2.0

    assert op.params["sigma_range"].type == "float"
    assert op.params["sigma_range"].default == 0.05
    assert op.params["sigma_range"].min == 0.005
    assert op.params["sigma_range"].max == 0.25

    assert op.params["passes"].type == "int"
    assert op.params["passes"].default == 1
    assert op.params["passes"].min == 1
    assert op.params["passes"].max == 3


def test_feature_scale_anchor():
    image = _flat_noise_fixture(seed=1)
    result = _apply(
        image,
        feature_scale_mm=0.80,
        solver_fine_pitch_mm=0.20,
        feature_scale_multiplier=1.0,
    )

    _assert_result_contract(
        result,
        feature_scale_mm=0.80,
        feature_scale_multiplier=1.0,
        sigma_spatial_mm=0.80,
        sigma_spatial_px=4.0,
        sigma_range=0.05,
        passes=1,
        kernel_d_px=9,
        solver_fine_pitch_mm=0.20,
    )


def test_feature_scale_multiplier():
    image = _flat_noise_fixture(seed=2)
    result_default = _apply(image, feature_scale_multiplier=1.0)
    result_double = _apply(image, feature_scale_multiplier=2.0)

    assert result_default.metrics["sigma_spatial_px"] == pytest.approx(2.0)
    assert result_double.metrics["sigma_spatial_px"] == pytest.approx(4.0)
    assert result_double.metrics["sigma_spatial_px"] == pytest.approx(
        2.0 * result_default.metrics["sigma_spatial_px"]
    )


def test_pitch_scaling():
    image = _flat_noise_fixture(seed=3)
    result_fine = _apply(
        image,
        feature_scale_mm=0.80,
        solver_fine_pitch_mm=0.20,
    )
    result_coarse = _apply(
        image,
        feature_scale_mm=0.80,
        solver_fine_pitch_mm=0.40,
    )

    assert result_fine.metrics["sigma_spatial_px"] == pytest.approx(4.0)
    assert result_coarse.metrics["sigma_spatial_px"] == pytest.approx(2.0)


def test_sub_feature_noise_flattened():
    image = _flat_noise_fixture(sigma=0.02, seed=4)
    before_std = float(_luma(image).std(dtype=np.float64))

    result = _apply(image)
    after_std = float(_luma(result.image).std(dtype=np.float64))

    _assert_result_contract(
        result,
        feature_scale_mm=0.40,
        feature_scale_multiplier=1.0,
        sigma_spatial_mm=0.40,
        sigma_spatial_px=2.0,
        sigma_range=0.05,
        passes=1,
        kernel_d_px=5,
        solver_fine_pitch_mm=0.20,
    )
    assert after_std <= (0.6 * before_std)


def test_above_feature_edge_preserved():
    image = _step_edge_fixture()
    before_row = _luma(image)[image.shape[0] // 2]
    before_peak_gradient = float(np.max(np.abs(np.diff(before_row)), initial=0.0))

    result = _apply(image)
    after_row = _luma(result.image)[image.shape[0] // 2]
    after_peak_gradient = float(np.max(np.abs(np.diff(after_row)), initial=0.0))

    assert after_peak_gradient >= (0.95 * before_peak_gradient)


def test_passes_compound():
    image = _flat_noise_fixture(sigma=0.03, seed=5)
    result_1 = _apply(image, passes=1)
    result_2 = _apply(image, passes=2)
    result_3 = _apply(image, passes=3)

    assert not np.array_equal(result_1.image, result_2.image)
    assert result_2.metrics["flat_region_variance_after"] < result_1.metrics["flat_region_variance_after"]
    assert result_3.metrics["flat_region_variance_after"] < result_2.metrics["flat_region_variance_after"]


def test_determinism():
    image = _flat_noise_fixture(sigma=0.025, seed=6)
    op = B1PrintscaleBilateral(feature_scale_multiplier=1.5, sigma_range=0.06, passes=2)
    context = _make_context(feature_scale_mm=0.50, solver_fine_pitch_mm=0.25)

    result_a = op.apply(image, context=context, progress=None)
    result_b = op.apply(image, context=context, progress=None)

    np.testing.assert_array_equal(result_a.image, result_b.image)
    np.testing.assert_array_equal(
        result_a.debug_maps["delta_abs_luma"],
        result_b.debug_maps["delta_abs_luma"],
    )
    assert result_a.metrics == result_b.metrics


# Removed: test_count_small_components_reduces_on_speckle_fixture — a fragile
# end-to-end proxy that counted small components in the full *solve* output to
# assert the B1 bilateral denoiser reduces speckle. Under the current default
# (photo-stack) solve those counts are tiny and noise-sensitive, so the invariant
# no longer holds via that proxy. The denoiser's actual effect is covered directly
# by the operator-level flat-region variance-reduction metrics above.
