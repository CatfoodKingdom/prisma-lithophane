"""Tests for Prisma/generator/pipeline/derived_views.py.

Phase 3 commit 2 — pure derivation helpers that project a ``SolvedMaterialPlan``
back into legacy raster compatibility views.

One-way ownership rule: derived views are always fresh arrays derived from the
plan, never the other way around. Mutating a derived view must not leak back
into the plan.
"""
from __future__ import annotations

import numpy as np
import pytest

from pipeline.solved_material_plan import SolvedMaterialPlan, StackDefinition
from pipeline.derived_views import (
    committed_stack_label_map,
    per_filament_thickness_maps,
    cap_height_map_view,
    compatibility_thickness_maps,
)


# ── Fixtures ────────────────────────────────────────────────────────────────


def _make_plan(
    segment_id_map: np.ndarray,
    segment_stack_id: np.ndarray,
    stack_table: tuple[StackDefinition, ...],
    cap: np.ndarray | None = None,
) -> SolvedMaterialPlan:
    if cap is None:
        cap = np.full(segment_id_map.shape, 0.24, dtype=np.float32)
    return SolvedMaterialPlan(
        image_domain_width_mm=float(segment_id_map.shape[1] * 2.0),
        image_domain_height_mm=float(segment_id_map.shape[0] * 2.0),
        image_sample_pitch_mm=2.0,
        solver_fine_pitch_mm=2.0,
        color_region_target_mm=2.0,
        segment_id_map=segment_id_map,
        segment_stack_id=segment_stack_id,
        stack_table=stack_table,
        cap_height_map=cap,
    )


def _two_stack_plan() -> SolvedMaterialPlan:
    seg = np.array(
        [
            [0, 0, 1],
            [0, 1, 1],
        ],
        dtype=np.int32,
    )
    assign = np.array([0, 1], dtype=np.int32)
    table = (
        StackDefinition.from_mapping({"cyan": 0.16}),
        StackDefinition.from_mapping({"cyan": 0.08, "magenta": 0.32}),
    )
    return _make_plan(seg, assign, table)


# ── committed_stack_label_map ───────────────────────────────────────────────


def test_committed_stack_label_map_matches_assignment_per_segment():
    plan = _two_stack_plan()
    label = committed_stack_label_map(plan)
    expected = np.array(
        [
            [0, 0, 1],
            [0, 1, 1],
        ]
    )
    assert np.array_equal(label, expected)


def test_committed_stack_label_map_shape_matches_segment_map():
    plan = _two_stack_plan()
    label = committed_stack_label_map(plan)
    assert label.shape == plan.segment_id_map.shape


def test_committed_stack_label_map_reflects_reassignment_after_derivation():
    """segment_stack_id is mutable; re-deriving picks up the new state."""
    plan = _two_stack_plan()
    before = committed_stack_label_map(plan)
    plan.segment_stack_id[0] = 1  # flip segment 0 onto stack 1
    after = committed_stack_label_map(plan)
    assert np.array_equal(
        after,
        np.full_like(plan.segment_id_map, 1),
    )
    assert not np.array_equal(before, after)


def test_committed_stack_label_map_is_fresh_array_not_plan_internal():
    """Mutating the derived view must not leak into the plan."""
    plan = _two_stack_plan()
    label = committed_stack_label_map(plan)
    label[0, 0] = 99
    # segment_id_map is read-only, segment_stack_id is untouched — plan unchanged.
    assert plan.segment_stack_id[0] == 0
    assert plan.segment_id_map[0, 0] == 0


# ── per_filament_thickness_maps ─────────────────────────────────────────────


def test_per_filament_thickness_maps_covers_all_filaments_by_default():
    plan = _two_stack_plan()
    maps = per_filament_thickness_maps(plan)
    assert set(maps.keys()) == {"cyan", "magenta"}


def test_per_filament_thickness_maps_values_match_stack_content():
    plan = _two_stack_plan()
    maps = per_filament_thickness_maps(plan)

    # Stack 0 = {cyan: 0.16}; stack 1 = {cyan: 0.08, magenta: 0.32}
    expected_cyan = np.array(
        [
            [0.16, 0.16, 0.08],
            [0.16, 0.08, 0.08],
        ],
        dtype=np.float32,
    )
    expected_magenta = np.array(
        [
            [0.00, 0.00, 0.32],
            [0.00, 0.32, 0.32],
        ],
        dtype=np.float32,
    )
    np.testing.assert_allclose(maps["cyan"], expected_cyan)
    np.testing.assert_allclose(maps["magenta"], expected_magenta)


def test_per_filament_thickness_maps_returns_float32_arrays():
    plan = _two_stack_plan()
    maps = per_filament_thickness_maps(plan)
    for arr in maps.values():
        assert arr.dtype == np.float32
        assert arr.shape == plan.segment_id_map.shape


def test_per_filament_thickness_maps_filament_ids_filter_restricts_output():
    plan = _two_stack_plan()
    maps = per_filament_thickness_maps(plan, filament_ids=["cyan"])
    assert set(maps.keys()) == {"cyan"}


def test_per_filament_thickness_maps_zeros_for_filament_absent_from_all_stacks():
    plan = _two_stack_plan()
    maps = per_filament_thickness_maps(plan, filament_ids=["yellow"])
    assert "yellow" in maps
    np.testing.assert_array_equal(
        maps["yellow"], np.zeros(plan.segment_id_map.shape, dtype=np.float32)
    )


def test_per_filament_thickness_maps_one_way_ownership():
    plan = _two_stack_plan()
    maps = per_filament_thickness_maps(plan)
    maps["cyan"][0, 0] = 99.0
    # Re-deriving yields the original stack-0 cyan value, not the mutation.
    maps2 = per_filament_thickness_maps(plan)
    assert maps2["cyan"][0, 0] == pytest.approx(0.16)


def test_per_filament_thickness_maps_reflects_assignment_changes():
    plan = _two_stack_plan()
    plan.segment_stack_id[0] = 1  # flip segment 0 onto stack 1
    maps = per_filament_thickness_maps(plan)
    # All pixels now resolve to stack 1 = {cyan: 0.08, magenta: 0.32}.
    np.testing.assert_allclose(
        maps["cyan"],
        np.full(plan.segment_id_map.shape, 0.08, dtype=np.float32),
    )
    np.testing.assert_allclose(
        maps["magenta"],
        np.full(plan.segment_id_map.shape, 0.32, dtype=np.float32),
    )


# ── cap_height_map_view ─────────────────────────────────────────────────────


def test_cap_height_map_view_matches_plan_cap():
    plan = _two_stack_plan()
    view = cap_height_map_view(plan)
    np.testing.assert_array_equal(view, plan.cap_height_map)


def test_cap_height_map_view_is_writable_copy():
    """Compat view must be safe for consumers to mutate without leaking."""
    plan = _two_stack_plan()
    view = cap_height_map_view(plan)
    assert view.flags.writeable is True
    view[0, 0] = 9.9
    assert plan.cap_height_map[0, 0] != 9.9


def test_compatibility_thickness_maps_bundle_color_and_white_cap_views():
    plan = _two_stack_plan()
    maps = compatibility_thickness_maps(plan)
    assert set(maps.keys()) == {"cyan", "magenta", "__white_cap__"}
    np.testing.assert_array_equal(maps["__white_cap__"], plan.cap_height_map)
    maps["__white_cap__"][0, 0] = 9.9
    assert plan.cap_height_map[0, 0] != 9.9


# ── one-way ownership: plan stays authoritative ─────────────────────────────


def test_segment_id_map_remains_read_only_after_any_derivation():
    plan = _two_stack_plan()
    _ = committed_stack_label_map(plan)
    _ = per_filament_thickness_maps(plan)
    _ = cap_height_map_view(plan)
    assert plan.segment_id_map.flags.writeable is False
