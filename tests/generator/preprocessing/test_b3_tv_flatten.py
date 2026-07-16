from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from facade import SolveConfig, solve_full
from preprocessing import color_convert
import preprocessing.operators.b3_tv_flatten as b3_module
from preprocessing.operators.b3_tv_flatten import B3TvFlatten
from preprocessing.types import PreprocessingContext


from tests.generator.profile_fixture import PROFILES_DIR as _PROFILES_DIR


def _make_context(
    nozzle_diameter: float = 0.20,
) -> PreprocessingContext:
    return PreprocessingContext(
        config=SimpleNamespace(
            nozzle_diameter=nozzle_diameter,
        ),
        image_fingerprint="b3-test-fp",
        source_path=None,
        source_image=None,
    )


def _rgb_from_luma(luma: np.ndarray) -> np.ndarray:
    return np.repeat(luma[..., None], 3, axis=2).astype(np.float32)


def _luma(image: np.ndarray) -> np.ndarray:
    return (
        image[..., 0] * 0.2126
        + image[..., 1] * 0.7152
        + image[..., 2] * 0.0722
    ).astype(np.float32)


def _high_frequency_std(channel: np.ndarray) -> float:
    local_mean = cv2.blur(
        channel.astype(np.float32),
        (5, 5),
        borderType=cv2.BORDER_REFLECT_101,
    )
    residual = channel.astype(np.float32) - local_mean
    return float(np.std(residual, dtype=np.float64))


def _quantized_distinct_count(channel: np.ndarray, bins: int = 64) -> int:
    quantized = np.rint(np.clip(channel, 0.0, 1.0) * float(bins - 1)).astype(np.int16)
    return int(np.unique(quantized).size)


def _max_gradient(channel: np.ndarray) -> float:
    return float(np.abs(np.diff(channel.astype(np.float32), axis=1)).max())


def _count_small_components(
    thickness_maps: dict[str, np.ndarray],
    *,
    max_area_px: int = 6,
) -> int:
    total = 0
    for filament_id, arr in thickness_maps.items():
        if filament_id.startswith("__"):
            continue
        mask = np.asarray(arr, dtype=np.float32) > 1e-6
        n_labels, _, stats, _ = cv2.connectedComponentsWithStats(
            mask.astype(np.uint8),
            connectivity=8,
        )
        if n_labels <= 1:
            continue
        areas = stats[1:, cv2.CC_STAT_AREA]
        total += int(np.count_nonzero(areas <= max_area_px))
    return total


def _mirrored_detail_fixture() -> np.ndarray:
    rng = np.random.default_rng(19)
    height, width_half = 64, 32
    yy, xx = np.indices((height, width_half), dtype=np.float32)
    l_channel = np.clip(
        0.42
        + (0.18 * (xx / max(width_half - 1, 1)))
        + 0.05 * np.sin(xx / 2.3)
        + 0.04 * np.cos(yy / 3.8)
        + rng.normal(0.0, 0.04, size=(height, width_half)).astype(np.float32),
        0.0,
        1.0,
    )
    a_channel = (
        0.05 * np.sin(xx / 4.0)
        + rng.normal(0.0, 0.02, size=(height, width_half)).astype(np.float32)
    )
    b_channel = (
        -0.05 * np.cos(yy / 5.0)
        + rng.normal(0.0, 0.02, size=(height, width_half)).astype(np.float32)
    )
    oklab_half = np.stack([l_channel, a_channel, b_channel], axis=-1).astype(np.float32)
    half = np.clip(color_convert.oklab_f32_to_srgb_f32(oklab_half), 0.0, 1.0).astype(
        np.float32
    )
    return np.concatenate([half, half], axis=1)


def _assert_metrics_contract(
    result,
    *,
    feature_scale_mm: float,
    tv_weight: float,
    weight_autoscale: bool,
    effective_weight: float,
    channel_axis: str,
    n_iter_max: int,
) -> None:
    assert result.output_domain == "srgb_f32"
    assert result.metrics.keys() == {
        "feature_scale_mm",
        "tv_weight",
        "weight_autoscale",
        "effective_weight",
        "channel_axis",
        "n_iter_max",
    }
    assert result.metrics["feature_scale_mm"] == pytest.approx(feature_scale_mm)
    assert result.metrics["tv_weight"] == pytest.approx(tv_weight)
    assert result.metrics["weight_autoscale"] is weight_autoscale
    assert result.metrics["effective_weight"] == pytest.approx(effective_weight)
    assert result.metrics["channel_axis"] == channel_axis
    assert result.metrics["n_iter_max"] == n_iter_max


def _oklab_gradient_chroma_fixture() -> np.ndarray:
    rng = np.random.default_rng(3)
    height, width = 72, 72
    yy, xx = np.indices((height, width), dtype=np.float32)
    l_channel = (
        0.45
        + (0.25 * (xx / (width - 1)))
        + rng.normal(0.0, 0.03, size=(height, width)).astype(np.float32)
    )
    a_channel = 0.04 * np.sin(xx / 5.0)
    b_channel = -0.05 * np.cos(yy / 6.0)
    oklab = np.stack([l_channel, a_channel, b_channel], axis=-1).astype(np.float32)
    return np.clip(color_convert.oklab_f32_to_srgb_f32(oklab), 0.0, 1.0).astype(np.float32)


def _oklab_noise_fixture() -> np.ndarray:
    rng = np.random.default_rng(4)
    height, width = 72, 72
    l_channel = np.clip(
        0.60 + rng.normal(0.0, 0.025, size=(height, width)).astype(np.float32),
        0.25,
        0.95,
    )
    a_channel = rng.normal(0.0, 0.03, size=(height, width)).astype(np.float32)
    b_channel = rng.normal(0.0, 0.03, size=(height, width)).astype(np.float32)
    oklab = np.stack([l_channel, a_channel, b_channel], axis=-1).astype(np.float32)
    return np.clip(color_convert.oklab_f32_to_srgb_f32(oklab), 0.0, 1.0).astype(np.float32)


def _gradient_speckle_fixture() -> np.ndarray:
    rng = np.random.default_rng(123)
    height, width = 48, 48
    yy, xx = np.indices((height, width), dtype=np.float32)
    l_channel = 0.42 + (0.20 * (xx / (width - 1)))
    a_channel = (
        0.08 * np.sin(xx / 3.0)
        + rng.normal(0.0, 0.03, size=(height, width)).astype(np.float32)
    )
    b_channel = (
        0.08 * np.cos(yy / 4.0)
        + rng.normal(0.0, 0.03, size=(height, width)).astype(np.float32)
    )
    oklab = np.stack([l_channel, a_channel, b_channel], axis=-1).astype(np.float32)
    srgb = np.clip(color_convert.oklab_f32_to_srgb_f32(oklab), 0.0, 1.0)
    return np.rint(srgb * 255.0).astype(np.uint8)


def _make_solve_config(*, preprocessing_params: dict[str, dict[str, object]] | None = None) -> SolveConfig:
    return SolveConfig(
        palette=["bambu-basic-cyan", "bambu-basic-yellow", "bambu-basic-red"],
        white_base="panchroma-matte-cotton-white",
        profiles_dir=_PROFILES_DIR,
        image_sample_pitch_mm=0.20,
        solver_fine_pitch_mm=0.20,
        color_region_target_mm=0.60,
        d_wb=0.20,
        d_wc_min=0.08,
        t_max=2.5,
        k_max=3,
        de_threshold=0.05,
        gamut_mode="hull",
        use_corrections=False,
        preprocessing_params=dict(preprocessing_params or {}),
    )


def test_descriptor_matches_contract():
    op = B3TvFlatten()

    assert op.name == "b3_tv_flatten"
    assert op.description == "TV flattening at print-feature scale (Wing B)"
    assert op.default_enabled is False
    assert op.input_domain == "srgb_f32"
    assert op.output_domain == "srgb_f32"
    assert op.required_context == frozenset()
    assert op.order == 220.0
    assert list(op.params) == [
        "tv_weight",
        "weight_autoscale",
        "channel_axis",
        "n_iter_max",
    ]

    assert op.params["tv_weight"].type == "float"
    assert op.params["tv_weight"].default == 0.04
    assert op.params["tv_weight"].min == 0.005
    assert op.params["tv_weight"].max == 0.40

    assert op.params["weight_autoscale"].type == "bool"
    assert op.params["weight_autoscale"].default is True

    assert op.params["channel_axis"].type == "choice"
    assert op.params["channel_axis"].default == "oklab_L_ab"
    assert op.params["channel_axis"].choices == [
        "srgb_rgb",
        "oklab_L_ab",
        "oklab_L_only",
    ]

    assert op.params["n_iter_max"].type == "int"
    assert op.params["n_iter_max"].default == 100
    assert op.params["n_iter_max"].min == 20
    assert op.params["n_iter_max"].max == 500


def test_flat_region_becomes_piecewise_constant():
    rng = np.random.default_rng(0)
    x = np.linspace(0.2, 0.8, 96, dtype=np.float32)
    noisy_luma = np.clip(
        np.tile(x, (96, 1)) + rng.normal(0.0, 0.03, size=(96, 96)).astype(np.float32),
        0.0,
        1.0,
    )
    image = _rgb_from_luma(noisy_luma)

    result = B3TvFlatten().apply(image, context=_make_context(), progress=None)

    distinct_before = _quantized_distinct_count(noisy_luma, bins=64)
    distinct_after = _quantized_distinct_count(_luma(result.image), bins=64)
    assert result.image.shape == image.shape
    assert result.image.dtype == np.float32
    assert distinct_after < distinct_before
    assert distinct_after <= 45


def test_sharp_edge_survives():
    luma = np.zeros((96, 128), dtype=np.float32)
    luma[:, 64:] = 1.0
    image = _rgb_from_luma(luma)

    result = B3TvFlatten().apply(image, context=_make_context(), progress=None)

    grad_before = _max_gradient(luma)
    grad_after = _max_gradient(_luma(result.image))
    assert grad_after >= (0.9 * grad_before)


def test_weight_autoscale():
    image = np.full((24, 24, 3), 0.5, dtype=np.float32)
    op = B3TvFlatten()

    # 0.20 mm nozzle -> feature scale 0.40 (ratio 1.0); 0.40 mm nozzle ->
    # feature scale 0.80 (ratio 2.0). B3's TV weight autoscales linearly.
    result_040 = op.apply(image, context=_make_context(0.20), progress=None)
    result_080 = op.apply(image, context=_make_context(0.40), progress=None)

    _assert_metrics_contract(
        result_040,
        feature_scale_mm=0.40,
        tv_weight=0.04,
        weight_autoscale=True,
        effective_weight=0.04,
        channel_axis="oklab_L_ab",
        n_iter_max=100,
    )
    _assert_metrics_contract(
        result_080,
        feature_scale_mm=0.80,
        tv_weight=0.04,
        weight_autoscale=True,
        effective_weight=0.08,
        channel_axis="oklab_L_ab",
        n_iter_max=100,
    )


def test_channel_axis_oklab_L_only_preserves_chroma():
    image = _oklab_gradient_chroma_fixture()
    input_oklab = color_convert.srgb_f32_to_oklab_f32(image)

    result = B3TvFlatten(channel_axis="oklab_L_only").apply(
        image,
        context=_make_context(),
        progress=None,
    )
    output_oklab = color_convert.srgb_f32_to_oklab_f32(result.image)

    assert _high_frequency_std(output_oklab[..., 0]) < (
        0.5 * _high_frequency_std(input_oklab[..., 0])
    )
    assert np.max(np.abs(output_oklab[..., 1:] - input_oklab[..., 1:])) < 1e-5


def test_channel_axis_oklab_L_ab_flattens_both():
    image = _oklab_noise_fixture()
    input_oklab = color_convert.srgb_f32_to_oklab_f32(image)

    result = B3TvFlatten(channel_axis="oklab_L_ab").apply(
        image,
        context=_make_context(),
        progress=None,
    )
    output_oklab = color_convert.srgb_f32_to_oklab_f32(result.image)

    assert _high_frequency_std(output_oklab[..., 0]) < (
        0.5 * _high_frequency_std(input_oklab[..., 0])
    )
    assert _high_frequency_std(output_oklab[..., 1]) < (
        0.5 * _high_frequency_std(input_oklab[..., 1])
    )
    assert _high_frequency_std(output_oklab[..., 2]) < (
        0.5 * _high_frequency_std(input_oklab[..., 2])
    )


def test_lazy_import_skimage_restoration():
    sys.modules.pop("skimage.restoration", None)
    module = importlib.reload(b3_module)

    assert "skimage.restoration" not in sys.modules

    image = np.full((8, 8, 3), 0.5, dtype=np.float32)
    result = module.B3TvFlatten().apply(
        image,
        context=_make_context(),
        progress=None,
    )

    assert result.image.shape == image.shape
    assert "skimage.restoration" in sys.modules


def test_determinism():
    rng = np.random.default_rng(5)
    image = rng.random((48, 48, 3), dtype=np.float32)
    op = B3TvFlatten(tv_weight=0.05, channel_axis="oklab_L_ab", n_iter_max=120)

    result_a = op.apply(image, context=_make_context(), progress=None)
    result_b = op.apply(image, context=_make_context(), progress=None)

    np.testing.assert_array_equal(result_a.image, result_b.image)
    assert result_a.metrics == result_b.metrics


def test_count_small_components_reduces_on_gradient_speckle_fixture():
    image = _gradient_speckle_fixture()
    preprocessing_params = {
        "b3_tv_flatten": {
            "tv_weight": 0.04,
            "weight_autoscale": True,
            "channel_axis": "oklab_L_ab",
            "n_iter_max": 100,
        }
    }

    baseline = solve_full(
        image,
        _make_solve_config(preprocessing_params=preprocessing_params),
        progress=None,
        module_state={"b3_tv_flatten": False},
    )
    with_b3 = solve_full(
        image,
        _make_solve_config(preprocessing_params=preprocessing_params),
        progress=None,
        module_state={"b3_tv_flatten": True},
    )

    baseline_components = _count_small_components(baseline.thickness_maps, max_area_px=6)
    b3_components = _count_small_components(with_b3.thickness_maps, max_area_px=6)

    assert b3_components <= baseline_components
