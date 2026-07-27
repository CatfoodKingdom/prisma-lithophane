from types import SimpleNamespace

import numpy as np

from Prisma.generator.pipeline.staged.stage4.detail import _apply_stage4_detail_cap_smoothing
from Prisma.generator.pipeline.detail_cap_smoothing import (
    DetailCapSmoothingSettings,
    cleanup_cumulative_detail_masks,
    detail_height_to_layers,
    detail_layers_to_height,
    measure_detail_cap_topology,
    remove_exact_height_speckles,
    smooth_detail_cap_layers,
)


def test_detail_height_roundtrip_uses_integer_layer_counts():
    height = np.array([[0.0, 0.08, 0.16], [0.24, 0.32, 0.64]], dtype=np.float32)

    layers = detail_height_to_layers(height, layer_height_mm=0.08)

    assert layers.dtype == np.int16
    assert layers.tolist() == [[0, 1, 2], [3, 4, 8]]
    np.testing.assert_allclose(detail_layers_to_height(layers, 0.08), height, atol=1e-6)


def test_exact_height_cleanup_replaces_one_pixel_void_with_ring_mode():
    layers = np.full((5, 5), 2, dtype=np.int16)
    layers[2, 2] = 0

    cleaned = remove_exact_height_speckles(layers, max_component_px=1, max_layer=4)

    assert cleaned[2, 2] == 2
    assert int((cleaned == 0).sum()) == 0


def test_exact_height_cleanup_preserves_component_above_threshold():
    layers = np.zeros((5, 5), dtype=np.int16)
    layers[2, 2:4] = 3

    cleaned = remove_exact_height_speckles(layers, max_component_px=1, max_layer=4)

    assert cleaned[2, 2] == 3
    assert cleaned[2, 3] == 3


def test_cumulative_cleanup_removes_tiny_material_island():
    layers = np.zeros((6, 6), dtype=np.int16)
    layers[2, 2:4] = 1

    cleaned = cleanup_cumulative_detail_masks(
        layers,
        max_component_px=2,
        max_hole_px=2,
        max_layer=4,
    )

    assert int(cleaned.sum()) == 0


def test_cumulative_cleanup_fills_tiny_internal_void():
    layers = np.ones((6, 6), dtype=np.int16)
    layers[2, 2:4] = 0

    cleaned = cleanup_cumulative_detail_masks(
        layers,
        max_component_px=2,
        max_hole_px=2,
        max_layer=4,
    )

    assert int((cleaned == 0).sum()) == 0


def test_smoothing_combines_exact_and_cumulative_cleanup_without_mutating_input():
    layers = np.zeros((10, 10), dtype=np.int16)
    layers[2:8, 2:8] = 2
    layers[3, 3] = 0
    layers[7, 7] = 4
    layers[0, 0:2] = 1
    original = layers.copy()

    result = smooth_detail_cap_layers(
        layers,
        DetailCapSmoothingSettings(
            max_layer=4,
            exact_speckle_max_px=1,
            cumulative_component_max_px=2,
            cumulative_hole_max_px=2,
        ),
    )

    np.testing.assert_array_equal(layers, original)
    assert result.smoothed_layers[3, 3] == 2
    assert int(result.smoothed_layers[0, 0:2].sum()) == 0
    assert result.after.changed_px > 0
    assert (
        result.after.topology.cumulative_components_le3
        <= result.before.topology.cumulative_components_le3
    )


def test_topology_metrics_count_exact_regions_cumulative_regions_and_holes():
    layers = np.ones((6, 6), dtype=np.int16)
    layers[2, 2] = 0
    layers[4, 4] = 2

    metrics = measure_detail_cap_topology(layers, max_layer=3)

    assert metrics.exact_components_le1 >= 1
    assert metrics.cumulative_component_count >= 2
    assert metrics.cumulative_holes_le1 >= 1


def test_stage4_luminance_detail_smoothing_updates_detail_before_final_cap():
    layers = np.zeros((10, 10), dtype=np.int16)
    layers[2:8, 2:8] = 2
    layers[3, 3] = 0
    layers[7, 7] = 4
    layers[0, 0:2] = 1
    detail = detail_layers_to_height(layers, 0.08)
    boundary = np.full_like(detail, 0.08, dtype=np.float32)
    cfg = SimpleNamespace(
        luminance_handler_enabled=True,
        detail_cap_enabled=True,
        detail_cap_smoothing_enabled=True,
        detail_cap_max_layers=4,
        detail_cap_smoothing_exact_speckle_max_px=1,
        detail_cap_smoothing_cumulative_component_max_px=2,
        detail_cap_smoothing_cumulative_hole_max_px=2,
    )

    smoothed_detail, summary = _apply_stage4_detail_cap_smoothing(
        detail_height_mm=detail,
        cfg=cfg,
        layer_height=0.08,
        boundary_cap_height_mm=boundary,
        remaining_cap_budget_mm=np.full_like(detail, 1.0, dtype=np.float32),
        desired_final_cap_target_mm=None,
    )

    assert summary is not None
    assert summary["changed_px"] > 0
    assert summary["applied"] is True
    assert summary["printability_regated"] is True
    assert smoothed_detail.shape == detail.shape
    assert not np.array_equal(smoothed_detail, detail)
    assert int(smoothed_detail[3, 3] / 0.08) == 2
    assert float(np.sum(smoothed_detail[0, 0:2])) == 0.0


def test_stage4_detail_smoothing_is_luminance_only():
    detail = np.zeros((5, 5), dtype=np.float32)
    detail[2, 2] = 0.08
    cfg = SimpleNamespace(
        luminance_handler_enabled=False,
        detail_cap_enabled=True,
        detail_cap_smoothing_enabled=True,
        detail_cap_max_layers=4,
    )

    smoothed_detail, summary = _apply_stage4_detail_cap_smoothing(
        detail_height_mm=detail,
        cfg=cfg,
        layer_height=0.08,
        boundary_cap_height_mm=np.zeros_like(detail),
        remaining_cap_budget_mm=np.full_like(detail, 1.0),
        desired_final_cap_target_mm=None,
    )

    assert summary is None
    np.testing.assert_array_equal(smoothed_detail, detail)
