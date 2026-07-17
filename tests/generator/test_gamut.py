"""Tests for gamut mapping — vectorised batch."""
import numpy as np
import sys
from types import SimpleNamespace
from pathlib import Path
from scipy.spatial import KDTree


from tests.generator.profile_fixture import PROFILES_DIR as _PROFILES_DIR

from model import load_profile, load_profiles
from lut import build_luts
def _make_luts():
    wb = load_profile("panchroma-matte-cotton-white", profiles_dir=_PROFILES_DIR)
    profiles = load_profiles(
        ["bambu-basic-cyan", "bambu-basic-yellow", "bambu-basic-magenta"],
        profiles_dir=_PROFILES_DIR,
    )
    return build_luts(
        profiles, wb_profile=wb, wc_profile=wb,
        layer_height=0.08, max_layers=10, d_wb=0.20,
        d_wc_min=0.08, k_max=2, verbose=False, use_cache=False,
    )


from lut import LUTEntry, build_hull_from_luts, nearest_sample_de_unweighted
from solve import gamut_map_hull_batch, gamut_map_hue_preserving_batch


def _lut_from_points(points: np.ndarray, chroma_weight: float = 1.0):
    points = np.asarray(points, dtype=np.float32)
    tree_points = points.copy()
    if chroma_weight != 1.0:
        tree_points[:, 0] /= chroma_weight
    return [
        LUTEntry(
            filaments=("unit",),
            thicknesses=np.zeros((len(points), 1), dtype=np.float32),
            cap_thicknesses=np.zeros(len(points), dtype=np.float32),
            oklab=points,
            tree=KDTree(tree_points),
            chroma_weight=chroma_weight,
        )
    ]


def _sparse_tetrahedron_luts():
    points = np.asarray(
        [
            [0.20, -0.20, -0.20],
            [0.80, -0.20, 0.20],
            [0.50, 0.30, -0.10],
            [0.50, 0.05, 0.45],
        ],
        dtype=np.float32,
    )
    return _lut_from_points(points)


def _oklab_hue(points: np.ndarray) -> np.ndarray:
    arr = np.asarray(points, dtype=np.float32)
    return (np.degrees(np.arctan2(arr[..., 2], arr[..., 1])) % 360.0).astype(np.float32)


def test_hull_projects_outside_points_back_to_boundary():
    """Points just outside known facets should project onto the hull boundary."""
    luts = _sparse_tetrahedron_luts()
    hull = build_hull_from_luts(luts)

    boundary_points = np.asarray(
        [hull.points[simplex].mean(axis=0) for simplex in hull.simplices[:2]],
        dtype=np.float32,
    )
    targets = (
        boundary_points + 0.2 * hull.equations[:2, :3]
    ).astype(np.float32)

    mapped, mask = gamut_map_hull_batch(targets, luts, hull)

    assert np.all(mask)
    np.testing.assert_allclose(mapped, boundary_points, atol=1e-6, rtol=0.0)
    signed_distances = mapped @ hull.equations[:, :3].T + hull.equations[:, 3]
    assert float(signed_distances.max()) <= 1e-6


def test_hull_preserves_in_gamut():
    """In-gamut pixels should not be modified by hull projection."""
    luts = _make_luts()
    hull = build_hull_from_luts(luts)
    targets = np.array([[0.5, 0.0, 0.0]], dtype=np.float32)
    mapped, mask = gamut_map_hull_batch(targets, luts, hull, de_threshold=0.05)
    if not mask[0]:
        np.testing.assert_array_almost_equal(mapped[0], targets[0])


def test_hull_preserves_sparse_interior_points():
    """Interior hull points are reachable even when no discrete LUT point is nearby."""
    luts = _sparse_tetrahedron_luts()
    hull = build_hull_from_luts(luts)
    target = np.mean(luts[0].oklab, axis=0, keepdims=True).astype(np.float32)

    mapped, mask = gamut_map_hull_batch(target, luts, hull, de_threshold=0.05)

    # Unified out-of-gamut detection is nearest discrete sample distance, so
    # sparse interior holes can be flagged even though hull mode must not move them.
    assert bool(mask[0])
    np.testing.assert_allclose(mapped, target, atol=1e-6)


def test_hull_shape():
    """Output shape matches input."""
    luts = _make_luts()
    hull = build_hull_from_luts(luts)
    targets = np.random.default_rng(0).random((30, 3), dtype=np.float32) * 0.8
    mapped, mask = gamut_map_hull_batch(targets, luts, hull)
    assert mapped.shape == (30, 3)
    assert mask.shape == (30,)


def test_unified_masks_are_chroma_weight_invariant_for_hue_and_hull():
    points = np.asarray(
        [
            [0.20, -0.20, -0.20],
            [0.80, -0.20, 0.20],
            [0.50, 0.30, -0.10],
            [0.50, 0.05, 0.45],
            [0.55, 0.02, 0.01],
        ],
        dtype=np.float32,
    )
    targets = np.asarray(
        [
            [0.55, 0.02, 0.01],
            [0.55, 0.20, 0.02],
            [0.90, -0.20, 0.20],
            [0.40, 0.08, 0.08],
        ],
        dtype=np.float32,
    )
    luts_w1 = _lut_from_points(points, chroma_weight=1.0)
    luts_w3 = _lut_from_points(points, chroma_weight=3.0)

    _, hue_mask_w1 = gamut_map_hue_preserving_batch(targets, luts_w1, de_threshold=0.05)
    _, hue_mask_w3 = gamut_map_hue_preserving_batch(targets, luts_w3, de_threshold=0.05)
    np.testing.assert_array_equal(hue_mask_w1, hue_mask_w3)

    hull_w1 = build_hull_from_luts(luts_w1)
    hull_w3 = build_hull_from_luts(luts_w3)
    _, hull_mask_w1 = gamut_map_hull_batch(targets, luts_w1, hull_w1, de_threshold=0.05)
    _, hull_mask_w3 = gamut_map_hull_batch(targets, luts_w3, hull_w3, de_threshold=0.05)
    np.testing.assert_array_equal(hull_mask_w1, hull_mask_w3)


def test_hull_flags_sparse_interior_hole_but_leaves_position_unchanged():
    luts = _sparse_tetrahedron_luts()
    hull = build_hull_from_luts(luts)
    target = np.mean(luts[0].oklab, axis=0, keepdims=True).astype(np.float32)

    mapped, mask = gamut_map_hull_batch(target, luts, hull, de_threshold=0.05)

    assert bool(mask[0])
    np.testing.assert_allclose(mapped, target, atol=1e-6)


def test_unified_out_of_gamut_masks_are_tolerance_monotonic():
    luts = _sparse_tetrahedron_luts()
    hull = build_hull_from_luts(luts)
    targets = np.asarray(
        [
            [0.20, -0.20, -0.20],
            [0.43, -0.05, 0.03],
            [0.50, 0.10, 0.30],
            [0.95, 0.35, 0.35],
        ],
        dtype=np.float32,
    )

    _, hue_low = gamut_map_hue_preserving_batch(targets, luts, de_threshold=0.03)
    _, hue_high = gamut_map_hue_preserving_batch(targets, luts, de_threshold=0.20)
    assert np.all(hue_high <= hue_low)

    _, hull_low = gamut_map_hull_batch(targets, luts, hull, de_threshold=0.03)
    _, hull_high = gamut_map_hull_batch(targets, luts, hull, de_threshold=0.20)
    assert np.all(hull_high <= hull_low)


def test_runner_diagnostics_record_unified_out_of_gamut_test_and_mask():
    from pipeline.runner import _apply_target_gamut_mapping

    points = np.asarray(
        [
            [0.55, 0.00, 0.00],
            [0.55, 0.08, 0.00],
            [0.55, 0.16, 0.00],
        ],
        dtype=np.float32,
    )
    targets = np.asarray(
        [
            [0.55, 0.08, 0.00],
            [0.55, 0.30, 0.00],
        ],
        dtype=np.float32,
    )
    luts = _lut_from_points(points, chroma_weight=3.0)
    state = SimpleNamespace(
        config=SimpleNamespace(gamut_mode="chroma", de_threshold=0.05),
        solve_target_oklab=targets.copy(),
        luts=luts,
        appearance_provider=SimpleNamespace(model_kind="unit_test"),
        diagnostics={},
    )

    _apply_target_gamut_mapping(state, shape=(1, 2), apply_white_rescale=False)

    expected_mask = nearest_sample_de_unweighted(luts, targets) > 0.05
    diag = state.diagnostics["__target_gamut_mapping__"]
    assert diag["oog_test"] == "nearest_sample_unweighted_v1"
    assert diag["remapped_count"] == int(np.count_nonzero(expected_mask))
    np.testing.assert_array_equal(
        state.diagnostics["__target_gamut_mask__"],
        expected_mask.reshape(1, 2).astype(np.float32),
    )


def test_hue_preserving_mapping_keeps_hue_for_remapped_pixels():
    points = np.asarray(
        [
            [0.50, 0.00, 0.00],
            [0.50, 0.05, 0.05],
        ],
        dtype=np.float32,
    )
    luts = _lut_from_points(points)
    targets = np.asarray([[0.50, 0.30, 0.30]], dtype=np.float32)

    mapped, mask = gamut_map_hue_preserving_batch(targets, luts, de_threshold=0.03)

    assert bool(mask[0])
    assert not np.allclose(mapped[0], targets[0])
    np.testing.assert_allclose(_oklab_hue(mapped)[0], _oklab_hue(targets)[0], atol=1e-5)


def test_hue_preserving_in_range_lightness_contract():
    points = np.asarray(
        [
            [0.50, 0.00, 0.00],
            [0.50, 0.05, 0.02],
            [0.50, 0.10, 0.04],
        ],
        dtype=np.float32,
    )
    luts = _lut_from_points(points)
    targets = np.asarray([[0.50, 0.30, 0.12]], dtype=np.float32)

    hue_mapped, hue_mask = gamut_map_hue_preserving_batch(targets, luts, de_threshold=0.03)

    expected_mask = nearest_sample_de_unweighted(luts, targets) > 0.03
    np.testing.assert_array_equal(hue_mask, expected_mask)
    np.testing.assert_allclose(hue_mapped[:, 0], targets[:, 0], atol=1e-7, rtol=0.0)
    np.testing.assert_allclose(_oklab_hue(hue_mapped), _oklab_hue(targets), atol=1e-5)
    mapped_chroma = np.linalg.norm(hue_mapped[:, 1:], axis=1)
    target_chroma = np.linalg.norm(targets[:, 1:], axis=1)
    assert np.all(mapped_chroma <= target_chroma + 1e-7)


def test_hue_preserving_bright_tint_descends_without_hue_shift_or_white_clip():
    points = np.asarray(
        [
            [0.55, 0.00, 0.00],
            [0.80, 0.00, 0.00],
            [0.80, 0.05, 0.00],
        ],
        dtype=np.float32,
    )
    luts = _lut_from_points(points)
    target = np.asarray([[1.00, 0.10, 0.00]], dtype=np.float32)
    threshold = 0.06

    mapped, mask = gamut_map_hue_preserving_batch(target, luts, de_threshold=threshold)

    assert bool(mask[0])
    assert mapped[0, 0] < target[0, 0]
    assert mapped[0, 0] <= 0.80 + threshold
    assert mapped[0, 1] > 0.0
    np.testing.assert_allclose(_oklab_hue(mapped)[0], _oklab_hue(target)[0], atol=1e-5)


def test_hue_preserving_leaves_in_gamut_targets_bit_identical():
    points = np.asarray(
        [
            [0.50, 0.00, 0.00],
            [0.50, 0.08, 0.02],
        ],
        dtype=np.float32,
    )
    luts = _lut_from_points(points)
    targets = np.asarray(
        [
            [0.50, 0.08, 0.02],
            [0.50, 0.00, 0.00],
        ],
        dtype=np.float32,
    )

    mapped, mask = gamut_map_hue_preserving_batch(targets, luts, de_threshold=0.03)

    np.testing.assert_array_equal(mask, np.asarray([False, False]))
    np.testing.assert_array_equal(mapped, targets)


def test_runner_dispatches_hue_preserving_mode_and_rejects_unknown_mode():
    from pipeline.runner import _apply_target_gamut_mapping
    import pytest

    points = np.asarray(
        [
            [0.55, 0.00, 0.00],
            [0.80, 0.05, 0.00],
        ],
        dtype=np.float32,
    )
    targets = np.asarray([[1.00, 0.10, 0.00]], dtype=np.float32)
    state = SimpleNamespace(
        config=SimpleNamespace(gamut_mode="hue_preserving", de_threshold=0.06),
        solve_target_oklab=targets.copy(),
        luts=_lut_from_points(points),
        appearance_provider=SimpleNamespace(model_kind="unit_test"),
        diagnostics={},
    )

    _apply_target_gamut_mapping(state, shape=(1, 1), apply_white_rescale=False)

    diag = state.diagnostics["__target_gamut_mapping__"]
    assert diag["requested_mode"] == "hue_preserving"
    assert diag["effective_mode"] == "hue_preserving"
    assert diag["oog_test"] == "nearest_sample_unweighted_v1"
    assert diag["remapped_count"] == 1
    hue_mapped = state.solve_target_oklab.copy()

    alias_state = SimpleNamespace(
        config=SimpleNamespace(gamut_mode="chroma", de_threshold=0.06),
        solve_target_oklab=targets.copy(),
        luts=_lut_from_points(points),
        appearance_provider=SimpleNamespace(model_kind="unit_test"),
        diagnostics={},
    )

    _apply_target_gamut_mapping(alias_state, shape=(1, 1), apply_white_rescale=False)

    alias_diag = alias_state.diagnostics["__target_gamut_mapping__"]
    assert alias_diag["requested_mode"] == "chroma"
    assert alias_diag["effective_mode"] == "hue_preserving"
    assert alias_diag["oog_test"] == "nearest_sample_unweighted_v1"
    np.testing.assert_allclose(alias_state.solve_target_oklab, hue_mapped, atol=0.0, rtol=0.0)

    bad_state = SimpleNamespace(
        config=SimpleNamespace(gamut_mode="mystery", de_threshold=0.06),
        solve_target_oklab=targets.copy(),
        luts=_lut_from_points(points),
        diagnostics={},
    )
    with pytest.raises(ValueError, match="hue_preserving"):
        _apply_target_gamut_mapping(bad_state, shape=(1, 1), apply_white_rescale=False)
