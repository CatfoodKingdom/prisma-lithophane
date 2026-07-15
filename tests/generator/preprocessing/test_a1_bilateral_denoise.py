from __future__ import annotations

import numpy as np
import pytest

from preprocessing.operators.a1_bilateral_denoise import A1BilateralDenoise
from preprocessing.types import PreprocessingContext


def _make_context() -> PreprocessingContext:
    return PreprocessingContext(
        config=None,  # type: ignore[arg-type]
        image_fingerprint="test-fp",
        source_path=None,
        source_image=None,
    )


def _rgb_from_luma(luma: np.ndarray) -> np.ndarray:
    return np.repeat(luma[..., None], 3, axis=2).astype(np.float32)


def _luma(image: np.ndarray) -> np.ndarray:
    return (image[..., 0] * 0.2126 + image[..., 1] * 0.7152 + image[..., 2] * 0.0722).astype(
        np.float32
    )


def _edge_position(row: np.ndarray, threshold: float = 0.5) -> float:
    idx = int(np.argmax(row >= threshold))
    if idx == 0 or row[idx] < threshold:
        raise AssertionError("row does not cross the threshold")
    left = float(row[idx - 1])
    right = float(row[idx])
    frac = 0.0 if right == left else (threshold - left) / (right - left)
    return (idx - 1) + frac


def _rms_contrast(luma: np.ndarray) -> float:
    centered = luma - float(luma.mean(dtype=np.float64))
    return float(np.sqrt(np.mean(np.square(centered), dtype=np.float64)))


def _assert_result_contract(result, *, radius_px: int, sigma_range: float, sigma_spatial: float) -> None:
    assert result.output_domain == "srgb_f32"
    assert set(result.debug_maps) == {"delta_abs_luma"}
    assert result.debug_maps["delta_abs_luma"].dtype == np.float32
    assert set(result.metrics) == {
        "radius_px",
        "sigma_range",
        "sigma_spatial",
        "mean_abs_delta",
        "flat_patch_variance_before",
        "flat_patch_variance_after",
    }
    assert result.metrics["radius_px"] == radius_px
    assert result.metrics["sigma_range"] == pytest.approx(sigma_range)
    assert result.metrics["sigma_spatial"] == pytest.approx(sigma_spatial)


def test_flat_region_variance_reduction():
    rng = np.random.default_rng(0)
    noisy_luma = np.clip(
        0.5 + rng.normal(0.0, 0.015, size=(96, 96)).astype(np.float32),
        0.0,
        1.0,
    )
    image = _rgb_from_luma(noisy_luma)

    op = A1BilateralDenoise()
    result = op.apply(image, context=_make_context(), progress=None)

    _assert_result_contract(result, radius_px=3, sigma_range=0.04, sigma_spatial=2.0)
    assert result.debug_maps["delta_abs_luma"].shape == noisy_luma.shape
    assert result.metrics["flat_patch_variance_after"] < result.metrics["flat_patch_variance_before"]


def test_bounded_edge_displacement_on_sharp_step_edge():
    luma = np.zeros((96, 128), dtype=np.float32)
    luma[:, 64:] = 1.0
    image = _rgb_from_luma(luma)

    result = A1BilateralDenoise().apply(image, context=_make_context(), progress=None)

    _assert_result_contract(result, radius_px=3, sigma_range=0.04, sigma_spatial=2.0)
    edge_before = _edge_position(luma[luma.shape[0] // 2])
    edge_after = _edge_position(_luma(result.image)[luma.shape[0] // 2])
    assert abs(edge_after - edge_before) < 0.5


def test_shape_dtype_preservation_and_exact_contract_surface():
    rng = np.random.default_rng(1)
    image = rng.random((63, 47, 3), dtype=np.float32)

    op = A1BilateralDenoise(radius_px=4, sigma_range=0.06, sigma_spatial=3.5)
    result = op.apply(image, context=_make_context(), progress=None)

    assert op.name == "a1_bilateral_denoise"
    assert op.input_domain == "srgb_f32"
    assert op.output_domain == "srgb_f32"
    assert op.order == 120.0
    assert op.default_enabled is False
    assert op.required_context == frozenset()
    assert list(op.params) == ["radius_px", "sigma_range", "sigma_spatial"]
    assert op.params["radius_px"].type == "int"
    assert op.params["radius_px"].default == 3
    assert op.params["radius_px"].min == 1
    assert op.params["radius_px"].max == 8
    assert op.params["sigma_range"].type == "float"
    assert op.params["sigma_range"].default == 0.04
    assert op.params["sigma_range"].min == 0.005
    assert op.params["sigma_range"].max == 0.25
    assert op.params["sigma_spatial"].type == "float"
    assert op.params["sigma_spatial"].default == 2.0
    assert op.params["sigma_spatial"].min == 0.5
    assert op.params["sigma_spatial"].max == 10.0

    assert result.image.shape == image.shape
    assert result.image.dtype == np.float32
    _assert_result_contract(result, radius_px=4, sigma_range=0.06, sigma_spatial=3.5)


def test_deterministic_output_for_fixed_params_and_input():
    rng = np.random.default_rng(2)
    image = rng.random((80, 80, 3), dtype=np.float32)
    op = A1BilateralDenoise(radius_px=5, sigma_range=0.05, sigma_spatial=2.5)

    result_a = op.apply(image, context=_make_context(), progress=None)
    result_b = op.apply(image, context=_make_context(), progress=None)

    np.testing.assert_array_equal(result_a.image, result_b.image)
    np.testing.assert_array_equal(
        result_a.debug_maps["delta_abs_luma"],
        result_b.debug_maps["delta_abs_luma"],
    )
    assert result_a.metrics == result_b.metrics


def test_contrast_preservation_on_textured_regions():
    yy, xx = np.indices((96, 96))
    checker = ((xx // 4 + yy // 4) % 2).astype(np.float32)
    textured_luma = 0.35 + (0.30 * checker)
    image = _rgb_from_luma(textured_luma)

    result = A1BilateralDenoise().apply(image, context=_make_context(), progress=None)

    _assert_result_contract(result, radius_px=3, sigma_range=0.04, sigma_spatial=2.0)
    contrast_before = _rms_contrast(textured_luma)
    contrast_after = _rms_contrast(_luma(result.image))
    assert contrast_after >= (0.9 * contrast_before)
