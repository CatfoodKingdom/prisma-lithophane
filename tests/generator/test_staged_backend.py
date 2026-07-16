"""Regression guards for the experimental backend proof slice."""
import sys
from types import SimpleNamespace
from pathlib import Path

import numpy as np
import pytest

_GEN_DIR = Path(__file__).resolve().parent.parent.parent / "Prisma" / "generator"
if str(_GEN_DIR) not in sys.path:
    sys.path.insert(0, str(_GEN_DIR))

from tests.generator.profile_fixture import PROFILES_DIR as _PROFILES_DIR

from facade import SolveConfig, solve_full, solve_preview
from model import to_oklab
from pipeline import staged_runner
from pipeline.staged_bridge import build_compatibility_bundle
from pipeline.staged_artifacts import (
    FillerGeometryPlan,
    LateralZonePlan,
    PlanningDiagnosticsStream,
    Stage2ObjectiveSummary,
    VisibleRecipe,
    VisibleRecipeRawGeometryPlan,
)
from pipeline.staged_solver_helpers import (
    _vectorized_stack_ids,
    generate_stage1_zone_labels,
)
from pipeline.staged_runner import (
    _ZoneCandidateSet,
    _apply_stage2_boundary_recipe_mutation,
    _iterate_stage2_boundary_recipe_mutation,
    _apply_stage2_final_color_printability_gate,
    _apply_stage2_fine_override_seam_gate,
    _apply_stage2_fine_override_printability_gate,
    _apply_stage4_luminance_detail_authoring_printability,
    _apply_stage2_localized_width_loss_boundary_nudge,
    _apply_stage4_boundary_cap_printability_gate,
    _apply_stage4_detail_printability_gate,
    _apply_stage4_edge_aware_boundary_restore,
    _augment_zone_candidates_with_neighbor_local_bests,
    _author_stage4_detail_zones,
    _build_stage4_boundary_edge_guard,
    _build_stage4_boundary_smoothing_guide,
    _build_stage2_fine_recipe_assignments,
    _build_stage2_objective_summary,
    _summarize_zone_targets,
    _compute_stage2_recipe_pressure,
    _downsample_rgb_image,
    _effective_color_region_target_mm,
    _optimize_zone_recipe_labels,
    _project_zone_labels_to_fine,
    _prune_zone_candidate_frontiers,
    _requested_stage4_cap_maps,
    _run_coord_descent,
    _rescue_stage2_optical_frontier_candidates,
    _seed_zone_recipe_labels_with_beam,
    _shape_stage4_detail_stack_layers,
    _score_pixels_against_stack_ids,
    _score_zone_pixels_against_candidates,
    _smooth_stage4_boundary_cap,
    _split_stage2_source_edge_subzones,
    _stage4_boundary_edge_restore_weight,
    _stage4_lookup_oklab_by_count,
    _stage2_printability_failure_snapshot_from_stack_ids,
)
from pipeline.staged_printability import (
    BlueprintPrintabilitySettings,
    build_layered_blueprint_view,
    opening_width_loss,
    opening_width_structure,
    run_blueprint_printability_diagnostic,
)
from white_cap_contract import (
    PHYSICAL_GEOMETRY_METADATA_KEY,
    POLICY_LUMINANCE_DETAIL_CANONICAL,
    POLICY_STANDARD_APPEARANCE_BOUNDED_STRUCTURAL_SPLIT,
    POLICY_STANDARD_SMOOTH_VARIABLE_CANONICAL,
    WHITE_CAP_FIELD_TARGET_METADATA_KEY,
    WHITE_CAP_FIELD_TARGET_UPPER_SURFACE_KEY,
    quantized_cover_floor_mm,
)


def _offline_solve_config(**kwargs):
    defaults = {
        "appearance_model_provider": "historical_spline",
        "model_domain_ingress": False,
    }
    defaults.update(kwargs)
    return SolveConfig(**defaults)


_FINAL_VISIBLE_TARGET_POLICIES = {
    POLICY_LUMINANCE_DETAIL_CANONICAL,
    POLICY_STANDARD_APPEARANCE_BOUNDED_STRUCTURAL_SPLIT,
    POLICY_STANDARD_SMOOTH_VARIABLE_CANONICAL,
}


def _assert_final_visible_white_cap_export_contract(result, *, expected_policy: str):
    staged = result.staged_result
    assert staged is not None
    bundle = staged.compatibility_bundle
    cap_plan = staged.cap_plan
    color_ceiling = np.asarray(staged.filler_plan.color_ceiling_mm, dtype=np.float32)
    target = np.asarray(
        bundle.export_maps[WHITE_CAP_FIELD_TARGET_UPPER_SURFACE_KEY],
        dtype=np.float32,
    )
    total_white = np.asarray(bundle.thickness_maps["__white_cap__"], dtype=np.float32)
    boundary = np.asarray(bundle.thickness_maps["__white_boundary_cap__"], dtype=np.float32)
    detail = np.asarray(bundle.thickness_maps["__white_detail_cap__"], dtype=np.float32)
    metadata = bundle.export_metadata
    physical = metadata[PHYSICAL_GEOMETRY_METADATA_KEY]
    target_meta = metadata[WHITE_CAP_FIELD_TARGET_METADATA_KEY]

    assert target_meta["policy"] == expected_policy
    assert target_meta["field_key"] == WHITE_CAP_FIELD_TARGET_UPPER_SURFACE_KEY
    assert target.shape == color_ceiling.shape == total_white.shape
    assert np.all(np.isfinite(target))
    np.testing.assert_allclose(total_white, boundary + detail, atol=1e-7)
    np.testing.assert_allclose(
        cap_plan.final_visible_top_mm,
        color_ceiling + total_white,
        atol=1e-6,
    )
    assert np.all(target + 1e-7 >= color_ceiling)

    required_floor = float(target_meta["required_cover_floor_mm"])
    assert required_floor == pytest.approx(
        quantized_cover_floor_mm(
            float(physical["d_wc_min_mm"]),
            float(physical["layer_height_mm"]),
        )
    )
    cap_thickness = (target - color_ceiling).astype(np.float32, copy=False)
    required_cover_mask = total_white > np.float32(1e-9)
    assert np.any(required_cover_mask)
    assert np.all(cap_thickness[required_cover_mask] + 1e-7 >= required_floor)
    solve_time_budget = np.minimum(
        np.maximum(float(physical["t_max_mm"]) - color_ceiling, 0.0),
        float(target_meta["effective_d_wc_max_mm"]),
    ).astype(np.float32, copy=False)
    assert np.all(cap_thickness <= solve_time_budget + 1e-6)
    boundary_budget = np.minimum(
        np.maximum(float(physical["t_max_mm"]) - color_ceiling, 0.0),
        float(target_meta["effective_boundary_d_wc_max_mm"]),
    ).astype(np.float32, copy=False)
    assert np.all(boundary <= boundary_budget + 1e-6)

    if expected_policy in _FINAL_VISIBLE_TARGET_POLICIES:
        np.testing.assert_allclose(target, cap_plan.final_visible_top_mm, atol=1e-6)
        np.testing.assert_allclose(target, color_ceiling + total_white, atol=1e-6)


def test_generate_stage1_zone_labels_returns_dense_grid_labels():
    image = np.zeros((5, 7, 3), dtype=np.uint8)

    labels = generate_stage1_zone_labels(
        image,
        color_region_target_mm=0.60,
        solver_fine_pitch_mm=0.20,
        cell_mode="grid",
        smooth_boundaries=False,
    )

    assert labels.shape == (5, 7)
    assert labels.dtype == np.int32
    unique = np.unique(labels)
    np.testing.assert_array_equal(unique, np.arange(len(unique), dtype=np.int32))


def test_downsample_rgb_image_preserves_srgb_float_domain_factor_one():
    image = np.array(
        [
            [[0.25, 0.50, 0.75], [0.10, 0.20, 0.30]],
            [[0.90, 0.40, 0.05], [0.00, 1.00, 0.60]],
        ],
        dtype=np.float32,
    )

    downsampled = _downsample_rgb_image(image, 1)

    assert downsampled.dtype == np.float32
    np.testing.assert_allclose(downsampled, image)
    assert float(np.min(downsampled)) >= 0.0
    assert float(np.max(downsampled)) <= 1.0


def test_downsample_rgb_image_preserves_srgb_float_domain_when_averaging():
    image = np.array(
        [
            [[0.20, 0.40, 0.60], [0.30, 0.50, 0.70]],
            [[0.40, 0.60, 0.80], [0.50, 0.70, 0.90]],
        ],
        dtype=np.float32,
    )

    downsampled = _downsample_rgb_image(image, 2)

    assert downsampled.shape == (1, 1, 3)
    assert downsampled.dtype == np.float32
    np.testing.assert_allclose(downsampled[0, 0], [0.35, 0.55, 0.75], atol=1e-6)


def test_downsample_rgb_image_keeps_uint8_domain_when_averaging():
    image = np.array(
        [
            [[10, 20, 30], [20, 30, 40]],
            [[30, 40, 50], [40, 50, 60]],
        ],
        dtype=np.uint8,
    )

    downsampled = _downsample_rgb_image(image, 2)

    assert downsampled.shape == (1, 1, 3)
    assert downsampled.dtype == np.uint8
    np.testing.assert_array_equal(downsampled[0, 0], [25, 35, 45])


def test_stage2_candidate_pixel_scoring_chunks_large_broadcasts():
    targets = np.array(
        [
            [0.0, 0.0, 0.0],
            [0.2, 0.0, 0.0],
            [0.9, 0.1, 0.0],
            [0.2, 0.5, 0.0],
            [0.7, 0.2, 0.2],
        ],
        dtype=np.float32,
    )
    all_oklabs = np.array(
        [
            [[0.0, 0.0, 0.0], [0.1, 0.0, 0.0], [np.nan, np.nan, np.nan]],
            [[0.2, 0.1, 0.0], [0.4, 0.2, 0.0], [0.6, 0.3, 0.0]],
            [[0.8, 0.1, 0.0], [0.8, 0.2, 0.2], [np.nan, np.nan, np.nan]],
            [[0.1, 0.6, 0.0], [0.2, 0.5, 0.1], [0.3, 0.5, 0.2]],
        ],
        dtype=np.float32,
    )
    candidate_ids = np.array([0, 1, 2, 3], dtype=np.int32)

    direct = _score_zone_pixels_against_candidates(
        targets,
        candidate_ids,
        all_oklabs,
        max_broadcast_floats=1_000_000,
    )
    chunked = _score_zone_pixels_against_candidates(
        targets,
        candidate_ids,
        all_oklabs,
        max_broadcast_floats=18,
    )

    np.testing.assert_allclose(chunked, direct, rtol=1e-6, atol=1e-6)


def test_stage2_pixel_stack_scoring_chunks_large_broadcasts():
    targets = np.array(
        [
            [0.0, 0.0, 0.0],
            [0.2, 0.0, 0.0],
            [0.9, 0.1, 0.0],
            [0.2, 0.5, 0.0],
            [0.7, 0.2, 0.2],
        ],
        dtype=np.float32,
    )
    all_oklabs = np.array(
        [
            [[0.0, 0.0, 0.0], [0.1, 0.0, 0.0], [np.nan, np.nan, np.nan]],
            [[0.2, 0.1, 0.0], [0.4, 0.2, 0.0], [0.6, 0.3, 0.0]],
            [[0.8, 0.1, 0.0], [0.8, 0.2, 0.2], [np.nan, np.nan, np.nan]],
            [[0.1, 0.6, 0.0], [0.2, 0.5, 0.1], [0.3, 0.5, 0.2]],
        ],
        dtype=np.float32,
    )
    stack_ids = np.array([0, 1, 2, 3, 1], dtype=np.int32)

    direct = _score_pixels_against_stack_ids(
        targets,
        stack_ids,
        all_oklabs,
        max_broadcast_floats=1_000_000,
    )
    chunked = _score_pixels_against_stack_ids(
        targets,
        stack_ids,
        all_oklabs,
        max_broadcast_floats=6,
    )

    np.testing.assert_allclose(chunked, direct, rtol=1e-6, atol=1e-6)


def test_stage2_boundary_recipe_mutation_borrows_only_improving_neighbor_recipe():
    fine_stack_id_map = np.array(
        [
            [0, 1],
            [0, 1],
        ],
        dtype=np.int32,
    )
    targets = np.array(
        [
            [1.0, 0.0, 0.0],
            [1.0, 0.0, 0.0],
            [1.0, 0.0, 0.0],
            [1.0, 0.0, 0.0],
        ],
        dtype=np.float32,
    )
    all_oklabs = np.array(
        [
            [[0.0, 0.0, 0.0]],
            [[1.0, 0.0, 0.0]],
        ],
        dtype=np.float32,
    )

    result = _apply_stage2_boundary_recipe_mutation(
        fine_stack_id_map=fine_stack_id_map,
        targets=targets,
        all_oklabs=all_oklabs,
        min_gain=0.1,
    )

    np.testing.assert_array_equal(
        result.fine_stack_id_map,
        np.array(
            [
                [1, 1],
                [1, 1],
            ],
            dtype=np.int32,
        ),
    )
    np.testing.assert_array_equal(
        result.mutation_map,
        np.array(
            [
                [1, 0],
                [1, 0],
            ],
            dtype=np.uint8,
        ),
    )
    assert result.candidate_pixels == 4
    assert result.accepted_pixels == 2
    assert result.accepted_components == 1
    assert result.edge_run_mode is True
    assert result.mean_gain > 0.9


def test_stage2_boundary_recipe_mutation_can_reject_short_contact_runs():
    fine_stack_id_map = np.array([[0, 1, 0]], dtype=np.int32)
    targets = np.array(
        [
            [1.0, 0.0, 0.0],
            [1.0, 0.0, 0.0],
            [1.0, 0.0, 0.0],
        ],
        dtype=np.float32,
    )
    all_oklabs = np.array(
        [
            [[0.0, 0.0, 0.0]],
            [[1.0, 0.0, 0.0]],
        ],
        dtype=np.float32,
    )

    result = _apply_stage2_boundary_recipe_mutation(
        fine_stack_id_map=fine_stack_id_map,
        targets=targets,
        all_oklabs=all_oklabs,
        min_gain=0.1,
        min_component_pixels=2,
    )

    np.testing.assert_array_equal(result.fine_stack_id_map, fine_stack_id_map)
    np.testing.assert_array_equal(result.mutation_map, np.zeros((1, 3), dtype=np.uint8))
    assert result.accepted_pixels == 0
    assert result.rejected_small_pixels == 0
    assert result.rejected_small_components == 0
    assert result.rejected_short_run_pixels == 2
    assert result.rejected_short_run_components == 2


def test_stage2_boundary_recipe_mutation_contact_gate_groups_by_borrowed_recipe():
    fine_stack_id_map = np.array([[0, 1, 2, 3]], dtype=np.int32)
    targets = np.array(
        [
            [1.0, 0.0, 0.0],
            [2.0, 0.0, 0.0],
            [2.0, 0.0, 0.0],
            [3.0, 0.0, 0.0],
        ],
        dtype=np.float32,
    )
    all_oklabs = np.array(
        [
            [[0.0, 0.0, 0.0]],
            [[1.0, 0.0, 0.0]],
            [[2.0, 0.0, 0.0]],
            [[3.0, 0.0, 0.0]],
        ],
        dtype=np.float32,
    )

    result = _apply_stage2_boundary_recipe_mutation(
        fine_stack_id_map=fine_stack_id_map,
        targets=targets,
        all_oklabs=all_oklabs,
        min_gain=0.1,
        min_component_pixels=2,
    )

    np.testing.assert_array_equal(result.fine_stack_id_map, fine_stack_id_map)
    np.testing.assert_array_equal(result.mutation_map, np.zeros((1, 4), dtype=np.uint8))
    assert result.rejected_small_pixels == 0
    assert result.rejected_small_components == 0
    assert result.rejected_short_run_pixels == 2
    assert result.rejected_short_run_components == 2


def test_stage2_boundary_recipe_mutation_rejects_weak_edge_run():
    fine_stack_id_map = np.array(
        [
            [0, 0, 0],
            [1, 1, 1],
        ],
        dtype=np.int32,
    )
    targets = np.array(
        [
            [0.55, 0.0, 0.0],
            [0.55, 0.0, 0.0],
            [0.55, 0.0, 0.0],
            [1.0, 0.0, 0.0],
            [1.0, 0.0, 0.0],
            [1.0, 0.0, 0.0],
        ],
        dtype=np.float32,
    )
    all_oklabs = np.array(
        [
            [[0.0, 0.0, 0.0]],
            [[1.0, 0.0, 0.0]],
        ],
        dtype=np.float32,
    )

    result = _apply_stage2_boundary_recipe_mutation(
        fine_stack_id_map=fine_stack_id_map,
        targets=targets,
        all_oklabs=all_oklabs,
        min_gain=0.2,
        min_component_pixels=3,
    )

    np.testing.assert_array_equal(result.fine_stack_id_map, fine_stack_id_map)
    np.testing.assert_array_equal(result.mutation_map, np.zeros((2, 3), dtype=np.uint8))
    assert result.accepted_pixels == 0
    assert result.rejected_weak_pixels == 3
    assert result.rejected_weak_components == 1


def test_stage2_boundary_recipe_mutation_current_de_gate_targets_worst_pixels():
    fine_stack_id_map = np.array([[0, 1, 0]], dtype=np.int32)
    targets = np.array(
        [
            [1.0, 0.0, 0.0],
            [1.0, 0.0, 0.0],
            [0.6, 0.0, 0.0],
        ],
        dtype=np.float32,
    )
    all_oklabs = np.array(
        [
            [[0.0, 0.0, 0.0]],
            [[1.0, 0.0, 0.0]],
        ],
        dtype=np.float32,
    )

    result = _apply_stage2_boundary_recipe_mutation(
        fine_stack_id_map=fine_stack_id_map,
        targets=targets,
        all_oklabs=all_oklabs,
        min_gain=0.1,
        current_de_percentile=75.0,
    )

    np.testing.assert_array_equal(
        result.fine_stack_id_map,
        np.array([[1, 1, 0]], dtype=np.int32),
    )
    np.testing.assert_array_equal(
        result.mutation_map,
        np.array([[1, 0, 0]], dtype=np.uint8),
    )
    assert result.accepted_pixels == 1
    assert result.current_de_eligible_pixels == 1
    assert result.current_de_threshold > 0.6


def test_stage2_boundary_recipe_mutation_edge_run_accepts_printable_boundary_run():
    fine_stack_id_map = np.array(
        [
            [0, 1],
            [0, 1],
            [0, 1],
        ],
        dtype=np.int32,
    )
    targets = np.tile(np.array([[1.0, 0.0, 0.0]], dtype=np.float32), (6, 1))
    all_oklabs = np.array(
        [
            [[0.0, 0.0, 0.0]],
            [[1.0, 0.0, 0.0]],
        ],
        dtype=np.float32,
    )

    result = _apply_stage2_boundary_recipe_mutation(
        fine_stack_id_map=fine_stack_id_map,
        targets=targets,
        all_oklabs=all_oklabs,
        min_gain=0.1,
        min_component_pixels=3,
    )

    np.testing.assert_array_equal(
        result.mutation_map,
        np.array(
            [
                [1, 0],
                [1, 0],
                [1, 0],
            ],
            dtype=np.uint8,
        ),
    )
    assert result.accepted_pixels == 3
    assert result.accepted_boundary_contact_pixels == 3
    assert result.rejected_short_run_components == 0


def test_stage2_boundary_recipe_mutation_edge_run_rejects_short_boundary_run():
    fine_stack_id_map = np.array([[0, 1, 0]], dtype=np.int32)
    targets = np.tile(np.array([[1.0, 0.0, 0.0]], dtype=np.float32), (3, 1))
    all_oklabs = np.array(
        [
            [[0.0, 0.0, 0.0]],
            [[1.0, 0.0, 0.0]],
        ],
        dtype=np.float32,
    )

    result = _apply_stage2_boundary_recipe_mutation(
        fine_stack_id_map=fine_stack_id_map,
        targets=targets,
        all_oklabs=all_oklabs,
        min_gain=0.1,
        min_component_pixels=2,
    )

    np.testing.assert_array_equal(result.fine_stack_id_map, fine_stack_id_map)
    assert result.accepted_pixels == 0
    assert result.rejected_short_run_pixels == 2
    assert result.rejected_short_run_components == 2


def test_stage2_boundary_recipe_mutation_max_passes_one_matches_edge_run_golden():
    fine_stack_id_map = np.array(
        [
            [0, 1, 0],
            [0, 1, 0],
            [0, 1, 0],
        ],
        dtype=np.int32,
    )
    targets = np.tile(np.array([[1.0, 0.0, 0.0]], dtype=np.float32), (9, 1))
    all_oklabs = np.array(
        [
            [[0.0, 0.0, 0.0]],
            [[1.0, 0.0, 0.0]],
        ],
        dtype=np.float32,
    )

    result, passes_run, pass_pixels = _iterate_stage2_boundary_recipe_mutation(
        fine_stack_id_map=fine_stack_id_map,
        targets=targets,
        all_oklabs=all_oklabs,
        min_gain=0.1,
        min_component_pixels=3,
        max_passes=1,
    )

    np.testing.assert_array_equal(
        result.fine_stack_id_map,
        np.ones((3, 3), dtype=np.int32),
    )
    np.testing.assert_array_equal(
        result.mutation_map,
        np.array([[1, 0, 1], [1, 0, 1], [1, 0, 1]], dtype=np.uint8),
    )
    assert passes_run == 1
    assert pass_pixels == [6]
    assert result.candidate_pixels == 9
    assert result.accepted_pixels == 6
    assert result.accepted_components == 2
    assert result.rejected_small_pixels == 0
    assert result.rejected_small_components == 0
    assert result.edge_run_mode is True
    assert result.accepted_boundary_contact_pixels == 6
    assert result.rejected_short_run_pixels == 0
    assert result.rejected_short_run_components == 0
    assert result.current_de_threshold == pytest.approx(0.0)
    assert result.current_de_eligible_pixels == 9
    assert result.mean_gain == pytest.approx(1.0)
    assert result.p95_gain == pytest.approx(1.0)


def test_stage2_boundary_recipe_mutation_iterates_boundary_walk_to_convergence():
    fine_stack_id_map = np.array(
        [
            [0, 0, 0, 0, 1],
            [0, 0, 0, 0, 1],
            [0, 0, 0, 0, 1],
        ],
        dtype=np.int32,
    )
    target_grid = np.zeros((3, 5, 3), dtype=np.float32)
    target_grid[:, 1:, 0] = 1.0
    targets = target_grid.reshape(-1, 3)
    all_oklabs = np.array(
        [
            [[0.0, 0.0, 0.0]],
            [[1.0, 0.0, 0.0]],
        ],
        dtype=np.float32,
    )

    result, passes_run, pass_pixels = _iterate_stage2_boundary_recipe_mutation(
        fine_stack_id_map=fine_stack_id_map,
        targets=targets,
        all_oklabs=all_oklabs,
        min_gain=0.1,
        min_component_pixels=3,
        max_passes=8,
    )

    expected = np.array(
        [
            [0, 1, 1, 1, 1],
            [0, 1, 1, 1, 1],
            [0, 1, 1, 1, 1],
        ],
        dtype=np.int32,
    )
    np.testing.assert_array_equal(result.fine_stack_id_map, expected)
    assert passes_run >= 3
    assert pass_pixels == [3, 3, 3, 0]
    assert result.accepted_pixels == 9
    assert result.candidate_pixels >= result.accepted_pixels


def test_stage2_boundary_recipe_mutation_stops_after_zero_acceptance_pass():
    fine_stack_id_map = np.array([[0, 0], [0, 0]], dtype=np.int32)
    targets = np.zeros((4, 3), dtype=np.float32)
    all_oklabs = np.array(
        [
            [[0.0, 0.0, 0.0]],
            [[1.0, 0.0, 0.0]],
        ],
        dtype=np.float32,
    )

    result, passes_run, pass_pixels = _iterate_stage2_boundary_recipe_mutation(
        fine_stack_id_map=fine_stack_id_map,
        targets=targets,
        all_oklabs=all_oklabs,
        min_gain=0.1,
        min_component_pixels=1,
        max_passes=8,
    )

    np.testing.assert_array_equal(result.fine_stack_id_map, fine_stack_id_map)
    assert passes_run == 1
    assert pass_pixels == [0]
    assert result.accepted_pixels == 0


def test_stage2_boundary_recipe_mutation_repeated_single_pass_scores_monotonic():
    fine_stack_id_map = np.array(
        [
            [0, 0, 0, 1],
            [0, 0, 0, 1],
            [0, 0, 0, 1],
        ],
        dtype=np.int32,
    )
    targets = np.tile(np.array([[1.0, 0.0, 0.0]], dtype=np.float32), (12, 1))
    all_oklabs = np.array(
        [
            [[0.0, 0.0, 0.0]],
            [[1.0, 0.0, 0.0]],
        ],
        dtype=np.float32,
    )
    scores = []
    current = fine_stack_id_map
    for _ in range(4):
        scores.append(
            float(
                np.sum(
                    _score_pixels_against_stack_ids(
                        targets,
                        current.reshape(-1),
                        all_oklabs,
                    )
                )
            )
        )
        current = _apply_stage2_boundary_recipe_mutation(
            fine_stack_id_map=current,
            targets=targets,
            all_oklabs=all_oklabs,
            min_gain=0.1,
            min_component_pixels=3,
        ).fine_stack_id_map

    assert scores == sorted(scores, reverse=True)
    assert scores[-1] < scores[0]


def test_stage2_boundary_recipe_mutation_contact_semantics_require_donor_run():
    all_oklabs = np.array(
        [
            [[0.0, 0.0, 0.0]],
            [[1.0, 0.0, 0.0]],
        ],
        dtype=np.float32,
    )
    rejected_map = np.array(
        [
            [0, 0, 0],
            [0, 1, 0],
            [0, 0, 0],
        ],
        dtype=np.int32,
    )
    accepted_map = np.array(
        [
            [1, 1, 1],
            [0, 0, 0],
        ],
        dtype=np.int32,
    )
    targets = np.tile(np.array([[1.0, 0.0, 0.0]], dtype=np.float32), (9, 1))
    accepted_targets = np.tile(np.array([[1.0, 0.0, 0.0]], dtype=np.float32), (6, 1))

    rejected = _apply_stage2_boundary_recipe_mutation(
        fine_stack_id_map=rejected_map,
        targets=targets,
        all_oklabs=all_oklabs,
        min_gain=0.1,
        min_component_pixels=3,
    )
    accepted = _apply_stage2_boundary_recipe_mutation(
        fine_stack_id_map=accepted_map,
        targets=accepted_targets,
        all_oklabs=all_oklabs,
        min_gain=0.1,
        min_component_pixels=3,
    )

    assert rejected.accepted_pixels == 0
    assert rejected.rejected_short_run_pixels == 4
    assert accepted.accepted_pixels == 3
    assert accepted.accepted_boundary_contact_pixels == 3


def test_effective_color_region_target_can_follow_printability_limits():
    cfg = SolveConfig(
        palette=["a", "b"],
        white_base="white",
        color_region_target_mm=0.60,
        color_region_target_from_printability=True,
        printability_minimum_extrusion_width_mm=0.40,
        printability_minimum_line_length_mm=0.50,
    )

    assert _effective_color_region_target_mm(cfg) == 0.80


def test_effective_color_region_target_preserves_larger_explicit_target():
    cfg = SolveConfig(
        palette=["a", "b"],
        white_base="white",
        color_region_target_mm=1.00,
        color_region_target_from_printability=True,
        printability_minimum_extrusion_width_mm=0.40,
        printability_minimum_line_length_mm=0.50,
    )

    assert _effective_color_region_target_mm(cfg) == 1.00


def test_stage1_zone_summary_fast_path_matches_finite_singleton_reduction() -> None:
    targets = np.array(
        [
            [0.25, -0.10, 0.05],
            [0.75, 0.20, -0.30],
            [0.50, 0.10, 0.15],
        ],
        dtype=np.float32,
    )
    memberships = (
        np.array([0], dtype=np.int32),
        np.array([1], dtype=np.int32),
        np.array([0, 2], dtype=np.int32),
        np.zeros(0, dtype=np.int32),
    )

    means, variances = _summarize_zone_targets(memberships, targets)
    expected_means = np.zeros_like(means)
    expected_variances = np.zeros_like(variances)
    for zone_id, indices in enumerate(memberships):
        if indices.size:
            expected_means[zone_id] = np.mean(targets[indices], axis=0).astype(np.float32)
            expected_variances[zone_id] = np.var(targets[indices], axis=0).astype(np.float32)

    np.testing.assert_array_equal(means, expected_means)
    np.testing.assert_array_equal(variances, expected_variances)


def test_stage1_zone_summary_retains_nonfinite_singleton_variance_semantics() -> None:
    targets = np.array(
        [[np.nan, 0.25, -0.50], [np.inf, -0.25, 0.50]],
        dtype=np.float32,
    )
    memberships = (
        np.array([0], dtype=np.int32),
        np.array([1], dtype=np.int32),
    )

    with np.errstate(invalid="ignore"):
        means, variances = _summarize_zone_targets(memberships, targets)
        expected_means = np.vstack(
            [np.mean(targets[indices], axis=0) for indices in memberships]
        ).astype(np.float32)
        expected_variances = np.vstack(
            [np.var(targets[indices], axis=0) for indices in memberships]
        ).astype(np.float32)

    np.testing.assert_array_equal(means, expected_means)
    np.testing.assert_array_equal(variances, expected_variances)


def test_stage4_single_count_lookup_is_bitwise_equivalent_to_grouped_path() -> None:
    rng = np.random.default_rng(20260713)
    lookup = {
        count: rng.normal(size=3).astype(np.float32)
        for count in range(2, 9)
    }

    for shape in ((1,), (1, 1)):
        for count in range(1, 10):
            counts = np.full(shape, count, dtype=np.int32)
            target = rng.normal(size=shape + (3,)).astype(np.float32)
            expected = np.full(counts.shape, np.inf, dtype=np.float32)
            for unique_count in np.unique(counts).tolist():
                mask = counts == int(unique_count)
                row = lookup.get(int(unique_count))
                if row is None:
                    continue
                delta = target[mask] - row.reshape(1, 3)
                expected[mask] = np.sqrt(np.sum(delta * delta, axis=1)).astype(
                    np.float32,
                    copy=False,
                )

            actual = _stage4_lookup_oklab_by_count(lookup, counts, target)
            np.testing.assert_array_equal(actual, expected)


def test_stage2_boundary_optimizer_can_override_independent_zone_choices():
    candidate_sets = (
        _ZoneCandidateSet(
            candidate_ids=np.array([10, 11], dtype=np.int32),
            local_scores=np.array([0.011, 0.010], dtype=np.float32),
            total_thickness_mm=np.array([0.16, 0.32], dtype=np.float32),
        ),
        _ZoneCandidateSet(
            candidate_ids=np.array([10, 11], dtype=np.int32),
            local_scores=np.array([0.010, 0.011], dtype=np.float32),
            total_thickness_mm=np.array([0.16, 0.32], dtype=np.float32),
        ),
    )

    result = _optimize_zone_recipe_labels(
        candidate_sets=candidate_sets,
        adjacency_edges=((0, 1),),
        adjacency_edge_lengths_px=np.array([10], dtype=np.int32),
    )

    assert tuple(result.initial_selected_stack_ids) == (1, 0)
    assert result.changed_zone_count >= 1
    assert result.boundary_step_mean_before_mm > result.boundary_step_mean_after_mm
    assert result.selected_stack_ids[0] == result.selected_stack_ids[1]

    zone_plan = LateralZonePlan(
        planning_shape=(1, 2),
        planning_pitch_mm=0.20,
        coarse_to_fine_scale=1,
        zone_label_map=np.array([[0, 1]], dtype=np.int32),
        zone_flat_indices=(
            np.array([0], dtype=np.int32),
            np.array([1], dtype=np.int32),
        ),
        adjacency_edges=((0, 1),),
        adjacency_edge_lengths_px=np.array([10], dtype=np.int32),
        zone_pixel_counts=np.array([1, 1], dtype=np.int32),
        target_oklab_mean_by_zone=np.zeros((2, 3), dtype=np.float32),
        target_oklab_var_by_zone=np.array([[0.04, 0.0, 0.0], [0.01, 0.0, 0.0]], dtype=np.float32),
    )
    summary = _build_stage2_objective_summary(
        zone_count=zone_plan.zone_count,
        zone_pixel_counts=zone_plan.zone_pixel_counts,
        target_oklab_var_by_zone=zone_plan.target_oklab_var_by_zone,
        adjacency_edges=zone_plan.adjacency_edges,
        adjacency_edge_lengths_px=zone_plan.adjacency_edge_lengths_px,
        candidate_sets=candidate_sets,
        optimization=result,
        continuity_weight=0.12,
        retaining_wall_weight=0.02,
    )

    assert summary.changed_zone_count >= 1
    assert summary.boundary_step_mean_before_mm > summary.boundary_step_mean_after_mm
    assert summary.boundary_step_p95_before_mm >= summary.boundary_step_p95_after_mm
    assert summary.intra_zone_target_variance_mean > 0.0
    assert len(summary.changed_zones) >= 1
    assert summary.changed_zones[0].boundary_cost_before > summary.changed_zones[0].boundary_cost_after
    assert len(summary.worst_edges) == 1
    assert summary.worst_edges[0].zone_a == 0
    assert summary.worst_edges[0].zone_b == 1
    assert summary.worst_edges[0].step_before_mm > summary.worst_edges[0].step_after_mm


def test_stage2_objective_summary_matches_full_breakdown_reference() -> None:
    candidate_sets = (
        _ZoneCandidateSet(
            candidate_ids=np.array([10, 11], dtype=np.int32),
            local_scores=np.array([0.04, 0.08], dtype=np.float32),
            total_thickness_mm=np.array([0.16, 0.32], dtype=np.float32),
        ),
        _ZoneCandidateSet(
            candidate_ids=np.array([20, 21], dtype=np.int32),
            local_scores=np.array([0.03, 0.01], dtype=np.float32),
            total_thickness_mm=np.array([0.24, 0.40], dtype=np.float32),
        ),
        _ZoneCandidateSet(
            candidate_ids=np.array([30, 31], dtype=np.int32),
            local_scores=np.array([0.02, 0.05], dtype=np.float32),
            total_thickness_mm=np.array([0.08, 0.24], dtype=np.float32),
        ),
    )
    initial = np.array([0, 0, 0], dtype=np.int32)
    selected = np.array([0, 1, 0], dtype=np.int32)
    adjacency = ((0, 1), (1, 2))
    edge_lengths = np.array([3, 5], dtype=np.int32)
    local_weights = np.array([0.75, 1.25, 1.50], dtype=np.float32)
    target_variances = np.array(
        [[0.01, 0.02, 0.03], [0.04, 0.01, 0.02], [0.03, 0.02, 0.01]],
        dtype=np.float32,
    )
    optimization = SimpleNamespace(
        local_seed_selected_stack_ids=initial.copy(),
        initial_selected_stack_ids=initial,
        selected_stack_ids=selected,
        boundary_step_mean_before_mm=0.12,
        boundary_step_mean_after_mm=0.08,
        changed_zone_count=1,
    )

    summary = _build_stage2_objective_summary(
        zone_count=3,
        zone_pixel_counts=np.array([1, 2, 3], dtype=np.int32),
        target_oklab_var_by_zone=target_variances,
        adjacency_edges=adjacency,
        adjacency_edge_lengths_px=edge_lengths,
        candidate_sets=candidate_sets,
        optimization=optimization,
        continuity_weight=0.12,
        retaining_wall_weight=0.02,
        local_cost_weights=local_weights,
    )

    neighbors = staged_runner._build_zone_neighbors(3, adjacency, edge_lengths)
    before = []
    after = []
    for zone_id in range(3):
        before.append(
            staged_runner._zone_objective_breakdown(
                zone_id=zone_id,
                candidate_index=int(initial[zone_id]),
                selected_stack_ids=initial,
                candidate_sets=candidate_sets,
                neighbors=neighbors,
                continuity_weight=0.12,
                retaining_wall_weight=0.02,
                local_cost_weight=float(local_weights[zone_id]),
            )
        )
        after.append(
            staged_runner._zone_objective_breakdown(
                zone_id=zone_id,
                candidate_index=int(selected[zone_id]),
                selected_stack_ids=selected,
                candidate_sets=candidate_sets,
                neighbors=neighbors,
                continuity_weight=0.12,
                retaining_wall_weight=0.02,
                local_cost_weight=float(local_weights[zone_id]),
            )
        )

    assert summary.local_cost_mean_before == float(np.mean([item.local_cost for item in before]))
    assert summary.local_cost_mean_after == float(np.mean([item.local_cost for item in after]))
    assert summary.intra_zone_target_variance_mean == float(
        np.mean([float(np.sqrt(np.sum(row))) for row in target_variances])
    )
    assert len(summary.changed_zones) == 1
    changed = summary.changed_zones[0]
    assert changed.zone_id == 1
    assert changed.local_cost_before == before[1].local_cost
    assert changed.local_cost_after == after[1].local_cost
    assert changed.boundary_cost_before == before[1].boundary_cost
    assert changed.boundary_cost_after == after[1].boundary_cost
    assert changed.retaining_cost_before == before[1].retaining_cost
    assert changed.retaining_cost_after == after[1].retaining_cost
    assert changed.total_cost_before == before[1].total_cost
    assert changed.total_cost_after == after[1].total_cost


def test_coordinate_descent_prepared_inputs_match_legacy_loop_exactly() -> None:
    def legacy_coord_descent(
        *,
        selected_stack_ids,
        candidate_sets,
        neighbors,
        continuity_weight,
        retaining_wall_weight,
        max_passes,
        local_cost_weights=None,
    ):
        selected = selected_stack_ids.astype(np.int32, copy=True)
        selected_totals = staged_runner._selected_total_thicknesses(
            selected,
            candidate_sets,
        )
        retaining_penalties = staged_runner._candidate_retaining_penalties(
            candidate_sets
        )
        local_weights = (
            np.asarray(local_cost_weights, dtype=np.float32)
            if local_cost_weights is not None
            else np.ones(len(candidate_sets), dtype=np.float32)
        )
        pass_count = 0
        eval_count = 0
        for _ in range(max_passes):
            pass_count += 1
            changed = False
            for zone_id, candidate_set in enumerate(candidate_sets):
                if candidate_set.local_scores.size <= 1:
                    continue
                candidate_totals = candidate_set.total_thickness_mm.astype(
                    np.float64,
                    copy=False,
                )
                local_costs = (
                    candidate_set.local_scores.astype(np.float64, copy=False)
                    * float(local_weights[zone_id])
                )
                retaining_costs = retaining_penalties[zone_id].astype(
                    np.float64,
                    copy=False,
                )
                eval_count += int(candidate_totals.size)
                if neighbors[zone_id]:
                    neighbor_zone_ids = np.fromiter(
                        (
                            neighbor_zone_id
                            for neighbor_zone_id, _ in neighbors[zone_id]
                        ),
                        dtype=np.int32,
                        count=len(neighbors[zone_id]),
                    )
                    neighbor_weights = np.fromiter(
                        (edge_weight for _, edge_weight in neighbors[zone_id]),
                        dtype=np.float64,
                        count=len(neighbors[zone_id]),
                    )
                    neighbor_totals = selected_totals[neighbor_zone_ids].astype(
                        np.float64,
                        copy=False,
                    )
                    steps_sq = np.square(
                        candidate_totals[:, None] - neighbor_totals[None, :],
                        dtype=np.float64,
                    )
                    boundary_costs = np.sum(
                        steps_sq * neighbor_weights[None, :],
                        axis=1,
                    )
                    weight_sum = float(np.sum(neighbor_weights))
                    if weight_sum > 0.0:
                        boundary_costs /= weight_sum
                else:
                    boundary_costs = np.zeros(
                        candidate_totals.shape[0],
                        dtype=np.float64,
                    )
                total_costs = (
                    local_costs
                    + float(retaining_wall_weight) * retaining_costs
                    + float(continuity_weight) * boundary_costs
                )
                best_index = int(np.argmin(total_costs))
                if best_index != int(selected[zone_id]):
                    selected[zone_id] = best_index
                    selected_totals[zone_id] = float(
                        candidate_set.total_thickness_mm[best_index]
                    )
                    changed = True
            if not changed:
                break
        return selected, selected_totals, pass_count, eval_count

    for seed in range(8):
        rng = np.random.default_rng(20260713 + seed)
        zone_count = 12
        candidate_sets = []
        selected = np.zeros(zone_count, dtype=np.int32)
        for zone_id in range(zone_count):
            candidate_count = int(rng.integers(0, 5))
            local_scores = rng.uniform(0.0, 0.2, size=candidate_count).astype(
                np.float32
            )
            totals = rng.integers(1, 8, size=candidate_count).astype(np.float32)
            totals *= np.float32(0.08)
            candidate_sets.append(
                _ZoneCandidateSet(
                    candidate_ids=np.arange(candidate_count, dtype=np.int32),
                    local_scores=local_scores,
                    total_thickness_mm=totals,
                )
            )
            if candidate_count:
                selected[zone_id] = int(rng.integers(0, candidate_count))
        candidate_sets_tuple = tuple(candidate_sets)
        neighbors = [[] for _ in range(zone_count)]
        for zone_id in range(zone_count - 1):
            if zone_id in (4, 8):
                continue
            weight = float(rng.integers(0, 7))
            neighbors[zone_id].append((zone_id + 1, weight))
            neighbors[zone_id + 1].append((zone_id, weight))
        local_weights = rng.uniform(0.5, 2.0, size=zone_count).astype(np.float32)
        kwargs = dict(
            selected_stack_ids=selected,
            candidate_sets=candidate_sets_tuple,
            neighbors=neighbors,
            continuity_weight=0.12,
            retaining_wall_weight=0.02,
            max_passes=4,
            local_cost_weights=local_weights,
        )

        expected = legacy_coord_descent(**kwargs)
        actual = _run_coord_descent(**kwargs)

        np.testing.assert_array_equal(actual[0], expected[0])
        np.testing.assert_array_equal(actual[1], expected[1])
        assert actual[2:] == expected[2:]
        for candidate_set in candidate_sets_tuple:
            assert candidate_set.local_scores.flags.writeable
            assert candidate_set.total_thickness_mm.flags.writeable


def test_coordinate_descent_preserves_first_candidate_tie_break() -> None:
    candidate_sets = (
        _ZoneCandidateSet(
            candidate_ids=np.array([10, 11], dtype=np.int32),
            local_scores=np.array([0.05, 0.05], dtype=np.float32),
            total_thickness_mm=np.array([0.24, 0.24], dtype=np.float32),
        ),
    )

    selected, totals, pass_count, eval_count = _run_coord_descent(
        selected_stack_ids=np.array([1], dtype=np.int32),
        candidate_sets=candidate_sets,
        neighbors=[[]],
        continuity_weight=0.12,
        retaining_wall_weight=0.02,
        max_passes=4,
    )

    np.testing.assert_array_equal(selected, np.array([0], dtype=np.int32))
    np.testing.assert_array_equal(totals, np.array([0.24], dtype=np.float32))
    assert pass_count == 2
    assert eval_count == 4


def test_stage2_candidate_frontiers_preserve_local_winner_and_thickness_anchors():
    candidate_sets = (
        _ZoneCandidateSet(
            candidate_ids=np.array([10, 11, 12, 13, 14], dtype=np.int32),
            local_scores=np.array([0.50, 0.20, 0.10, 0.40, 0.30], dtype=np.float32),
            total_thickness_mm=np.array([0.08, 0.16, 0.24, 0.32, 0.40], dtype=np.float32),
        ),
    )

    pruned, neighbor_hits = _prune_zone_candidate_frontiers(candidate_sets, frontier_size=3)

    assert len(pruned) == 1
    assert neighbor_hits == 0
    np.testing.assert_array_equal(pruned[0].candidate_ids, np.array([12, 10, 14], dtype=np.int32))
    np.testing.assert_allclose(pruned[0].local_scores, np.array([0.10, 0.50, 0.30], dtype=np.float32))
    np.testing.assert_allclose(
        [float(np.min(pruned[0].total_thickness_mm)), float(np.max(pruned[0].total_thickness_mm))],
        [0.08, 0.40],
    )


def test_stage2_candidate_frontiers_keep_seam_useful_extremes_alive():
    candidate_sets = (
        _ZoneCandidateSet(
            candidate_ids=np.array([10, 11, 12, 13, 14], dtype=np.int32),
            local_scores=np.array([0.10, 0.11, 0.12, 0.13, 0.90], dtype=np.float32),
            total_thickness_mm=np.array([0.20, 0.21, 0.22, 0.23, 0.40], dtype=np.float32),
        ),
    )

    pruned, neighbor_hits = _prune_zone_candidate_frontiers(candidate_sets, frontier_size=3)

    assert len(pruned) == 1
    assert neighbor_hits == 0
    assert 14 in set(pruned[0].candidate_ids.tolist())
    assert 10 in set(pruned[0].candidate_ids.tolist())
    assert pruned[0].candidate_ids.size == 3


def test_stage2_optical_frontier_rescue_restores_strong_pruned_candidates():
    preprune = (
        _ZoneCandidateSet(
            candidate_ids=np.array([10, 11, 12, 13, 14, 15], dtype=np.int32),
            local_scores=np.array([0.10, 0.11, 0.12, 0.13, 0.14, 0.50], dtype=np.float32),
            total_thickness_mm=np.array([0.20, 0.21, 0.22, 0.23, 0.24, 0.60], dtype=np.float32),
        ),
    )
    pruned = (
        _ZoneCandidateSet(
            candidate_ids=np.array([10, 15], dtype=np.int32),
            local_scores=np.array([0.10, 0.50], dtype=np.float32),
            total_thickness_mm=np.array([0.20, 0.60], dtype=np.float32),
        ),
    )

    rescued, zone_hits, candidate_count, pressure_candidate_count = _rescue_stage2_optical_frontier_candidates(
        preprune_candidate_sets=preprune,
        pruned_candidate_sets=pruned,
        frontier_size=2,
        max_extra_candidates=2,
        rank_budget=4,
        min_score_gap=0.01,
    )

    assert zone_hits == 1
    assert candidate_count == 2
    assert pressure_candidate_count == 0
    np.testing.assert_array_equal(
        rescued[0].candidate_ids,
        np.array([10, 15, 11, 12], dtype=np.int32),
    )
    np.testing.assert_allclose(
        rescued[0].local_scores,
        np.array([0.10, 0.50, 0.11, 0.12], dtype=np.float32),
    )


def test_stage2_optical_frontier_rescue_leaves_weak_candidates_pruned():
    preprune = (
        _ZoneCandidateSet(
            candidate_ids=np.array([10, 11, 12], dtype=np.int32),
            local_scores=np.array([0.10, 0.499, 0.50], dtype=np.float32),
            total_thickness_mm=np.array([0.20, 0.21, 0.60], dtype=np.float32),
        ),
    )
    pruned = (
        _ZoneCandidateSet(
            candidate_ids=np.array([10, 12], dtype=np.int32),
            local_scores=np.array([0.10, 0.50], dtype=np.float32),
            total_thickness_mm=np.array([0.20, 0.60], dtype=np.float32),
        ),
    )

    rescued, zone_hits, candidate_count, pressure_candidate_count = _rescue_stage2_optical_frontier_candidates(
        preprune_candidate_sets=preprune,
        pruned_candidate_sets=pruned,
        frontier_size=2,
        max_extra_candidates=2,
        rank_budget=3,
        min_score_gap=0.01,
    )

    assert zone_hits == 0
    assert candidate_count == 0
    assert pressure_candidate_count == 0
    assert rescued[0] is pruned[0]


def test_stage2_optical_frontier_rescue_can_use_pressure_pixels():
    all_oklabs = np.full((3, 1, 3), np.nan, dtype=np.float32)
    all_oklabs[0, 0] = np.array([0.0, 0.0, 0.0], dtype=np.float32)
    all_oklabs[1, 0] = np.array([0.4, 0.0, 0.0], dtype=np.float32)
    all_oklabs[2, 0] = np.array([1.0, 0.0, 0.0], dtype=np.float32)
    targets = np.tile(np.array([[1.0, 0.0, 0.0]], dtype=np.float32), (4, 1))
    preprune = (
        _ZoneCandidateSet(
            candidate_ids=np.array([0, 1, 2], dtype=np.int32),
            local_scores=np.array([1.0, 0.6, 0.0], dtype=np.float32),
            total_thickness_mm=np.array([0.10, 0.20, 0.30], dtype=np.float32),
        ),
    )
    pruned = (
        _ZoneCandidateSet(
            candidate_ids=np.array([0, 1], dtype=np.int32),
            local_scores=np.array([1.0, 0.6], dtype=np.float32),
            total_thickness_mm=np.array([0.10, 0.20], dtype=np.float32),
        ),
    )

    rescued, zone_hits, candidate_count, pressure_candidate_count = (
        _rescue_stage2_optical_frontier_candidates(
            preprune_candidate_sets=preprune,
            pruned_candidate_sets=pruned,
            zone_flat_indices=(np.array([0, 1, 2, 3], dtype=np.int32),),
            targets=targets,
            all_oklabs=all_oklabs,
            frontier_size=2,
            max_extra_candidates=1,
        )
    )

    assert zone_hits == 1
    assert candidate_count == 1
    assert pressure_candidate_count == 1
    np.testing.assert_array_equal(rescued[0].candidate_ids, np.array([0, 1, 2], dtype=np.int32))


def test_stage2_neighbor_seed_augmentation_adds_strong_neighbor_local_best():
    zone_plan = LateralZonePlan(
        planning_shape=(1, 4),
        planning_pitch_mm=0.20,
        coarse_to_fine_scale=1,
        zone_label_map=np.array([[0, 0, 1, 1]], dtype=np.int32),
        zone_flat_indices=(
            np.array([0, 1], dtype=np.int32),
            np.array([2, 3], dtype=np.int32),
        ),
        adjacency_edges=((0, 1),),
        adjacency_edge_lengths_px=np.array([10], dtype=np.int32),
        zone_pixel_counts=np.array([2, 2], dtype=np.int32),
        target_oklab_mean_by_zone=np.zeros((2, 3), dtype=np.float32),
        target_oklab_var_by_zone=np.array([[0.01, 0.0, 0.0], [0.01, 0.0, 0.0]], dtype=np.float32),
    )
    candidate_sets = (
        _ZoneCandidateSet(
            candidate_ids=np.array([0, 1], dtype=np.int32),
            local_scores=np.array([0.10, 0.11], dtype=np.float32),
            total_thickness_mm=np.array([0.10, 0.20], dtype=np.float32),
        ),
        _ZoneCandidateSet(
            candidate_ids=np.array([2, 3], dtype=np.int32),
            local_scores=np.array([0.10, 0.11], dtype=np.float32),
            total_thickness_mm=np.array([0.30, 0.40], dtype=np.float32),
        ),
    )
    targets = np.zeros((4, 3), dtype=np.float32)
    all_oklabs = np.zeros((4, 1, 3), dtype=np.float32)
    unique_stack_dicts = {
        0: {"a": 0.10},
        1: {"a": 0.20},
        2: {"a": 0.30},
        3: {"a": 0.40},
    }

    augmented, zone_hits, candidate_additions = _augment_zone_candidates_with_neighbor_local_bests(
        zone_count=zone_plan.zone_count,
        zone_flat_indices=zone_plan.zone_flat_indices,
        target_oklab_var_by_zone=zone_plan.target_oklab_var_by_zone,
        adjacency_edges=zone_plan.adjacency_edges,
        adjacency_edge_lengths_px=zone_plan.adjacency_edge_lengths_px,
        candidate_sets=candidate_sets,
        targets=targets,
        unique_stack_dicts=unique_stack_dicts,
        all_oklabs=all_oklabs,
    )

    assert zone_hits == 2
    assert candidate_additions == 2
    assert 2 in set(augmented[0].candidate_ids.tolist())
    assert 0 in set(augmented[1].candidate_ids.tolist())


def test_stage2_candidate_frontiers_preserve_neighbor_matching_candidate():
    candidate_sets = (
        _ZoneCandidateSet(
            candidate_ids=np.array([90], dtype=np.int32),
            local_scores=np.array([0.0], dtype=np.float32),
            total_thickness_mm=np.array([0.30], dtype=np.float32),
        ),
        _ZoneCandidateSet(
            candidate_ids=np.array([10, 11, 12, 13, 14], dtype=np.int32),
            local_scores=np.array([0.10, 0.11, 0.12, 0.13, 0.14], dtype=np.float32),
            total_thickness_mm=np.array([0.10, 0.20, 0.30, 0.40, 0.50], dtype=np.float32),
        ),
    )

    pruned, neighbor_hits = _prune_zone_candidate_frontiers(
        candidate_sets,
        adjacency_edges=((0, 1),),
        adjacency_edge_lengths_px=np.array([10], dtype=np.int32),
        frontier_size=3,
    )

    assert len(pruned) == 2
    assert neighbor_hits == 1
    assert 12 in set(pruned[1].candidate_ids.tolist())
    assert 10 in set(pruned[1].candidate_ids.tolist())
    assert pruned[1].candidate_ids.size == 3


def test_stage2_beam_seed_can_override_local_argmins():
    candidate_sets = (
        _ZoneCandidateSet(
            candidate_ids=np.array([10, 11], dtype=np.int32),
            local_scores=np.array([0.011, 0.010], dtype=np.float32),
            total_thickness_mm=np.array([0.16, 0.32], dtype=np.float32),
        ),
        _ZoneCandidateSet(
            candidate_ids=np.array([10, 11], dtype=np.int32),
            local_scores=np.array([0.010, 0.011], dtype=np.float32),
            total_thickness_mm=np.array([0.16, 0.32], dtype=np.float32),
        ),
        _ZoneCandidateSet(
            candidate_ids=np.array([10, 11], dtype=np.int32),
            local_scores=np.array([0.011, 0.010], dtype=np.float32),
            total_thickness_mm=np.array([0.16, 0.32], dtype=np.float32),
        ),
    )
    zone_plan = LateralZonePlan(
        planning_shape=(1, 3),
        planning_pitch_mm=0.20,
        coarse_to_fine_scale=1,
        zone_label_map=np.array([[0, 1, 2]], dtype=np.int32),
        zone_flat_indices=(
            np.array([0], dtype=np.int32),
            np.array([1], dtype=np.int32),
            np.array([2], dtype=np.int32),
        ),
        adjacency_edges=((0, 1), (1, 2)),
        adjacency_edge_lengths_px=np.array([10, 10], dtype=np.int32),
        zone_pixel_counts=np.array([1, 1, 1], dtype=np.int32),
        target_oklab_mean_by_zone=np.zeros((3, 3), dtype=np.float32),
        target_oklab_var_by_zone=np.array(
            [[0.04, 0.0, 0.0], [0.04, 0.0, 0.0], [0.04, 0.0, 0.0]],
            dtype=np.float32,
        ),
    )

    local_seed = np.array([1, 0, 1], dtype=np.int32)
    beam_seed = _seed_zone_recipe_labels_with_beam(
        zone_count=zone_plan.zone_count,
        adjacency_edges=zone_plan.adjacency_edges,
        adjacency_edge_lengths_px=zone_plan.adjacency_edge_lengths_px,
        zone_pixel_counts=zone_plan.zone_pixel_counts,
        target_oklab_var_by_zone=zone_plan.target_oklab_var_by_zone,
        candidate_sets=candidate_sets,
        beam_width=6,
    )

    assert tuple(local_seed) == (1, 0, 1)
    assert tuple(beam_seed.selected_stack_ids) != tuple(local_seed)
    assert beam_seed.selected_stack_ids[0] == beam_seed.selected_stack_ids[1] == beam_seed.selected_stack_ids[2]
    assert beam_seed.expansion_count >= 1
    assert beam_seed.max_beam_size >= 1


def test_stage2_pair_repair_can_escape_single_zone_local_minimum():
    candidate_sets = (
        _ZoneCandidateSet(
            candidate_ids=np.array([10], dtype=np.int32),
            local_scores=np.array([0.0], dtype=np.float32),
            total_thickness_mm=np.array([0.0], dtype=np.float32),
        ),
        _ZoneCandidateSet(
            candidate_ids=np.array([10, 11], dtype=np.int32),
            local_scores=np.array([0.035, 0.0], dtype=np.float32),
            total_thickness_mm=np.array([0.0, 1.0], dtype=np.float32),
        ),
        _ZoneCandidateSet(
            candidate_ids=np.array([10, 11], dtype=np.int32),
            local_scores=np.array([0.035, 0.0], dtype=np.float32),
            total_thickness_mm=np.array([0.0, 1.0], dtype=np.float32),
        ),
        _ZoneCandidateSet(
            candidate_ids=np.array([10], dtype=np.int32),
            local_scores=np.array([0.0], dtype=np.float32),
            total_thickness_mm=np.array([0.0], dtype=np.float32),
        ),
    )

    result = _optimize_zone_recipe_labels(
        candidate_sets=candidate_sets,
        adjacency_edges=((0, 1), (1, 2), (2, 3)),
        adjacency_edge_lengths_px=np.array([10, 10, 10], dtype=np.int32),
        continuity_weight=0.12,
        retaining_wall_weight=0.0,
    )

    assert tuple(result.initial_selected_stack_ids) == (0, 1, 1, 0)
    assert tuple(result.selected_stack_ids) == (0, 0, 0, 0)
    assert result.boundary_step_mean_after_coord_mm == result.boundary_step_mean_before_mm
    assert result.boundary_step_mean_after_mm < result.boundary_step_mean_after_coord_mm
    assert result.pair_repair_zone_changes == 2


def test_preview_uses_staged_backend_with_export_solved_plan():
    image = np.zeros((8, 8, 3), dtype=np.uint8)
    config = _offline_solve_config(
        palette=["bambu-basic-cyan", "bambu-basic-magenta"],
        white_base="panchroma-matte-cotton-white",
        profiles_dir=_PROFILES_DIR,
    )

    result = solve_preview(image, config)

    assert result.solved_plan is not None
    assert result.solved_plan.shape == result.cap_map.shape
    assert result.solved_plan.n_segments >= 1
    assert result.solved_plan.n_stacks >= 1
    assert result.staged_result is not None
    assert "__white_cap__" in result.thickness_maps
    # Task 5.4: DE/GAMUT live in diagnostics on the webapp path, not thickness_maps.
    assert "__de__" not in result.thickness_maps
    assert "__gamut_mask__" not in result.thickness_maps
    assert "__de__" in result.diagnostics
    assert "__gamut_mask__" in result.diagnostics
    # Task 2.3 regression: translucent underfill is removed, so a normal solve
    # must never emit the solve-only placeholder map.
    assert "__translucent_underfill__" not in result.thickness_maps
    assert result.cap_quality.get("cap_quality_mode") == "experimental"
    perf = result.staged_result.performance_profile
    assert perf.timings_s["stage0_compile_directives_s"] >= 0.0
    assert perf.timings_s["stage1_zone_plan_s"] >= 0.0
    assert perf.timings_s["stage2_total_s"] >= 0.0
    assert perf.timings_s["stage2_coord_descent_s"] >= 0.0
    assert perf.timings_s["stage2_pair_repair_s"] >= 0.0
    assert perf.timings_s["stage3_filler_s"] >= 0.0
    assert perf.timings_s["stage4_cap_s"] >= 0.0
    assert perf.timings_s["stage5_bridge_s"] >= 0.0
    assert perf.timings_s["stage5_solved_plan_bridge_s"] >= 0.0
    assert perf.timings_s["staged_finalize_total_s"] >= 0.0
    assert perf.timings_s["staged_pipeline_total_s"] >= 0.0
    assert perf.counters["stage1_zone_count"] >= 1
    assert perf.counters["stage2_zone_count"] >= 1
    assert perf.counters["solve_grid_pixel_count"] == 64
    assert perf.counters["stage2_solve_pixel_count"] == 64
    assert perf.counters["stage2_beam_expansion_count"] >= 1
    assert perf.counters["stage2_beam_max_size"] >= 1
    assert perf.counters["stage2_coord_descent_pass_count"] >= 1
    assert perf.counters["stage2_coord_descent_eval_count"] >= 0
    assert perf.counters["stage2_pair_repair_pass_count"] >= 0
    assert perf.counters["stage5_solved_plan_segments"] >= 1
    assert perf.counters["stage5_solved_plan_stacks"] >= 1
    assert perf.counters["stage2_pair_repair_trial_count"] >= 0
    assert perf.counters["stage2_boundary_mutation_enabled"] is True
    assert perf.counters["stage2_boundary_mutation_max_passes"] == 1
    assert perf.counters["stage2_boundary_mutation_passes_run"] == 1
    assert len(perf.counters["stage2_boundary_mutation_pass_accepted_pixels"]) == 1
    assert perf.counters["stage2_boundary_mutation_accepted_pixels"] >= 0
    assert perf.counters["stage4_boundary_edge_guard_pixels"] >= 0
    assert perf.counters["stage4_detail_active_pixels"] >= 0
    assert perf.counters["stage4_detail_candidate_zone_count"] >= 0
    assert perf.counters["stage4_detail_zone_count"] >= 0
    assert perf.counters["stage4_detail_rejected_zone_count"] >= 0
    assert perf.counters["stage4_detail_zone_rejected_pixels"] >= 0
    summary = result.staged_result.visible_plan.stage2_objective_summary
    assert result.staged_result.visible_plan.implied_cap_height_mm.shape == (8, 8)
    assert summary.boundary_step_mean_before_mm >= summary.boundary_step_mean_after_mm
    assert summary.boundary_step_p95_before_mm >= summary.boundary_step_p95_after_mm
    assert summary.local_cost_mean_before >= 0.0
    assert summary.local_cost_mean_after >= 0.0
    assert isinstance(summary.worst_edges, tuple)
    diagnostic_codes = {
        entry.code for entry in result.staged_result.planning_diagnostics.entries
    }
    assert "stage2_neighbor_seed_candidate_zone_hits" in diagnostic_codes
    assert "stage2_frontier_mean_size" in diagnostic_codes
    assert "stage2_frontier_thickness_span_mean_mm" in diagnostic_codes
    assert "stage2_frontier_neighbor_match_zone_hits" in diagnostic_codes
    assert "stage2_pair_repair_zone_changes" in diagnostic_codes


def test_project_zone_labels_to_fine_can_shift_projected_lattice():
    coarse = np.array([[0, 1], [2, 3]], dtype=np.int32)

    projected = _project_zone_labels_to_fine(
        coarse,
        factor=2,
        fine_shape=(4, 4),
        offset_y_px=1,
        offset_x_px=1,
    )

    np.testing.assert_array_equal(
        projected,
        np.array(
            [
                [0, 0, 0, 1],
                [0, 0, 0, 1],
                [0, 0, 0, 1],
                [2, 2, 2, 3],
            ],
            dtype=np.int32,
        ),
    )


def test_preview_can_use_coarse_stage1_and_fine_stage2():
    image = np.zeros((8, 8, 3), dtype=np.uint8)
    config = _offline_solve_config(
        palette=["bambu-basic-cyan", "bambu-basic-magenta"],
        white_base="panchroma-matte-cotton-white",
        profiles_dir=_PROFILES_DIR,
        stage1_coarsening_factor=2,
    )

    result = solve_preview(image, config)

    stage1 = result.staged_result.lateral_zone_plan
    stage2 = result.staged_result.visible_plan
    perf = result.staged_result.performance_profile

    assert stage1.planning_shape == (4, 4)
    assert stage1.planning_pitch_mm == 0.40
    assert stage1.coarse_to_fine_scale == 2
    assert stage2.evaluation_shape == (8, 8)
    assert stage2.zone_label_map.shape == (8, 8)
    assert perf.counters["stage1_planning_height_px"] == 4
    assert perf.counters["stage1_planning_width_px"] == 4
    assert perf.counters["stage1_coarsening_factor"] == 2
    assert perf.counters["stage2_solve_pixel_count"] == 64


def test_preview_can_offset_stage1_lattice():
    image = np.zeros((8, 8, 3), dtype=np.uint8)
    config = _offline_solve_config(
        palette=["bambu-basic-cyan", "bambu-basic-magenta"],
        white_base="panchroma-matte-cotton-white",
        profiles_dir=_PROFILES_DIR,
        stage1_coarsening_factor=2,
        stage1_lattice_offset_y_px=1,
        stage1_lattice_offset_x_px=1,
        cell_mode="grid",
        color_region_target_mm=0.20,
    )

    result = solve_preview(image, config)

    perf = result.staged_result.performance_profile
    assert perf.counters["stage1_lattice_offset_y_px"] == 1
    assert perf.counters["stage1_lattice_offset_x_px"] == 1
    assert result.staged_result.visible_plan.zone_label_map.shape == (8, 8)


def test_stage2_fine_recipe_assignments_can_override_within_zone():
    all_oklabs = np.full((12, 1, 3), np.nan, dtype=np.float32)
    all_oklabs[10, 0] = np.array([0.0, 0.0, 0.0], dtype=np.float32)
    all_oklabs[11, 0] = np.array([1.0, 0.0, 0.0], dtype=np.float32)

    (
        fine_stack_id_map,
        override_pixels,
        override_zones,
        interior_override_pixels,
        interior_override_zones,
    ) = _build_stage2_fine_recipe_assignments(
        fine_shape=(2, 2),
        coarse_to_fine_scale=2,
        zone_flat_indices=(np.array([0, 1, 2, 3], dtype=np.int32),),
        target_oklab_var_by_zone=np.array([[0.09, 0.0, 0.0]], dtype=np.float32),
        targets=np.array(
            [
                [0.0, 0.0, 0.0],
                [1.0, 0.0, 0.0],
                [1.0, 0.0, 0.0],
                [0.0, 0.0, 0.0],
            ],
            dtype=np.float32,
        ),
        pixel_stack_ids=np.array([10, 11, 11, 10], dtype=np.int32),
        candidate_sets=(
            _ZoneCandidateSet(
                candidate_ids=np.array([10, 11], dtype=np.int32),
                local_scores=np.array([0.0, 0.05], dtype=np.float32),
                total_thickness_mm=np.array([0.10, 0.20], dtype=np.float32),
            ),
        ),
        optimization=SimpleNamespace(selected_stack_ids=np.array([0], dtype=np.int32)),
        all_oklabs=all_oklabs,
    )

    assert override_pixels == 2
    assert override_zones == 1
    assert interior_override_pixels == 0
    assert interior_override_zones == 0
    np.testing.assert_array_equal(
        fine_stack_id_map,
        np.array([[10, 11], [11, 10]], dtype=np.int32),
    )


def test_stage2_fine_recipe_assignments_can_override_smooth_interior_blocks():
    all_oklabs = np.full((12, 1, 3), np.nan, dtype=np.float32)
    all_oklabs[10, 0] = np.array([0.0, 0.0, 0.0], dtype=np.float32)
    all_oklabs[11, 0] = np.array([1.0, 0.0, 0.0], dtype=np.float32)

    (
        fine_stack_id_map,
        override_pixels,
        override_zones,
        interior_override_pixels,
        interior_override_zones,
    ) = _build_stage2_fine_recipe_assignments(
        fine_shape=(2, 2),
        coarse_to_fine_scale=2,
        zone_flat_indices=(np.array([0, 1, 2, 3], dtype=np.int32),),
        target_oklab_var_by_zone=np.array([[0.0, 0.0, 0.0]], dtype=np.float32),
        targets=np.array(
            [
                [1.0, 0.0, 0.0],
                [1.0, 0.0, 0.0],
                [1.0, 0.0, 0.0],
                [1.0, 0.0, 0.0],
            ],
            dtype=np.float32,
        ),
        pixel_stack_ids=np.array([11, 11, 11, 11], dtype=np.int32),
        candidate_sets=(
            _ZoneCandidateSet(
                candidate_ids=np.array([10, 11], dtype=np.int32),
                local_scores=np.array([0.0, 0.05], dtype=np.float32),
                total_thickness_mm=np.array([0.10, 0.20], dtype=np.float32),
            ),
        ),
        optimization=SimpleNamespace(selected_stack_ids=np.array([0], dtype=np.int32)),
        all_oklabs=all_oklabs,
    )

    assert override_pixels == 4
    assert override_zones == 1
    assert interior_override_pixels == 4
    assert interior_override_zones == 1
    np.testing.assert_array_equal(
        fine_stack_id_map,
        np.full((2, 2), 11, dtype=np.int32),
    )


def test_stage2_fine_override_seam_gate_rejects_single_pixel_component():
    all_oklabs = np.full((2, 1, 3), np.nan, dtype=np.float32)
    all_oklabs[0, 0] = np.array([0.0, 0.0, 0.0], dtype=np.float32)
    all_oklabs[1, 0] = np.array([0.02, 0.0, 0.0], dtype=np.float32)
    targets = np.tile(np.array([[0.03, 0.0, 0.0]], dtype=np.float32), (4, 1))

    gated, rejected_pixels, rejected_components, accepted_components = (
        _apply_stage2_fine_override_seam_gate(
            fine_stack_id_map=np.array([[1, 0], [0, 0]], dtype=np.int32),
            fine_shape=(2, 2),
            zone_flat_indices=(np.array([0, 1, 2, 3], dtype=np.int32),),
            selected_zone_stack_ids=np.array([0], dtype=np.int32),
            targets=targets,
            unique_stack_dicts={0: {"a": 0.0}, 1: {"a": 1.0}},
            all_oklabs=all_oklabs,
            seam_penalty_weight=0.01,
        )
    )

    assert rejected_pixels == 1
    assert rejected_components == 1
    assert accepted_components == 0
    np.testing.assert_array_equal(gated, np.zeros((2, 2), dtype=np.int32))


def test_stage2_fine_override_seam_gate_keeps_whole_zone_component():
    all_oklabs = np.full((2, 1, 3), np.nan, dtype=np.float32)
    all_oklabs[0, 0] = np.array([0.0, 0.0, 0.0], dtype=np.float32)
    all_oklabs[1, 0] = np.array([0.02, 0.0, 0.0], dtype=np.float32)
    targets = np.tile(np.array([[0.03, 0.0, 0.0]], dtype=np.float32), (4, 1))

    gated, rejected_pixels, rejected_components, accepted_components = (
        _apply_stage2_fine_override_seam_gate(
            fine_stack_id_map=np.ones((2, 2), dtype=np.int32),
            fine_shape=(2, 2),
            zone_flat_indices=(np.array([0, 1, 2, 3], dtype=np.int32),),
            selected_zone_stack_ids=np.array([0], dtype=np.int32),
            targets=targets,
            unique_stack_dicts={0: {"a": 0.0}, 1: {"a": 1.0}},
            all_oklabs=all_oklabs,
            seam_penalty_weight=0.01,
        )
    )

    assert rejected_pixels == 0
    assert rejected_components == 0
    assert accepted_components == 1
    np.testing.assert_array_equal(gated, np.ones((2, 2), dtype=np.int32))


def test_stage2_fine_override_printability_gate_rejects_hard_fail_component():
    settings = BlueprintPrintabilitySettings(
        minimum_extrusion_width_mm=0.40,
        minimum_line_length_mm=0.50,
        pitch_mm=0.20,
        layer_height_mm=0.08,
    )

    result = _apply_stage2_fine_override_printability_gate(
        fine_stack_id_map=np.array(
            [
                [1, 0, 0],
                [0, 0, 0],
                [0, 0, 0],
            ],
            dtype=np.int32,
        ),
        fine_shape=(3, 3),
        zone_flat_indices=(np.arange(9, dtype=np.int32),),
        selected_zone_stack_ids=np.array([0], dtype=np.int32),
        settings=settings,
    )

    assert result.reverted_pixels == 1
    assert result.reverted_components == 1
    assert result.accepted_components == 0
    assert result.rejected_tiny_components == 1
    assert result.rejected_narrow_components == 1
    assert result.rejected_short_components == 1
    np.testing.assert_array_equal(result.fine_stack_id_map, np.zeros((3, 3), dtype=np.int32))
    assert int(result.rejection_map[0, 0]) == 7


def test_stage2_fine_override_printability_gate_keeps_acceptable_component():
    settings = BlueprintPrintabilitySettings(
        minimum_extrusion_width_mm=0.40,
        minimum_line_length_mm=0.50,
        pitch_mm=0.20,
        layer_height_mm=0.08,
    )
    fine_stack_id_map = np.array(
        [
            [1, 1, 1],
            [1, 1, 1],
            [0, 0, 0],
        ],
        dtype=np.int32,
    )

    result = _apply_stage2_fine_override_printability_gate(
        fine_stack_id_map=fine_stack_id_map,
        fine_shape=(3, 3),
        zone_flat_indices=(np.arange(9, dtype=np.int32),),
        selected_zone_stack_ids=np.array([0], dtype=np.int32),
        settings=settings,
    )

    assert result.rejected_pixels == 0
    assert result.rejected_components == 0
    assert result.accepted_components == 1
    np.testing.assert_array_equal(result.fine_stack_id_map, fine_stack_id_map)
    assert int(np.count_nonzero(result.rejection_map)) == 0


def test_stage2_fine_override_printability_repair_grows_valuable_component():
    settings = BlueprintPrintabilitySettings(
        minimum_extrusion_width_mm=0.40,
        minimum_line_length_mm=0.50,
        pitch_mm=0.20,
        layer_height_mm=0.08,
    )
    all_oklabs = np.full((2, 1, 3), np.nan, dtype=np.float32)
    all_oklabs[0, 0] = np.array([0.0, 0.0, 0.0], dtype=np.float32)
    all_oklabs[1, 0] = np.array([1.0, 0.0, 0.0], dtype=np.float32)
    targets = np.tile(np.array([[1.0, 0.0, 0.0]], dtype=np.float32), (25, 1))

    result = _apply_stage2_fine_override_printability_gate(
        fine_stack_id_map=np.array(
            [
                [0, 0, 0, 0, 0],
                [0, 0, 0, 0, 0],
                [0, 0, 1, 0, 0],
                [0, 0, 0, 0, 0],
                [0, 0, 0, 0, 0],
            ],
            dtype=np.int32,
        ),
        fine_shape=(5, 5),
        zone_flat_indices=(np.arange(25, dtype=np.int32),),
        selected_zone_stack_ids=np.array([0], dtype=np.int32),
        settings=settings,
        repair_enabled=True,
        targets=targets,
        all_oklabs=all_oklabs,
        repair_min_mean_gain=0.004,
    )

    assert result.repaired_components == 1
    assert result.repaired_original_pixels == 1
    assert result.repaired_added_pixels == 4
    assert result.rejected_components == 0
    assert int(np.count_nonzero(result.fine_stack_id_map == 1)) == 5
    assert int(np.count_nonzero(result.repair_map == 1)) == 1
    assert int(np.count_nonzero(result.repair_map == 2)) == 4


def test_stage2_fine_override_printability_repair_rejects_weak_growth():
    settings = BlueprintPrintabilitySettings(
        minimum_extrusion_width_mm=0.40,
        minimum_line_length_mm=0.50,
        pitch_mm=0.20,
        layer_height_mm=0.08,
    )
    all_oklabs = np.full((2, 1, 3), np.nan, dtype=np.float32)
    all_oklabs[0, 0] = np.array([0.0, 0.0, 0.0], dtype=np.float32)
    all_oklabs[1, 0] = np.array([1.0, 0.0, 0.0], dtype=np.float32)
    targets = np.zeros((25, 3), dtype=np.float32)
    targets[12] = np.array([1.0, 0.0, 0.0], dtype=np.float32)

    result = _apply_stage2_fine_override_printability_gate(
        fine_stack_id_map=np.array(
            [
                [0, 0, 0, 0, 0],
                [0, 0, 0, 0, 0],
                [0, 0, 1, 0, 0],
                [0, 0, 0, 0, 0],
                [0, 0, 0, 0, 0],
            ],
            dtype=np.int32,
        ),
        fine_shape=(5, 5),
        zone_flat_indices=(np.arange(25, dtype=np.int32),),
        selected_zone_stack_ids=np.array([0], dtype=np.int32),
        settings=settings,
        repair_enabled=True,
        targets=targets,
        all_oklabs=all_oklabs,
        repair_min_mean_gain=0.004,
    )

    assert result.repaired_components == 0
    assert result.repair_rejected_components == 1
    assert result.rejected_components == 1
    np.testing.assert_array_equal(result.fine_stack_id_map, np.zeros((5, 5), dtype=np.int32))


def test_stage2_final_color_printability_gate_keeps_boundary_attached_delta():
    settings = BlueprintPrintabilitySettings(
        minimum_extrusion_width_mm=0.20,
        minimum_line_length_mm=0.50,
        pitch_mm=0.20,
        layer_height_mm=0.08,
    )
    fine_stack_id_map = np.array(
        [
            [0, 0, 1, 1],
            [0, 1, 1, 1],
            [0, 0, 1, 1],
        ],
        dtype=np.int32,
    )
    zone0 = np.array([0, 1, 4, 5, 8, 9], dtype=np.int32)
    zone1 = np.array([2, 3, 6, 7, 10, 11], dtype=np.int32)

    result = _apply_stage2_final_color_printability_gate(
        fine_stack_id_map=fine_stack_id_map,
        fine_shape=(3, 4),
        zone_flat_indices=(zone0, zone1),
        selected_zone_stack_ids=np.array([0, 1], dtype=np.int32),
        unique_stack_dicts={
            0: {"a": 0.08},
            1: {"b": 0.08},
        },
        palette_order=("a", "b"),
        layer_height_mm=0.08,
        settings=settings,
    )

    assert result.absorbed_components == 0
    assert result.unresolved_components == 0
    np.testing.assert_array_equal(result.fine_stack_id_map, fine_stack_id_map)


def test_stage2_final_color_printability_gate_rejects_isolated_final_feature():
    settings = BlueprintPrintabilitySettings(
        minimum_extrusion_width_mm=0.20,
        minimum_line_length_mm=0.50,
        pitch_mm=0.20,
        layer_height_mm=0.08,
    )
    fine_stack_id_map = np.array(
        [
            [0, 0, 0],
            [0, 1, 0],
            [0, 0, 0],
        ],
        dtype=np.int32,
    )

    result = _apply_stage2_final_color_printability_gate(
        fine_stack_id_map=fine_stack_id_map,
        fine_shape=(3, 3),
        zone_flat_indices=(np.arange(9, dtype=np.int32),),
        selected_zone_stack_ids=np.array([0], dtype=np.int32),
        unique_stack_dicts={
            0: {"a": 0.08},
            1: {"b": 0.08},
        },
        palette_order=("a", "b"),
        layer_height_mm=0.08,
        settings=settings,
    )

    assert result.absorbed_pixels == 1
    assert result.absorbed_components == 1
    np.testing.assert_array_equal(result.fine_stack_id_map, np.zeros((3, 3), dtype=np.int32))


def test_stage2_final_color_printability_gate_absorbs_mandatory_cap_island():
    settings = BlueprintPrintabilitySettings(
        minimum_extrusion_width_mm=0.20,
        minimum_line_length_mm=0.50,
        pitch_mm=0.20,
        layer_height_mm=0.08,
    )
    fine_stack_id_map = np.array(
        [
            [1, 1, 1],
            [1, 0, 1],
            [1, 1, 1],
        ],
        dtype=np.int32,
    )

    result = _apply_stage2_final_color_printability_gate(
        fine_stack_id_map=fine_stack_id_map,
        fine_shape=(3, 3),
        zone_flat_indices=(np.arange(9, dtype=np.int32),),
        selected_zone_stack_ids=np.array([1], dtype=np.int32),
        unique_stack_dicts={
            0: {"a": 0.08},
            1: {"a": 0.16},
        },
        palette_order=("a",),
        layer_height_mm=0.08,
        settings=settings,
        minimum_cap_height_mm=0.08,
    )

    assert result.absorbed_pixels == 1
    assert result.absorbed_components == 1
    assert int(result.absorption_map[1, 1]) != 0
    np.testing.assert_array_equal(
        result.fine_stack_id_map,
        np.ones((3, 3), dtype=np.int32),
    )


def test_stage2_final_color_printability_gate_absorbs_coarse_hard_fail():
    settings = BlueprintPrintabilitySettings(
        minimum_extrusion_width_mm=0.20,
        minimum_line_length_mm=0.50,
        pitch_mm=0.20,
        layer_height_mm=0.08,
    )
    fine_stack_id_map = np.array(
        [
            [2, 2, 2],
            [2, 1, 2],
            [2, 2, 2],
        ],
        dtype=np.int32,
    )

    result = _apply_stage2_final_color_printability_gate(
        fine_stack_id_map=fine_stack_id_map,
        fine_shape=(3, 3),
        zone_flat_indices=(np.arange(9, dtype=np.int32),),
        selected_zone_stack_ids=np.array([1], dtype=np.int32),
        unique_stack_dicts={
            1: {"a": 0.08},
            2: {"b": 0.08},
        },
        palette_order=("a", "b"),
        layer_height_mm=0.08,
        settings=settings,
    )

    assert result.absorbed_pixels == 1
    assert result.absorbed_components == 1
    assert int(result.absorption_map[1, 1]) != 0
    np.testing.assert_array_equal(
        result.fine_stack_id_map,
        np.full((3, 3), 2, dtype=np.int32),
    )


def test_stage2_final_color_printability_gate_prefers_optical_neighbor():
    settings = BlueprintPrintabilitySettings(
        minimum_extrusion_width_mm=0.20,
        minimum_line_length_mm=0.40,
        pitch_mm=0.20,
        layer_height_mm=0.08,
    )
    fine_stack_id_map = np.array(
        [
            [2, 2, 3, 3],
            [2, 2, 1, 3],
            [3, 3, 3, 3],
            [3, 3, 3, 3],
        ],
        dtype=np.int32,
    )
    all_oklabs = np.full((4, 1, 3), np.nan, dtype=np.float32)
    all_oklabs[1, 0] = np.array([0.5, 0.0, 0.0], dtype=np.float32)
    all_oklabs[2, 0] = np.array([0.5, 0.0, 0.0], dtype=np.float32)
    all_oklabs[3, 0] = np.array([0.0, 0.8, 0.0], dtype=np.float32)
    targets = np.tile(np.array([[0.5, 0.0, 0.0]], dtype=np.float32), (16, 1))

    result = _apply_stage2_final_color_printability_gate(
        fine_stack_id_map=fine_stack_id_map,
        fine_shape=(4, 4),
        zone_flat_indices=(np.arange(16, dtype=np.int32),),
        selected_zone_stack_ids=np.array([1], dtype=np.int32),
        unique_stack_dicts={
            1: {"a": 0.08},
            2: {"b": 0.08},
            3: {"c": 0.08},
        },
        palette_order=("a", "b", "c"),
        layer_height_mm=0.08,
        settings=settings,
        targets=targets,
        all_oklabs=all_oklabs,
    )

    assert result.absorbed_pixels == 1
    assert int(result.fine_stack_id_map[1, 2]) == 2


def test_stage2_final_color_printability_gate_diagnostic_only_preserves_map():
    settings = BlueprintPrintabilitySettings(
        minimum_extrusion_width_mm=0.20,
        minimum_line_length_mm=0.50,
        pitch_mm=0.20,
        layer_height_mm=0.08,
    )
    fine_stack_id_map = np.array(
        [
            [0, 0, 0],
            [0, 1, 0],
            [0, 0, 0],
        ],
        dtype=np.int32,
    )

    result = _apply_stage2_final_color_printability_gate(
        fine_stack_id_map=fine_stack_id_map,
        fine_shape=(3, 3),
        zone_flat_indices=(np.arange(9, dtype=np.int32),),
        selected_zone_stack_ids=np.array([0], dtype=np.int32),
        unique_stack_dicts={
            0: {"a": 0.08},
            1: {"b": 0.08},
        },
        palette_order=("a", "b"),
        layer_height_mm=0.08,
        settings=settings,
        apply_changes=False,
    )

    assert result.absorbed_pixels == 1
    assert result.absorbed_components == 1
    assert int(result.absorption_map[1, 1]) != 0
    np.testing.assert_array_equal(result.fine_stack_id_map, fine_stack_id_map)


def test_stage2_final_color_printability_gate_ignores_nonstructural_opening_loss():
    settings = BlueprintPrintabilitySettings(
        minimum_extrusion_width_mm=0.40,
        minimum_line_length_mm=0.50,
        pitch_mm=0.20,
        layer_height_mm=0.08,
    )
    fine_stack_id_map = np.array(
        [
            [0, 0, 0, 0, 0, 1, 1, 0, 0, 0, 0],
            [0, 0, 0, 0, 0, 1, 1, 0, 0, 0, 0],
            [0, 0, 0, 0, 1, 1, 1, 0, 0, 0, 0],
            [0, 0, 0, 0, 1, 1, 1, 0, 0, 0, 0],
            [0, 0, 0, 1, 1, 1, 1, 0, 0, 0, 0],
            [0, 0, 1, 1, 1, 0, 1, 1, 0, 0, 0],
            [0, 0, 1, 1, 0, 0, 1, 1, 0, 0, 0],
            [0, 0, 0, 0, 0, 0, 0, 1, 1, 0, 0],
            [0, 0, 0, 0, 0, 0, 0, 1, 1, 0, 0],
            [0, 0, 0, 0, 0, 0, 0, 1, 1, 0, 0],
            [0, 0, 0, 0, 0, 0, 0, 1, 1, 0, 0],
        ],
        dtype=np.int32,
    )

    result = _apply_stage2_final_color_printability_gate(
        fine_stack_id_map=fine_stack_id_map,
        fine_shape=fine_stack_id_map.shape,
        zone_flat_indices=(np.arange(fine_stack_id_map.size, dtype=np.int32),),
        selected_zone_stack_ids=np.array([0], dtype=np.int32),
        unique_stack_dicts={
            0: {"purple": 0.08},
            1: {"cyan": 0.08},
        },
        palette_order=("purple", "cyan"),
        layer_height_mm=0.08,
        settings=settings,
    )

    # This shape has raw morphological opening loss at one interior pixel, but
    # the loss is nonstructural: removing it does not destroy or split the
    # component.  The blueprint diagnostic reports it as warning/margin
    # telemetry rather than a hard fail, so final substrate repair must not
    # rewrite the recipe map here.
    assert result.absorbed_pixels == 0
    assert result.absorbed_components == 0
    assert result.unresolved_components == 0
    np.testing.assert_array_equal(result.fine_stack_id_map, fine_stack_id_map)
    assert int(np.count_nonzero(result.absorption_map)) == 0


def test_stage2_printability_ledger_snapshot_matches_structural_contract():
    settings = BlueprintPrintabilitySettings(
        minimum_extrusion_width_mm=0.40,
        minimum_line_length_mm=0.50,
        pitch_mm=0.20,
        layer_height_mm=0.08,
    )
    structural_neck = np.zeros((5, 9), dtype=np.int32)
    structural_neck[1:4, 1:4] = 1
    structural_neck[1:4, 5:8] = 1
    structural_neck[2, 4] = 1
    nonstructural = np.array(
        [
            [0, 0, 0, 0, 0, 1, 1, 0, 0, 0, 0],
            [0, 0, 0, 0, 0, 1, 1, 0, 0, 0, 0],
            [0, 0, 0, 0, 1, 1, 1, 0, 0, 0, 0],
            [0, 0, 0, 0, 1, 1, 1, 0, 0, 0, 0],
            [0, 0, 0, 1, 1, 1, 1, 0, 0, 0, 0],
            [0, 0, 1, 1, 1, 0, 1, 1, 0, 0, 0],
            [0, 0, 1, 1, 0, 0, 1, 1, 0, 0, 0],
            [0, 0, 0, 0, 0, 0, 0, 1, 1, 0, 0],
            [0, 0, 0, 0, 0, 0, 0, 1, 1, 0, 0],
            [0, 0, 0, 0, 0, 0, 0, 1, 1, 0, 0],
            [0, 0, 0, 0, 0, 0, 0, 1, 1, 0, 0],
        ],
        dtype=np.int32,
    )

    structural_snapshot = _stage2_printability_failure_snapshot_from_stack_ids(
        fine_stack_id_map=structural_neck,
        unique_stack_dicts={0: {"purple": 0.08}, 1: {"cyan": 0.08}},
        palette_order=("purple", "cyan"),
        layer_height_mm=0.08,
        settings=settings,
    )
    nonstructural_snapshot = _stage2_printability_failure_snapshot_from_stack_ids(
        fine_stack_id_map=nonstructural,
        unique_stack_dicts={0: {"purple": 0.08}, 1: {"cyan": 0.08}},
        palette_order=("purple", "cyan"),
        layer_height_mm=0.08,
        settings=settings,
    )

    assert structural_snapshot.total_hard_pixels > 0
    assert structural_snapshot.total_hard_components > 0
    assert nonstructural_snapshot.total_hard_pixels == 0
    assert nonstructural_snapshot.total_hard_components == 0


def test_stage2_localized_width_nudge_fixes_large_neck_one_shot():
    settings = BlueprintPrintabilitySettings(
        minimum_extrusion_width_mm=0.40,
        minimum_line_length_mm=0.50,
        pitch_mm=0.20,
        layer_height_mm=0.08,
    )
    fine_stack_id_map = np.zeros((5, 9), dtype=np.int32)
    fine_stack_id_map[1:4, 1:4] = 1
    fine_stack_id_map[1:4, 5:8] = 1
    fine_stack_id_map[2, 4] = 1

    result = _apply_stage2_localized_width_loss_boundary_nudge(
        fine_stack_id_map=fine_stack_id_map,
        unique_stack_dicts={
            0: {"purple": 0.08},
            1: {"cyan": 0.08},
        },
        palette_order=("purple", "cyan"),
        layer_height_mm=0.08,
        settings=settings,
    )

    assert result.candidate_pixels >= 1
    assert 1 <= result.accepted_pixels <= 3
    assert result.accepted_components == 1
    assert result.edge_delta <= 0
    assert not np.array_equal(result.fine_stack_id_map, fine_stack_id_map)

    post = _apply_stage2_localized_width_loss_boundary_nudge(
        fine_stack_id_map=result.fine_stack_id_map,
        unique_stack_dicts={
            0: {"purple": 0.08},
            1: {"cyan": 0.08},
        },
        palette_order=("purple", "cyan"),
        layer_height_mm=0.08,
        settings=settings,
    )
    assert post.candidate_pixels == 0


def _stage4_printability_settings() -> BlueprintPrintabilitySettings:
    return BlueprintPrintabilitySettings(
        minimum_extrusion_width_mm=0.40,
        minimum_line_length_mm=0.50,
        pitch_mm=0.20,
        layer_height_mm=0.08,
    )


def _stage4_boundary_visible_plan(shape: tuple[int, int]) -> VisibleRecipeRawGeometryPlan:
    recipe_label_map = np.zeros(shape, dtype=np.int32)
    return VisibleRecipeRawGeometryPlan(
        evaluation_shape=shape,
        evaluation_pitch_mm=0.20,
        zone_label_map=np.zeros(shape, dtype=np.int32),
        zone_recipe_labels=np.array([0], dtype=np.int32),
        fine_recipe_label_map=recipe_label_map,
        recipe_table=(VisibleRecipe.from_mapping({}),),
        base_top_mm=np.full(shape, 0.20, dtype=np.float32),
        raw_color_ceiling_mm=np.full(shape, 0.20, dtype=np.float32),
        implied_cap_height_mm=np.full(shape, 0.08, dtype=np.float32),
        gamut_mask=np.zeros(shape, dtype=np.float32),
        mapped_target_oklab=np.zeros((int(np.prod(shape)), 3), dtype=np.float32),
        stage2_objective_summary=Stage2ObjectiveSummary(
            continuity_weight=0.0,
            retaining_wall_weight=0.0,
            local_cost_mean_before=0.0,
            local_cost_mean_after=0.0,
            intra_zone_target_variance_mean=0.0,
            boundary_step_mean_before_mm=0.0,
            boundary_step_mean_after_mm=0.0,
            boundary_step_p95_before_mm=0.0,
            boundary_step_p95_after_mm=0.0,
            changed_zone_count=0,
            changed_zones=(),
            worst_edges=(),
        ),
    )


def _appearance_bound_visible_plan(
    *,
    shape: tuple[int, int],
    cap_oklab_rows: np.ndarray,
    target_oklab: np.ndarray | None = None,
) -> VisibleRecipeRawGeometryPlan:
    recipe_label_map = np.zeros(shape, dtype=np.int32)
    layer_height = 0.08
    caps = (
        np.arange(1, int(cap_oklab_rows.shape[0]) + 1, dtype=np.float32)
        * np.float32(layer_height)
    )
    targets = (
        np.asarray(target_oklab, dtype=np.float32).reshape(shape + (3,))
        if target_oklab is not None
        else np.zeros(shape + (3,), dtype=np.float32)
    )
    return VisibleRecipeRawGeometryPlan(
        evaluation_shape=shape,
        evaluation_pitch_mm=0.20,
        zone_label_map=np.zeros(shape, dtype=np.int32),
        zone_recipe_labels=np.array([0], dtype=np.int32),
        fine_recipe_label_map=recipe_label_map,
        recipe_table=(VisibleRecipe.from_mapping({}),),
        base_top_mm=np.full(shape, 0.20, dtype=np.float32),
        raw_color_ceiling_mm=np.full(shape, 0.20, dtype=np.float32),
        implied_cap_height_mm=np.full(shape, 0.08, dtype=np.float32),
        gamut_mask=np.zeros(shape, dtype=np.float32),
        mapped_target_oklab=targets.reshape(-1, 3),
        stage2_objective_summary=Stage2ObjectiveSummary(
            continuity_weight=0.0,
            retaining_wall_weight=0.0,
            local_cost_mean_before=0.0,
            local_cost_mean_after=0.0,
            intra_zone_target_variance_mean=0.0,
            boundary_step_mean_before_mm=0.0,
            boundary_step_mean_after_mm=0.0,
            boundary_step_p95_before_mm=0.0,
            boundary_step_p95_after_mm=0.0,
            changed_zone_count=0,
            changed_zones=(),
            worst_edges=(),
        ),
        recipe_stack_ids=np.array([0], dtype=np.int32),
        stage2_cap_values_mm=caps,
        stage2_stack_cap_oklab=np.asarray(cap_oklab_rows, dtype=np.float32).reshape(
            1,
            int(cap_oklab_rows.shape[0]),
            3,
        ),
    )


def _appearance_bound_state(*, de_budget: float = 0.008):
    class _Provider:
        model_kind = "photo_stack_bundle"

        def predict_stack_appearance_linear_rgb_batch(self, requests):
            raise AssertionError("precomputed Stage 2 cap lookup should be used")

    return SimpleNamespace(
        config=SimpleNamespace(
            layer_height=0.08,
            d_wb=0.20,
            white_base="white",
            white_cap=None,
            boundary_cap_de_budget=de_budget,
            effective_white_cap=lambda: "white",
        ),
        appearance_provider=_Provider(),
        diagnostics={},
    )


def _stage4_boundary_plan_from_requested_cap(
    monkeypatch,
    *,
    requested_cap: np.ndarray,
    enforce_printability: bool,
):
    config = SolveConfig(
        palette=["a"],
        white_base="white",
        enforce_printability=enforce_printability,
        stage4_printability_gate_detail=not enforce_printability,
        emit_blueprint_printability=True,
        printability_minimum_extrusion_width_mm=0.40,
        printability_minimum_line_length_mm=0.50,
        solver_fine_pitch_mm=0.20,
        image_sample_pitch_mm=0.20,
        layer_height=0.08,
        d_wb=0.20,
        d_wc_min=0.08,
        d_wc_max=0.32,
        t_max=1.00,
        detail_cap_enabled=False,
    )
    shape = tuple(requested_cap.shape)
    visible_plan = _stage4_boundary_visible_plan(shape)  # type: ignore[arg-type]
    filler_plan = FillerGeometryPlan(
        raw_color_ceiling_mm=np.full(shape, 0.20, dtype=np.float32),
        filler_height_mm=np.zeros(shape, dtype=np.float32),
        color_ceiling_mm=np.full(shape, 0.20, dtype=np.float32),
    )

    def fake_requested_stage4_cap_maps(state, visible_plan, filler_plan, diagnostics):
        _ = state, visible_plan, filler_plan, diagnostics
        return (
            np.asarray(requested_cap, dtype=np.float32).copy(),
            np.asarray(requested_cap, dtype=np.float32).copy(),
            np.zeros(shape, dtype=np.float32),
        )

    monkeypatch.setattr(
        staged_runner,
        "_requested_stage4_cap_maps",
        fake_requested_stage4_cap_maps,
    )
    diagnostics = PlanningDiagnosticsStream()
    cap_plan = staged_runner._build_stage4_cap_plan(
        SimpleNamespace(config=config),
        visible_plan,
        filler_plan,
        diagnostics,
    )
    return config, visible_plan, filler_plan, diagnostics, cap_plan


def _stage4_split_plan_from_requested_cap(
    monkeypatch,
    *,
    requested_cap: np.ndarray,
    detail_enabled: bool = True,
    detail_max_layers: int = 20,
    enforce_printability: bool = False,
    luminance_enabled: bool = False,
    visible_plan: VisibleRecipeRawGeometryPlan | None = None,
):
    config = SolveConfig(
        palette=["a"],
        white_base="white",
        cap_mode="appearance_bounded_smooth",
        enforce_printability=enforce_printability,
        stage4_printability_gate_detail=not enforce_printability,
        emit_blueprint_printability=True,
        printability_minimum_extrusion_width_mm=0.40,
        printability_minimum_line_length_mm=0.50,
        solver_fine_pitch_mm=0.20,
        image_sample_pitch_mm=0.20,
        layer_height=0.08,
        d_wb=0.20,
        d_wc_min=0.08,
        d_wc_max=0.32,
        t_max=1.00,
        detail_cap_enabled=detail_enabled,
        detail_cap_max_layers=detail_max_layers,
        luminance_handler_enabled=luminance_enabled,
    )
    shape = tuple(requested_cap.shape)
    if visible_plan is None:
        visible_plan = _stage4_boundary_visible_plan(shape)  # type: ignore[arg-type]
    filler_plan = FillerGeometryPlan(
        raw_color_ceiling_mm=np.full(shape, 0.20, dtype=np.float32),
        filler_height_mm=np.zeros(shape, dtype=np.float32),
        color_ceiling_mm=np.full(shape, 0.20, dtype=np.float32),
    )

    def fake_requested_stage4_cap_maps(state, visible_plan, filler_plan, diagnostics):
        _ = state, visible_plan, filler_plan, diagnostics
        return (
            np.asarray(requested_cap, dtype=np.float32).copy(),
            np.asarray(requested_cap, dtype=np.float32).copy(),
            np.zeros(shape, dtype=np.float32),
        )

    def fake_optical_gain_map(*, state, visible_plan, boundary_cap_height, final_cap_target, detail_mask):
        _ = state, visible_plan, boundary_cap_height, final_cap_target
        return np.asarray(detail_mask, dtype=np.float32)

    monkeypatch.setattr(
        staged_runner,
        "_requested_stage4_cap_maps",
        fake_requested_stage4_cap_maps,
    )
    monkeypatch.setattr(
        staged_runner,
        "_compute_stage4_detail_optical_gain_map",
        fake_optical_gain_map,
    )
    if luminance_enabled:
        monkeypatch.setattr(staged_runner, "luminance_handler_enabled", lambda cfg: True)
    diagnostics = PlanningDiagnosticsStream()
    state = SimpleNamespace(config=config, debug_maps={})
    cap_plan = staged_runner._build_stage4_cap_plan(
        state,
        visible_plan,
        filler_plan,
        diagnostics,
    )
    return config, visible_plan, filler_plan, state, diagnostics, cap_plan


def test_stage4_appearance_bounded_mode_is_registered():
    assert "appearance_bounded_smooth" in staged_runner._STAGE4_SUPPORTED_CAP_MODES
    assert "fixed" not in staged_runner._STAGE4_SUPPORTED_CAP_MODES


def test_stage4_appearance_bound_accepts_visually_cheap_smoothing():
    rows = np.asarray(
        [
            [0.000, 0.0, 0.0],
            [0.002, 0.0, 0.0],
            [0.003, 0.0, 0.0],
            [0.004, 0.0, 0.0],
        ],
        dtype=np.float32,
    )
    visible_plan = _appearance_bound_visible_plan(shape=(1, 1), cap_oklab_rows=rows)
    accepted, debug_maps, summary = staged_runner._apply_stage4_boundary_appearance_bound(
        state=_appearance_bound_state(de_budget=0.008),
        visible_plan=visible_plan,
        raw_cap=np.asarray([[0.08]], dtype=np.float32),
        smooth_candidate_cap=np.asarray([[0.32]], dtype=np.float32),
        layer_height=0.08,
        d_wc_min=0.08,
        d_wc_max=0.32,
        de_budget=0.008,
    )

    np.testing.assert_allclose(accepted, np.asarray([[0.32]], dtype=np.float32))
    assert summary["accepted_pixels"] == 1
    assert summary["rejected_pixels"] == 0
    assert debug_maps["stage4_boundary_appearance_accept_mask"][0, 0] == 1.0


def test_stage4_appearance_bound_rejects_damaging_smooth_cap():
    rows = np.asarray(
        [
            [0.000, 0.0, 0.0],
            [0.004, 0.0, 0.0],
            [0.014, 0.0, 0.0],
            [0.030, 0.0, 0.0],
        ],
        dtype=np.float32,
    )
    visible_plan = _appearance_bound_visible_plan(shape=(1, 1), cap_oklab_rows=rows)
    accepted, debug_maps, summary = staged_runner._apply_stage4_boundary_appearance_bound(
        state=_appearance_bound_state(de_budget=0.001),
        visible_plan=visible_plan,
        raw_cap=np.asarray([[0.08]], dtype=np.float32),
        smooth_candidate_cap=np.asarray([[0.32]], dtype=np.float32),
        layer_height=0.08,
        d_wc_min=0.08,
        d_wc_max=0.32,
        de_budget=0.001,
    )

    np.testing.assert_allclose(accepted, np.asarray([[0.08]], dtype=np.float32))
    assert summary["accepted_pixels"] == 0
    assert summary["rejected_pixels"] == 1
    np.testing.assert_allclose(
        debug_maps["stage4_boundary_appearance_rejected_mm"],
        np.asarray([[0.24]], dtype=np.float32),
        atol=1e-6,
    )


def test_stage4_appearance_bound_chooses_intermediate_layer():
    rows = np.asarray(
        [
            [0.000, 0.0, 0.0],
            [0.004, 0.0, 0.0],
            [0.012, 0.0, 0.0],
            [0.030, 0.0, 0.0],
        ],
        dtype=np.float32,
    )
    visible_plan = _appearance_bound_visible_plan(shape=(1, 1), cap_oklab_rows=rows)
    accepted, debug_maps, summary = staged_runner._apply_stage4_boundary_appearance_bound(
        state=_appearance_bound_state(de_budget=0.008),
        visible_plan=visible_plan,
        raw_cap=np.asarray([[0.08]], dtype=np.float32),
        smooth_candidate_cap=np.asarray([[0.32]], dtype=np.float32),
        layer_height=0.08,
        d_wc_min=0.08,
        d_wc_max=0.32,
        de_budget=0.008,
    )

    np.testing.assert_allclose(accepted, np.asarray([[0.16]], dtype=np.float32))
    assert summary["accepted_pixels"] == 0
    assert summary["rejected_pixels"] == 1
    np.testing.assert_allclose(
        debug_maps["stage4_boundary_accepted_minus_raw_mm"],
        np.asarray([[0.08]], dtype=np.float32),
        atol=1e-6,
    )


def test_stage4_appearance_bound_records_debug_maps():
    rows = np.asarray(
        [
            [0.000, 0.0, 0.0],
            [0.004, 0.0, 0.0],
            [0.012, 0.0, 0.0],
            [0.030, 0.0, 0.0],
        ],
        dtype=np.float32,
    )
    visible_plan = _appearance_bound_visible_plan(shape=(2, 2), cap_oklab_rows=rows)
    accepted, debug_maps, summary = staged_runner._apply_stage4_boundary_appearance_bound(
        state=_appearance_bound_state(de_budget=0.008),
        visible_plan=visible_plan,
        raw_cap=np.full((2, 2), 0.08, dtype=np.float32),
        smooth_candidate_cap=np.full((2, 2), 0.32, dtype=np.float32),
        layer_height=0.08,
        d_wc_min=0.08,
        d_wc_max=0.32,
        de_budget=0.008,
    )

    assert accepted.shape == (2, 2)
    assert summary["provider_fallback_count"] == 0
    expected_debug_keys = {
        "stage4_boundary_smooth_candidate_cap_mm",
        "stage4_boundary_appearance_raw_de",
        "stage4_boundary_appearance_candidate_de",
        "stage4_boundary_appearance_accepted_de",
        "stage4_boundary_appearance_extra_de",
        "stage4_boundary_appearance_bounded_cap_mm",
        "stage4_boundary_appearance_rejected_mm",
        "stage4_boundary_appearance_accept_mask",
        "stage4_boundary_candidate_minus_raw_mm",
        "stage4_boundary_accepted_minus_raw_mm",
    }
    assert expected_debug_keys <= set(debug_maps)
    for key in expected_debug_keys:
        assert debug_maps[key].shape == (2, 2)
        assert debug_maps[key].dtype == np.float32
        assert np.all(np.isfinite(debug_maps[key]))


def test_stage4_appearance_bound_wires_through_requested_cap_maps(monkeypatch):
    color_ceiling = np.zeros((1, 1), dtype=np.float32)
    rows = np.asarray(
        [
            [0.000, 0.0, 0.0],
            [0.004, 0.0, 0.0],
            [0.012, 0.0, 0.0],
            [0.030, 0.0, 0.0],
        ],
        dtype=np.float32,
    )

    class _Config(SimpleNamespace):
        def effective_d_wc_max(self):
            return 0.32

        def effective_boundary_d_wc_max(self):
            return 0.32

    cfg = _Config(
        layer_height=0.08,
        d_wc_min=0.08,
        d_wb=0.20,
        white_base="white",
        white_cap=None,
        cap_mode="appearance_bounded_smooth",
        boundary_cap_de_budget=0.008,
        smooth_kernel=1.0,
        cap_continuity_cleanup=False,
        luminance_handler_enabled=False,
        effective_white_cap=lambda: "white",
    )
    visible_plan = _appearance_bound_visible_plan(shape=(1, 1), cap_oklab_rows=rows)
    visible_plan.implied_cap_height_mm[:] = np.float32(0.08)
    filler_plan = SimpleNamespace(color_ceiling_mm=color_ceiling)
    state = _appearance_bound_state(de_budget=0.008)
    state.config = cfg
    diagnostics = PlanningDiagnosticsStream()

    def fake_smooth_stage4_boundary_cap(*, raw_cap, smoothing_guide, smooth_kernel):
        _ = raw_cap, smoothing_guide, smooth_kernel
        return np.full((1, 1), 0.32, dtype=np.float32)

    monkeypatch.setattr(
        staged_runner,
        "_smooth_stage4_boundary_cap",
        fake_smooth_stage4_boundary_cap,
    )

    requested, _, _ = _requested_stage4_cap_maps(
        state,
        visible_plan,
        filler_plan,
        diagnostics,
    )

    np.testing.assert_allclose(requested, np.asarray([[0.16]], dtype=np.float32))
    assert "stage4_boundary_appearance_bound" in {entry.code for entry in diagnostics.entries}
    assert "stage4_boundary_appearance_bounded_cap_mm" in state.debug_maps


def test_stage4_appearance_bound_skips_luminance_handler(monkeypatch):
    color_ceiling = np.zeros((1, 1), dtype=np.float32)
    rows = np.asarray(
        [
            [0.000, 0.0, 0.0],
            [0.030, 0.0, 0.0],
        ],
        dtype=np.float32,
    )

    class _Config(SimpleNamespace):
        def effective_d_wc_max(self):
            return 0.16

        def effective_boundary_d_wc_max(self):
            return 0.16

    class _FakeLuminanceHandler:
        def __init__(self, cfg, profiles, appearance_provider=None):
            _ = cfg, profiles, appearance_provider

        def build(self, *, target_oklab, shape, raw_implied_cap_mm, color_ceiling_mm):
            _ = target_oklab, raw_implied_cap_mm, color_ceiling_mm
            return SimpleNamespace(
                boundary_cap_request_mm=np.full(shape, 0.08, dtype=np.float32),
                detail_cap_reference_mm=np.full(shape, 0.16, dtype=np.float32),
                reference=SimpleNamespace(boundary_authority_mm=0.16),
                diagnostics={
                    "boundary_request_mean_mm": 0.08,
                    "detail_reference_mean_mm": 0.16,
                },
            )

    cfg = _Config(
        layer_height=0.08,
        d_wc_min=0.08,
        d_wb=0.20,
        white_base="white",
        white_cap=None,
        cap_mode="appearance_bounded_smooth",
        boundary_cap_de_budget=0.008,
        smooth_kernel=0.0,
        cap_continuity_cleanup=False,
        luminance_handler_enabled=True,
        luminance_handler_mode="boundary_prior",
        effective_white_cap=lambda: "white",
    )
    visible_plan = _appearance_bound_visible_plan(shape=(1, 1), cap_oklab_rows=rows)
    filler_plan = SimpleNamespace(color_ceiling_mm=color_ceiling)
    state = _appearance_bound_state(de_budget=0.008)
    state.config = cfg
    state.profiles = SimpleNamespace()
    diagnostics = PlanningDiagnosticsStream()

    monkeypatch.setattr(staged_runner, "luminance_handler_enabled", lambda cfg: True)
    monkeypatch.setattr(staged_runner, "LuminanceHandler", _FakeLuminanceHandler)

    requested, detail_reference, _ = _requested_stage4_cap_maps(
        state,
        visible_plan,
        filler_plan,
        diagnostics,
    )

    np.testing.assert_allclose(requested, np.asarray([[0.08]], dtype=np.float32))
    np.testing.assert_allclose(detail_reference, np.asarray([[0.16]], dtype=np.float32))
    assert "stage4_boundary_appearance_bound_skipped_luminance" in {
        entry.code for entry in diagnostics.entries
    }
    assert "stage4_boundary_appearance_bounded_cap_mm" not in state.debug_maps


def test_luminance_handler_requires_active_photo_stack_provider():
    from pipeline.luminance_handler import LuminanceHandler

    cfg = SimpleNamespace(appearance_model_provider="photo_stack_bundle")

    with pytest.raises(RuntimeError, match="requires the active photo_stack_bundle"):
        LuminanceHandler(cfg, SimpleNamespace(), appearance_provider=None)

    with pytest.raises(RuntimeError, match="requires the active photo_stack_bundle"):
        LuminanceHandler(
            cfg,
            SimpleNamespace(),
            appearance_provider=SimpleNamespace(model_kind="historical_spline"),
        )


def test_luminance_runtime_records_authority_pass_and_provider(monkeypatch):
    from pipeline import luminance_handler as luminance_mod

    reference = luminance_mod.LuminanceReference(
        source_l=np.asarray([[0.1]], dtype=np.float32),
        boundary_l=np.asarray([[0.1]], dtype=np.float32),
        full_luminance_cap_mm=np.asarray([[0.16]], dtype=np.float32),
        boundary_cap_prior_mm=np.asarray([[0.08]], dtype=np.float32),
        boundary_authority_mm=0.16,
        diagnostics={
            "full_cap_mean_mm": 0.16,
            "boundary_cap_prior_mean_mm": 0.08,
        },
    )

    class _FakeRuntimeHandler:
        def __init__(self, cfg, profiles, appearance_provider=None):
            assert getattr(appearance_provider, "model_kind", None) == "photo_stack_bundle"

        def build_reference(self, *, target_oklab, shape):
            assert shape == (1, 1)
            assert target_oklab.shape == (1, 3)
            return reference

    monkeypatch.setattr(luminance_mod, "LuminanceHandler", _FakeRuntimeHandler)
    cfg = SimpleNamespace(luminance_handler_enabled=True)
    state = SimpleNamespace(
        config=cfg,
        profiles=SimpleNamespace(),
        appearance_provider=SimpleNamespace(model_kind="photo_stack_bundle"),
        solve_target_oklab=np.zeros((1, 3), dtype=np.float32),
        debug_maps={},
        diagnostics={},
        preprocessing_metrics={},
    )

    luminance_mod.configure_luminance_handler_runtime(
        state,
        shape=(1, 1),
        authority_pass="final_post_mapping",
    )

    diag = state.diagnostics["__luminance_handler_runtime__"]
    assert diag["target_domain"] == "mapped_solver_target"
    assert diag["authority_pass"] == "final_post_mapping"
    assert diag["provider_kind"] == "photo_stack_bundle"
    assert diag["boundary_authority_mm"] == pytest.approx(0.16)
    assert state.diagnostics["__luminance_handler_runtime_history__"] == [diag]


def test_stage4_structural_split_moves_appearance_residual_to_detail(monkeypatch):
    requested_cap = np.full((2, 2), 0.32, dtype=np.float32)

    config, visible_plan, filler_plan, state, diagnostics, cap_plan = _stage4_split_plan_from_requested_cap(
        monkeypatch,
        requested_cap=requested_cap,
        detail_enabled=True,
        detail_max_layers=20,
        enforce_printability=False,
    )

    boundary_height = cap_plan.cap_boundary_top_mm - filler_plan.color_ceiling_mm
    np.testing.assert_allclose(boundary_height, np.full((2, 2), 0.08, dtype=np.float32))
    np.testing.assert_allclose(cap_plan.detail_height_mm, np.full((2, 2), 0.24, dtype=np.float32))
    np.testing.assert_allclose(cap_plan.cap_height_mm, requested_cap, atol=1e-6)
    np.testing.assert_allclose(
        state.debug_maps["stage4_boundary_structural_cap_mm"],
        np.full((2, 2), 0.08, dtype=np.float32),
    )
    np.testing.assert_allclose(
        state.debug_maps["stage4_detail_residual_from_appearance_target_mm"],
        np.full((2, 2), 0.24, dtype=np.float32),
        atol=1e-6,
    )
    np.testing.assert_allclose(
        state.debug_maps["stage4_final_target_equivalence_delta_mm"],
        np.zeros((2, 2), dtype=np.float32),
        atol=1e-6,
    )
    bundle = build_compatibility_bundle(
        palette=config.palette,
        solver_fine_pitch_mm=float(config.solver_fine_pitch_mm),
        layer_height_mm=float(config.layer_height),
        d_wb_mm=float(config.d_wb),
        d_wc_min_mm=float(config.d_wc_min),
        t_max_mm=float(config.t_max),
        effective_d_wc_max_mm=float(config.effective_d_wc_max()),
        effective_boundary_d_wc_max_mm=float(config.effective_boundary_d_wc_max()),
        luminance_mode="standard",
        cap_mode=str(config.cap_mode),
        visible_plan=visible_plan,
        filler_plan=filler_plan,
        cap_plan=cap_plan,
    )
    _assert_final_visible_white_cap_export_contract(
        SimpleNamespace(
            staged_result=SimpleNamespace(
                compatibility_bundle=bundle,
                cap_plan=cap_plan,
                filler_plan=filler_plan,
            )
        ),
        expected_policy=POLICY_STANDARD_APPEARANCE_BOUNDED_STRUCTURAL_SPLIT,
    )
    target = bundle.export_maps[WHITE_CAP_FIELD_TARGET_UPPER_SURFACE_KEY]
    assert np.count_nonzero(
        target > cap_plan.cap_boundary_top_mm + np.float32(1e-9)
    ) == target.size
    assert "stage4_boundary_structural_split" in {entry.code for entry in diagnostics.entries}


def test_stage4_structural_split_preserves_lateral_shield_without_enforcement(monkeypatch):
    requested_cap = np.full((1, 2), 0.32, dtype=np.float32)
    visible_plan = _stage4_boundary_visible_plan((1, 2))
    visible_plan.mandatory_lateral_boundary_shield_floor_mm = np.asarray(
        [[0.16, 0.0]],
        dtype=np.float32,
    )
    visible_plan.mandatory_lateral_boundary_shield_floor_layer_pixels = 2
    visible_plan.mandatory_lateral_boundary_shield_floor_active_pixels = 1
    visible_plan.mandatory_lateral_boundary_shield_floor_max_layers = 2

    _, _, filler_plan, state, diagnostics, cap_plan = _stage4_split_plan_from_requested_cap(
        monkeypatch,
        requested_cap=requested_cap,
        detail_enabled=False,
        enforce_printability=False,
        visible_plan=visible_plan,
    )

    boundary_height = cap_plan.cap_boundary_top_mm - filler_plan.color_ceiling_mm
    np.testing.assert_allclose(
        boundary_height,
        np.asarray([[0.16, 0.08]], dtype=np.float32),
        atol=1e-6,
    )
    np.testing.assert_allclose(cap_plan.cap_height_mm, boundary_height, atol=1e-6)
    np.testing.assert_allclose(cap_plan.detail_height_mm, np.zeros((1, 2), dtype=np.float32))
    np.testing.assert_allclose(
        state.debug_maps["stage2_lateral_boundary_shield_floor"],
        np.asarray([[0.16, 0.0]], dtype=np.float32),
        atol=1e-6,
    )
    assert "stage2_lateral_boundary_shield_floor_preserved" in {
        entry.code for entry in diagnostics.entries
    }


def test_stage4_structural_split_direct_residual_keeps_tiny_detail(monkeypatch):
    requested_cap = np.asarray([[0.32]], dtype=np.float32)

    _, _, filler_plan, state, _, cap_plan = _stage4_split_plan_from_requested_cap(
        monkeypatch,
        requested_cap=requested_cap,
        detail_enabled=True,
        detail_max_layers=20,
        enforce_printability=False,
    )

    boundary_height = cap_plan.cap_boundary_top_mm - filler_plan.color_ceiling_mm
    np.testing.assert_allclose(boundary_height, np.asarray([[0.08]], dtype=np.float32))
    np.testing.assert_allclose(cap_plan.detail_height_mm, np.asarray([[0.24]], dtype=np.float32))
    np.testing.assert_allclose(cap_plan.cap_height_mm, requested_cap, atol=1e-6)
    assert cap_plan.detail_zone_summary.candidate_pixels == 1
    assert cap_plan.detail_zone_summary.active_pixels == 1
    assert cap_plan.detail_zone_summary.rejected_too_small_zone_count == 0
    assert int(state.debug_maps["stage4_detail_zone_labels"][0, 0]) == 0


def test_stage4_structural_split_layer_budget_redistributes_to_smooth_cap(monkeypatch):
    requested_cap = np.asarray([[0.32]], dtype=np.float32)

    _, _, filler_plan, state, diagnostics, cap_plan = _stage4_split_plan_from_requested_cap(
        monkeypatch,
        requested_cap=requested_cap,
        detail_enabled=True,
        detail_max_layers=1,
        enforce_printability=False,
    )

    boundary_height = cap_plan.cap_boundary_top_mm - filler_plan.color_ceiling_mm
    np.testing.assert_allclose(boundary_height, np.asarray([[0.24]], dtype=np.float32))
    np.testing.assert_allclose(cap_plan.detail_height_mm, np.asarray([[0.08]], dtype=np.float32))
    np.testing.assert_allclose(cap_plan.cap_height_mm, requested_cap, atol=1e-6)
    np.testing.assert_allclose(
        state.debug_maps["stage4_final_target_equivalence_delta_mm"],
        np.zeros((1, 1), dtype=np.float32),
        atol=1e-6,
    )
    np.testing.assert_allclose(
        state.debug_maps["stage4_boundary_smooth_residual_mm"],
        np.asarray([[0.16]], dtype=np.float32),
        atol=1e-6,
    )
    severity_by_code = {entry.code: entry.severity for entry in diagnostics.entries}
    assert severity_by_code["stage4_final_target_equivalence_delta"] == "info"


def test_stage4_structural_split_coverage_redistributes_to_smooth_cap(monkeypatch):
    requested_cap = np.full((2, 2), 0.32, dtype=np.float32)

    _, _, filler_plan, state, diagnostics, cap_plan = _stage4_split_plan_from_requested_cap(
        monkeypatch,
        requested_cap=requested_cap,
        detail_enabled=True,
        detail_max_layers=0,
        enforce_printability=False,
    )

    boundary_height = cap_plan.cap_boundary_top_mm - filler_plan.color_ceiling_mm
    np.testing.assert_allclose(boundary_height, requested_cap, atol=1e-6)
    np.testing.assert_allclose(cap_plan.detail_height_mm, np.zeros((2, 2), dtype=np.float32))
    np.testing.assert_allclose(cap_plan.cap_height_mm, requested_cap, atol=1e-6)
    np.testing.assert_allclose(
        state.debug_maps["stage4_boundary_smooth_residual_mm"],
        np.full((2, 2), 0.24, dtype=np.float32),
        atol=1e-6,
    )
    np.testing.assert_allclose(
        state.debug_maps["stage4_final_target_equivalence_delta_mm"],
        np.zeros((2, 2), dtype=np.float32),
        atol=1e-6,
    )
    severity_by_code = {entry.code: entry.severity for entry in diagnostics.entries}
    assert severity_by_code["stage4_final_target_equivalence_delta"] == "info"


def test_stage4_structural_split_skips_luminance_handler_path(monkeypatch):
    requested_cap = np.asarray([[0.32]], dtype=np.float32)

    _, _, filler_plan, state, diagnostics, cap_plan = _stage4_split_plan_from_requested_cap(
        monkeypatch,
        requested_cap=requested_cap,
        detail_enabled=False,
        enforce_printability=False,
        luminance_enabled=True,
    )

    boundary_height = cap_plan.cap_boundary_top_mm - filler_plan.color_ceiling_mm
    np.testing.assert_allclose(boundary_height, requested_cap, atol=1e-6)
    np.testing.assert_allclose(cap_plan.cap_height_mm, requested_cap, atol=1e-6)
    assert "stage4_boundary_structural_cap_mm" not in state.debug_maps
    assert "stage4_boundary_structural_split" not in {entry.code for entry in diagnostics.entries}


def test_stage4_optical_gain_uses_active_appearance_provider():
    class _Config:
        white_base = "white"
        d_wb = 0.20

        @staticmethod
        def effective_white_cap() -> str:
            return "white"

    class _Provider:
        model_kind = "photo_stack_bundle"

        def __init__(self) -> None:
            self.seen_caps: list[float] = []

        def predict_stack_appearance_linear_rgb_batch(self, requests):
            out = []
            for request in requests:
                cap = float(request.white_cap[1])
                self.seen_caps.append(cap)
                if cap >= 0.40 - 1e-9:
                    out.append([0.36, 0.62, 0.78])
                else:
                    out.append([0.78, 0.18, 0.18])
            return np.asarray(out, dtype=np.float32)

    provider = _Provider()
    target_oklab = to_oklab(
        np.asarray([[[0.36, 0.62, 0.78]]], dtype=np.float32)
    ).reshape(1, 3)
    base_plan = _stage4_boundary_visible_plan((1, 1))
    visible_plan = VisibleRecipeRawGeometryPlan(
        evaluation_shape=base_plan.evaluation_shape,
        evaluation_pitch_mm=base_plan.evaluation_pitch_mm,
        zone_label_map=base_plan.zone_label_map,
        zone_recipe_labels=base_plan.zone_recipe_labels,
        fine_recipe_label_map=base_plan.fine_recipe_label_map,
        recipe_table=(VisibleRecipe.from_mapping({"cyan": 0.20}),),
        base_top_mm=base_plan.base_top_mm,
        raw_color_ceiling_mm=base_plan.raw_color_ceiling_mm,
        implied_cap_height_mm=base_plan.implied_cap_height_mm,
        gamut_mask=base_plan.gamut_mask,
        mapped_target_oklab=target_oklab,
        stage2_objective_summary=base_plan.stage2_objective_summary,
    )
    state = SimpleNamespace(config=_Config(), appearance_provider=provider)

    gain = staged_runner._compute_stage4_detail_optical_gain_map(
        state=state,
        visible_plan=visible_plan,
        boundary_cap_height=np.asarray([[0.20]], dtype=np.float32),
        final_cap_target=np.asarray([[0.40]], dtype=np.float32),
        detail_mask=np.asarray([[True]], dtype=bool),
    )

    np.testing.assert_allclose(provider.seen_caps, [0.20, 0.40], rtol=0.0, atol=1e-6)
    assert float(gain[0, 0]) > 0.1


def test_stage4_predicted_oklab_map_uses_active_appearance_provider():
    class _Config:
        white_base = "white"
        d_wb = 0.20

        @staticmethod
        def effective_white_cap() -> str:
            return "white"

    class _Provider:
        model_kind = "photo_stack_bundle"

        def __init__(self) -> None:
            self.seen_caps: list[float] = []

        def predict_stack_appearance_linear_rgb_batch(self, requests):
            out = []
            for request in requests:
                cap = float(request.white_cap[1])
                self.seen_caps.append(cap)
                out.append([0.20 + cap, 0.40, 0.60])
            return np.asarray(out, dtype=np.float32)

    provider = _Provider()
    base_plan = _stage4_boundary_visible_plan((1, 1))
    visible_plan = VisibleRecipeRawGeometryPlan(
        evaluation_shape=base_plan.evaluation_shape,
        evaluation_pitch_mm=base_plan.evaluation_pitch_mm,
        zone_label_map=base_plan.zone_label_map,
        zone_recipe_labels=base_plan.zone_recipe_labels,
        fine_recipe_label_map=base_plan.fine_recipe_label_map,
        recipe_table=(VisibleRecipe.from_mapping({"cyan": 0.20}),),
        base_top_mm=base_plan.base_top_mm,
        raw_color_ceiling_mm=base_plan.raw_color_ceiling_mm,
        implied_cap_height_mm=base_plan.implied_cap_height_mm,
        gamut_mask=base_plan.gamut_mask,
        mapped_target_oklab=base_plan.mapped_target_oklab,
        stage2_objective_summary=base_plan.stage2_objective_summary,
    )
    state = SimpleNamespace(config=_Config(), appearance_provider=provider)

    predicted = staged_runner._predict_stage4_oklab_map(
        state=state,
        visible_plan=visible_plan,
        cap_height_mm=np.asarray([[0.40]], dtype=np.float32),
    )

    expected = to_oklab(np.asarray([[[0.60, 0.40, 0.60]]], dtype=np.float32))
    np.testing.assert_allclose(provider.seen_caps, [0.40], rtol=0.0, atol=1e-6)
    np.testing.assert_allclose(predicted, expected, rtol=0.0, atol=1e-6)


def test_stage4_provider_path_reuses_stage2_cap_curves_when_available():
    class _Config:
        layer_height = 0.20
        white_base = "white"
        d_wb = 0.20

        @staticmethod
        def effective_white_cap() -> str:
            return "white"

    class _Provider:
        model_kind = "photo_stack_bundle"

        def predict_stack_appearance_linear_rgb_batch(self, requests):  # pragma: no cover
            raise AssertionError("Stage 4 should reuse precomputed Stage 2 cap curves")

    target_rgb = np.asarray([[[0.20, 0.70, 0.80]]], dtype=np.float32)
    target_oklab = to_oklab(target_rgb).reshape(1, 3)
    poor_oklab = to_oklab(np.asarray([[[0.90, 0.10, 0.10]]], dtype=np.float32))[0, 0]
    good_oklab = target_oklab[0]
    base_plan = _stage4_boundary_visible_plan((1, 1))
    visible_plan = VisibleRecipeRawGeometryPlan(
        evaluation_shape=base_plan.evaluation_shape,
        evaluation_pitch_mm=base_plan.evaluation_pitch_mm,
        zone_label_map=base_plan.zone_label_map,
        zone_recipe_labels=base_plan.zone_recipe_labels,
        fine_recipe_label_map=base_plan.fine_recipe_label_map,
        recipe_table=(VisibleRecipe.from_mapping({"cyan": 0.20}),),
        base_top_mm=base_plan.base_top_mm,
        raw_color_ceiling_mm=base_plan.raw_color_ceiling_mm,
        implied_cap_height_mm=base_plan.implied_cap_height_mm,
        gamut_mask=base_plan.gamut_mask,
        mapped_target_oklab=target_oklab,
        stage2_objective_summary=base_plan.stage2_objective_summary,
        recipe_stack_ids=np.asarray([0], dtype=np.int32),
        stage2_cap_values_mm=np.asarray([0.20, 0.40], dtype=np.float32),
        stage2_stack_cap_oklab=np.asarray(
            [[[poor_oklab[0], poor_oklab[1], poor_oklab[2]], good_oklab]],
            dtype=np.float32,
        ),
    )
    state = SimpleNamespace(config=_Config(), appearance_provider=_Provider())

    gain = staged_runner._compute_stage4_detail_optical_gain_map(
        state=state,
        visible_plan=visible_plan,
        boundary_cap_height=np.asarray([[0.20]], dtype=np.float32),
        final_cap_target=np.asarray([[0.40]], dtype=np.float32),
        detail_mask=np.asarray([[True]], dtype=bool),
    )

    assert float(gain[0, 0]) > 0.1


def test_recipe_materialization_preserves_supplied_filament_order():
    _zone_labels, _fine_labels, recipes, recipe_stack_ids = staged_runner._materialize_recipe_assignments(
        zone_selected_stack_ids=np.asarray([0], dtype=np.int32),
        fine_stack_id_map=np.asarray([[0]], dtype=np.int32),
        unique_stack_dicts={0: {"b-filament": 0.2, "a-filament": 0.1}},
        filament_order=("b-filament", "a-filament"),
    )

    assert recipes[0].thickness_by_filament == (
        ("b-filament", 0.2),
        ("a-filament", 0.1),
    )
    np.testing.assert_array_equal(recipe_stack_ids, np.asarray([0], dtype=np.int32))


def test_stage2_stack_ids_preserve_photo_stack_palette_order():
    thickness_result = {
        "upper-blue": np.asarray([0.16], dtype=np.float32),
        "lower-yellow": np.asarray([0.08], dtype=np.float32),
    }

    _ids, _codes, stacks = _vectorized_stack_ids(
        thickness_result,
        ["upper-blue", "lower-yellow"],
        layer_height=0.08,
        max_layers=8,
    )

    assert tuple(stacks[0].items()) == (
        ("upper-blue", 0.16),
        ("lower-yellow", 0.08),
    )


def test_stage4_boundary_cap_printability_gate_grows_tiny_top_layer():
    boundary_cap = np.full((3, 3), 0.08, dtype=np.float32)
    boundary_cap[1, 1] = 0.16

    result = _apply_stage4_boundary_cap_printability_gate(
        boundary_cap_height_mm=boundary_cap,
        settings=_stage4_printability_settings(),
    )

    expected = np.full((3, 3), 0.16, dtype=np.float32)
    assert result.summary.enabled is True
    assert result.summary.flagged_layer_pixels == 1
    assert result.summary.flagged_components == 1
    assert result.summary.grown_layer_pixels == 8
    assert result.summary.grown_components == 1
    assert result.summary.suppressed_optional_layer_pixels == 0
    assert result.summary.rejected_tiny_components == 1
    assert result.summary.rejected_narrow_components == 1
    assert result.summary.rejected_short_components == 1
    np.testing.assert_array_equal(result.boundary_cap_height_mm, expected)
    assert int(result.rejection_map[1, 1]) == 7


def test_stage4_boundary_cap_printability_gate_checks_absolute_layers():
    settings = _stage4_printability_settings()
    boundary_cap = np.full((3, 3), 0.08, dtype=np.float32)
    color_ceiling = np.full((3, 3), 0.20, dtype=np.float32)
    color_ceiling[2, 2] = np.float32(0.28)

    result = _apply_stage4_boundary_cap_printability_gate(
        boundary_cap_height_mm=boundary_cap,
        color_ceiling_mm=color_ceiling,
        settings=settings,
    )

    expected = np.full((3, 3), 0.08, dtype=np.float32)
    expected[2, 2] = np.float32(0.0)
    assert result.summary.flagged_components == 1
    assert result.summary.flagged_layer_pixels == 1
    assert result.summary.suppressed_optional_components == 1
    assert result.summary.suppressed_optional_layer_pixels == 1
    np.testing.assert_array_equal(result.boundary_cap_height_mm, expected)
    assert int(result.rejection_map[2, 2]) == 7


def test_stage4_boundary_cap_printability_gate_preserves_mandatory_floor():
    settings = _stage4_printability_settings()
    boundary_cap = np.full((3, 3), 0.08, dtype=np.float32)
    color_ceiling = np.full((3, 3), 0.20, dtype=np.float32)
    color_ceiling[2, 2] = np.float32(0.28)

    result = _apply_stage4_boundary_cap_printability_gate(
        boundary_cap_height_mm=boundary_cap,
        color_ceiling_mm=color_ceiling,
        settings=settings,
        minimum_boundary_cap_height_mm=0.08,
    )

    assert result.summary.flagged_components == 1
    assert result.summary.flagged_layer_pixels == 1
    assert result.summary.preserved_mandatory_components == 1
    assert result.summary.preserved_mandatory_layer_pixels == 1
    assert result.summary.suppressed_optional_layer_pixels == 0
    np.testing.assert_array_equal(result.boundary_cap_height_mm, boundary_cap)
    assert int(result.rejection_map[2, 2]) == 7


def test_stage4_boundary_cap_printability_gate_diagnostic_only_preserves_cap():
    settings = _stage4_printability_settings()
    boundary_cap = np.full((3, 3), 0.08, dtype=np.float32)
    color_ceiling = np.full((3, 3), 0.20, dtype=np.float32)
    color_ceiling[2, 2] = np.float32(0.28)

    result = _apply_stage4_boundary_cap_printability_gate(
        boundary_cap_height_mm=boundary_cap,
        color_ceiling_mm=color_ceiling,
        settings=settings,
        apply_changes=False,
    )

    assert result.summary.flagged_components == 1
    assert result.summary.flagged_layer_pixels == 1
    assert result.summary.suppressed_optional_layer_pixels == 0
    np.testing.assert_array_equal(result.boundary_cap_height_mm, boundary_cap)
    assert int(result.rejection_map[2, 2]) == 7


def test_stage4_boundary_cap_enforce_off_preserves_output_but_diagnostic_reports(monkeypatch):
    requested_cap = np.full((3, 3), 0.08, dtype=np.float32)
    requested_cap[1, 1] = 0.16

    config, visible_plan, filler_plan, _, cap_plan = _stage4_boundary_plan_from_requested_cap(
        monkeypatch,
        requested_cap=requested_cap,
        enforce_printability=False,
    )
    boundary_cap_height = cap_plan.cap_boundary_top_mm - np.float32(0.20)
    np.testing.assert_allclose(boundary_cap_height, requested_cap, atol=1e-7)
    assert cap_plan.boundary_cap_printability_summary is not None
    assert cap_plan.boundary_cap_printability_summary.enabled is False

    diagnostic = run_blueprint_printability_diagnostic(
        build_layered_blueprint_view(
            visible_plan=visible_plan,
            cap_plan=cap_plan,
            palette_order=config.palette,
            d_wb_mm=float(config.d_wb),
            layer_height_mm=float(config.layer_height),
        ),
        _stage4_printability_settings(),
    )

    assert diagnostic.cap_hard_fail_pixels == 1
    assert diagnostic.cap_hard_fail_map[1, 1] == 1.0
    assert diagnostic.detail_hard_fail_pixels == 0
    cap_plan.blueprint_printability_diagnostic = diagnostic
    bundle = build_compatibility_bundle(
        palette=config.palette,
        solver_fine_pitch_mm=float(config.solver_fine_pitch_mm),
        layer_height_mm=float(config.layer_height),
        d_wb_mm=float(config.d_wb),
        d_wc_min_mm=float(config.d_wc_min),
        t_max_mm=float(config.t_max),
        effective_d_wc_max_mm=float(config.effective_d_wc_max()),
        effective_boundary_d_wc_max_mm=float(config.effective_boundary_d_wc_max()),
        luminance_mode="standard",
        cap_mode=str(config.cap_mode),
        visible_plan=visible_plan,
        filler_plan=filler_plan,
        cap_plan=cap_plan,
    )
    assert "blueprint_printability_boundary_cap_hard_fail" in bundle.debug_maps
    assert bundle.debug_maps["blueprint_printability_boundary_cap_hard_fail"][1, 1] == 1.0
    assert "white_cap_field_target_upper_surface_map" in bundle.export_maps
    assert bundle.export_metadata["physical_geometry"]["d_wb_mm"] == float(config.d_wb)


def test_stage4_boundary_cap_enforce_on_repairs_cap_hard_fail(monkeypatch):
    requested_cap = np.full((3, 3), 0.08, dtype=np.float32)
    requested_cap[1, 1] = 0.16

    config, visible_plan, filler_plan, diagnostics, cap_plan = _stage4_boundary_plan_from_requested_cap(
        monkeypatch,
        requested_cap=requested_cap,
        enforce_printability=True,
    )

    boundary_cap_height = cap_plan.cap_boundary_top_mm - np.float32(0.20)
    np.testing.assert_allclose(
        boundary_cap_height,
        np.full((3, 3), 0.16, dtype=np.float32),
        atol=1e-7,
    )
    assert cap_plan.boundary_cap_printability_summary is not None
    assert cap_plan.boundary_cap_printability_summary.enabled is True
    assert cap_plan.boundary_cap_printability_summary.flagged_layer_pixels == 1
    assert cap_plan.boundary_cap_printability_summary.grown_layer_pixels == 8
    assert cap_plan.boundary_cap_printability_repair_map is not None
    assert int(cap_plan.boundary_cap_printability_repair_map[1, 1]) == 7
    assert "stage4_boundary_cap_printability_gate" in {
        entry.code for entry in diagnostics.entries
    }

    diagnostic = run_blueprint_printability_diagnostic(
        build_layered_blueprint_view(
            visible_plan=visible_plan,
            cap_plan=cap_plan,
            palette_order=config.palette,
            d_wb_mm=float(config.d_wb),
            layer_height_mm=float(config.layer_height),
        ),
        _stage4_printability_settings(),
    )
    assert diagnostic.cap_hard_fail_pixels == 0
    bundle = build_compatibility_bundle(
        palette=config.palette,
        solver_fine_pitch_mm=float(config.solver_fine_pitch_mm),
        layer_height_mm=float(config.layer_height),
        d_wb_mm=float(config.d_wb),
        d_wc_min_mm=float(config.d_wc_min),
        t_max_mm=float(config.t_max),
        effective_d_wc_max_mm=float(config.effective_d_wc_max()),
        effective_boundary_d_wc_max_mm=float(config.effective_boundary_d_wc_max()),
        luminance_mode="standard",
        cap_mode=str(config.cap_mode),
        visible_plan=visible_plan,
        filler_plan=filler_plan,
        cap_plan=cap_plan,
    )
    assert "stage4_boundary_cap_printability_repairs" in bundle.debug_maps
    assert bundle.debug_maps["stage4_boundary_cap_printability_repairs"][1, 1] == 7.0


def test_stage4_detail_printability_gate_removes_tiny_top_detail():
    settings = BlueprintPrintabilitySettings(
        minimum_extrusion_width_mm=0.40,
        minimum_line_length_mm=0.50,
        pitch_mm=0.20,
        layer_height_mm=0.08,
    )

    result = _apply_stage4_detail_printability_gate(
        detail_height_mm=np.array(
            [
                [0.08, 0.0, 0.0],
                [0.0, 0.0, 0.0],
                [0.0, 0.0, 0.0],
            ],
            dtype=np.float32,
        ),
        settings=settings,
    )

    assert result.summary.enabled is True
    assert result.summary.suppressed_layer_pixels == 1
    assert result.summary.suppressed_components == 1
    assert result.summary.rejected_tiny_components == 1
    assert result.summary.rejected_narrow_components == 1
    assert result.summary.rejected_short_components == 1
    np.testing.assert_array_equal(result.detail_height_mm, np.zeros((3, 3), dtype=np.float32))
    assert int(result.rejection_map[0, 0]) == 7


def test_stage4_detail_printability_gate_keeps_printable_detail_block():
    settings = BlueprintPrintabilitySettings(
        minimum_extrusion_width_mm=0.40,
        minimum_line_length_mm=0.50,
        pitch_mm=0.20,
        layer_height_mm=0.08,
    )
    detail = np.zeros((4, 4), dtype=np.float32)
    detail[1:3, 0:3] = 0.08

    result = _apply_stage4_detail_printability_gate(
        detail_height_mm=detail,
        settings=settings,
    )

    assert result.summary.suppressed_components == 0
    assert result.summary.accepted_components == 1
    np.testing.assert_array_equal(result.detail_height_mm, detail)
    assert int(np.count_nonzero(result.rejection_map)) == 0


def test_stage4_detail_printability_gate_removes_only_unprintable_top_layer():
    settings = BlueprintPrintabilitySettings(
        minimum_extrusion_width_mm=0.40,
        minimum_line_length_mm=0.50,
        pitch_mm=0.20,
        layer_height_mm=0.08,
    )
    detail = np.zeros((4, 4), dtype=np.float32)
    detail[1:3, 0:3] = 0.08
    detail[1, 1] = 0.16

    result = _apply_stage4_detail_printability_gate(
        detail_height_mm=detail,
        settings=settings,
    )

    expected = np.zeros((4, 4), dtype=np.float32)
    expected[1:3, 0:3] = 0.08
    assert result.summary.suppressed_layer_pixels == 1
    assert result.summary.suppressed_components == 1
    np.testing.assert_array_equal(result.detail_height_mm, expected)


def test_stage4_detail_printability_gate_checks_absolute_detail_layers():
    settings = _stage4_printability_settings()
    detail = np.full((2, 3), 0.08, dtype=np.float32)
    base_top = np.zeros((2, 3), dtype=np.float32)
    base_top[:, 2] = np.float32(0.08)

    relative_result = _apply_stage4_detail_printability_gate(
        detail_height_mm=detail,
        settings=settings,
    )
    absolute_result = _apply_stage4_detail_printability_gate(
        detail_height_mm=detail,
        settings=settings,
        base_top_mm=base_top,
    )

    assert relative_result.summary.suppressed_layer_pixels == 0
    np.testing.assert_array_equal(relative_result.detail_height_mm, detail)
    assert absolute_result.summary.suppressed_layer_pixels == 6
    np.testing.assert_array_equal(
        absolute_result.detail_height_mm,
        np.zeros_like(detail, dtype=np.float32),
    )


def test_stage4_detail_printability_gate_counts_boundary_as_white_support():
    settings = _stage4_printability_settings()
    detail = np.zeros((3, 3), dtype=np.float32)
    detail[1, 1] = np.float32(0.16)
    color_ceiling = np.zeros((3, 3), dtype=np.float32)
    boundary = np.full((3, 3), 0.16, dtype=np.float32)
    boundary[1, 1] = np.float32(0.0)

    detail_only = _apply_stage4_detail_printability_gate(
        detail_height_mm=detail,
        settings=settings,
        base_top_mm=color_ceiling + boundary,
    )
    unified_white = _apply_stage4_detail_printability_gate(
        detail_height_mm=detail,
        settings=settings,
        base_top_mm=color_ceiling + boundary,
        color_ceiling_mm=color_ceiling,
        boundary_cap_height_mm=boundary,
    )

    assert detail_only.summary.suppressed_layer_pixels == 2
    np.testing.assert_array_equal(
        detail_only.detail_height_mm,
        np.zeros_like(detail, dtype=np.float32),
    )
    assert unified_white.summary.suppressed_layer_pixels == 0
    assert unified_white.summary.accepted_components == 2
    np.testing.assert_array_equal(unified_white.detail_height_mm, detail)
    assert int(np.count_nonzero(unified_white.rejection_map)) == 0


def test_stage4_detail_printability_gate_still_removes_unsupported_detail():
    settings = _stage4_printability_settings()
    detail = np.zeros((3, 3), dtype=np.float32)
    detail[1, 1] = np.float32(0.16)
    color_ceiling = np.zeros((3, 3), dtype=np.float32)
    boundary = np.zeros((3, 3), dtype=np.float32)

    result = _apply_stage4_detail_printability_gate(
        detail_height_mm=detail,
        settings=settings,
        base_top_mm=color_ceiling + boundary,
        color_ceiling_mm=color_ceiling,
        boundary_cap_height_mm=boundary,
    )

    assert result.summary.suppressed_layer_pixels == 2
    np.testing.assert_array_equal(
        result.detail_height_mm,
        np.zeros_like(detail, dtype=np.float32),
    )
    assert int(result.rejection_map[1, 1]) != 0


def test_stage4_detail_printability_gate_rejects_inconsistent_unified_base():
    settings = _stage4_printability_settings()
    detail = np.full((2, 2), 0.08, dtype=np.float32)
    color_ceiling = np.zeros((2, 2), dtype=np.float32)
    boundary = np.zeros((2, 2), dtype=np.float32)
    base_top = np.full((2, 2), 0.08, dtype=np.float32)

    with pytest.raises(ValueError, match="base_top_mm must equal"):
        _apply_stage4_detail_printability_gate(
            detail_height_mm=detail,
            settings=settings,
            base_top_mm=base_top,
            color_ceiling_mm=color_ceiling,
            boundary_cap_height_mm=boundary,
        )


def test_stage4_luminance_detail_authoring_gate_prevents_final_cleanup():
    settings = _stage4_printability_settings()
    detail = np.full((2, 3), 0.08, dtype=np.float32)
    color_ceiling = np.zeros((2, 3), dtype=np.float32)
    color_ceiling[:, 2] = np.float32(0.08)
    boundary = np.zeros((2, 3), dtype=np.float32)
    remaining_budget = np.full((2, 3), 0.08, dtype=np.float32)

    authored = _apply_stage4_luminance_detail_authoring_printability(
        detail_height_mm=detail,
        settings=settings,
        color_ceiling_mm=color_ceiling,
        boundary_cap_height_mm=boundary,
        remaining_cap_budget_mm=remaining_budget,
    )
    final_gate = _apply_stage4_detail_printability_gate(
        detail_height_mm=authored.detail_height_mm,
        settings=settings,
        base_top_mm=color_ceiling + boundary,
    )

    assert authored.summary.enabled is True
    assert authored.summary.requested_layer_pixels_before == 6
    assert authored.summary.requested_layer_pixels_after == 0
    assert authored.summary.prevented_layer_pixels == 6
    assert authored.summary.prevented_active_pixels == 6
    assert int(np.count_nonzero(authored.rejection_map)) == 6
    assert final_gate.summary.suppressed_layer_pixels == 0
    np.testing.assert_array_equal(
        authored.detail_height_mm,
        np.zeros_like(detail, dtype=np.float32),
    )


def test_stage4_luminance_detail_authoring_counts_boundary_as_white_support():
    settings = _stage4_printability_settings()
    detail = np.zeros((3, 3), dtype=np.float32)
    detail[1, 1] = np.float32(0.16)
    color_ceiling = np.zeros((3, 3), dtype=np.float32)
    boundary = np.full((3, 3), 0.24, dtype=np.float32)
    boundary[1, 1] = np.float32(0.08)
    remaining_budget = (boundary + detail).astype(np.float32, copy=False)

    authored = _apply_stage4_luminance_detail_authoring_printability(
        detail_height_mm=detail,
        settings=settings,
        color_ceiling_mm=color_ceiling,
        boundary_cap_height_mm=boundary,
        remaining_cap_budget_mm=remaining_budget,
    )

    assert authored.summary.enabled is True
    assert authored.summary.requested_layer_pixels_before == 2
    assert authored.summary.requested_layer_pixels_after == 2
    assert authored.summary.prevented_layer_pixels == 0
    assert authored.summary.prevented_active_pixels == 0
    np.testing.assert_array_equal(authored.detail_height_mm, detail)
    assert int(np.count_nonzero(authored.rejection_map)) == 0


def test_stage4_luminance_authoring_flag_moves_detail_cleanup_earlier(monkeypatch):
    detail = np.full((2, 3), 0.08, dtype=np.float32)
    color_ceiling = np.full((2, 3), 0.20, dtype=np.float32)
    color_ceiling[:, 2] = np.float32(0.28)

    def run_case(*, authoring_mode: str):
        config = SolveConfig(
            palette=["a"],
            white_base="white",
            luminance_handler_enabled=True,
            luminance_detail_authoring_printability=authoring_mode,
            enforce_printability=True,
            emit_blueprint_printability=True,
            printability_minimum_extrusion_width_mm=0.40,
            printability_minimum_line_length_mm=0.50,
            solver_fine_pitch_mm=0.20,
            image_sample_pitch_mm=0.20,
            layer_height=0.08,
            d_wb=0.20,
            d_wc_min=0.0,
            d_wc_max=0.16,
            t_max=0.60,
            detail_cap_enabled=True,
            detail_cap_max_layers=1,
        )
        visible_plan = _stage4_boundary_visible_plan(tuple(detail.shape))  # type: ignore[arg-type]
        filler_plan = FillerGeometryPlan(
            raw_color_ceiling_mm=color_ceiling.copy(),
            filler_height_mm=np.zeros_like(detail, dtype=np.float32),
            color_ceiling_mm=color_ceiling.copy(),
        )

        def fake_requested_stage4_cap_maps(state, visible_plan, filler_plan, diagnostics):
            _ = state, visible_plan, filler_plan, diagnostics
            return (
                np.zeros_like(detail, dtype=np.float32),
                detail.copy(),
                np.zeros_like(detail, dtype=np.float32),
            )

        def fake_optical_detail_surface(
            *,
            state,
            visible_plan,
            boundary_cap_height,
            remaining_cap_budget,
            max_layers=None,
        ):
            _ = state, visible_plan, boundary_cap_height, remaining_cap_budget, max_layers
            return detail.copy(), np.ones_like(detail, dtype=np.float32)

        def fake_optical_gain_map(
            *,
            state,
            visible_plan,
            boundary_cap_height,
            final_cap_target,
            detail_mask,
        ):
            _ = state, visible_plan, boundary_cap_height, final_cap_target
            return np.asarray(detail_mask, dtype=np.float32)

        monkeypatch.setattr(
            staged_runner,
            "_requested_stage4_cap_maps",
            fake_requested_stage4_cap_maps,
        )
        monkeypatch.setattr(
            staged_runner,
            "_build_stage4_optical_detail_surface",
            fake_optical_detail_surface,
        )
        monkeypatch.setattr(
            staged_runner,
            "_compute_stage4_detail_optical_gain_map",
            fake_optical_gain_map,
        )
        diagnostics = PlanningDiagnosticsStream()
        return staged_runner._build_stage4_cap_plan(
            SimpleNamespace(config=config),
            visible_plan,
            filler_plan,
            diagnostics,
        )

    disabled = run_case(authoring_mode="off")
    enabled = run_case(authoring_mode="absolute_finalgate")

    assert disabled.detail_authoring_printability_summary is not None
    assert disabled.detail_authoring_printability_summary.enabled is False
    assert disabled.detail_printability_summary is not None
    assert disabled.detail_printability_summary.suppressed_layer_pixels == 6

    assert enabled.detail_authoring_printability_summary is not None
    assert enabled.detail_authoring_printability_summary.enabled is True
    assert enabled.detail_authoring_printability_summary.prevented_layer_pixels == 6
    assert enabled.detail_printability_summary is not None
    assert enabled.detail_printability_summary.suppressed_layer_pixels == 0
    np.testing.assert_array_equal(disabled.detail_height_mm, enabled.detail_height_mm)


def test_stage4_luminance_uses_layer_limited_optical_detail(monkeypatch):
    shape = (6, 6)
    boundary = np.full(shape, 0.08, dtype=np.float32)
    zero_edge_guard = np.zeros(shape, dtype=np.float32)

    def run_case(*, max_layers: int) -> np.ndarray:
        config = SolveConfig(
            palette=["a"],
            white_base="white",
            luminance_handler_enabled=True,
            cap_mode="smooth_variable",
            enforce_printability=False,
            solver_fine_pitch_mm=0.20,
            image_sample_pitch_mm=0.20,
            layer_height=0.08,
            d_wb=0.20,
            d_wc_min=0.08,
            d_wc_max=0.40,
            t_max=1.00,
            detail_cap_enabled=True,
            detail_cap_max_layers=max_layers,
        )
        visible_plan = _stage4_boundary_visible_plan(shape)
        filler_plan = FillerGeometryPlan(
            raw_color_ceiling_mm=np.full(shape, 0.20, dtype=np.float32),
            filler_height_mm=np.zeros(shape, dtype=np.float32),
            color_ceiling_mm=np.full(shape, 0.20, dtype=np.float32),
        )

        def fake_requested_stage4_cap_maps(state, visible_plan, filler_plan, diagnostics):
            _ = visible_plan, filler_plan, diagnostics
            return boundary.copy(), boundary.copy(), zero_edge_guard.copy()

        def fake_optical_detail_surface(
            *,
            state,
            visible_plan,
            boundary_cap_height,
            remaining_cap_budget,
            max_layers=None,
        ):
            _ = state, visible_plan, boundary_cap_height, remaining_cap_budget, max_layers
            requested = np.full(shape, 0.40, dtype=np.float32)
            if max_layers is not None:
                requested = np.minimum(requested, np.float32(max_layers) * np.float32(0.08))
            return requested, np.ones(shape, dtype=np.float32)

        def fake_optical_gain_map(
            *,
            state,
            visible_plan,
            boundary_cap_height,
            final_cap_target,
            detail_mask,
        ):
            _ = state, visible_plan, boundary_cap_height, final_cap_target
            return np.asarray(detail_mask, dtype=np.float32)

        monkeypatch.setattr(
            staged_runner,
            "_requested_stage4_cap_maps",
            fake_requested_stage4_cap_maps,
        )
        monkeypatch.setattr(
            staged_runner,
            "_build_stage4_optical_detail_surface",
            fake_optical_detail_surface,
        )
        monkeypatch.setattr(
            staged_runner,
            "_compute_stage4_detail_optical_gain_map",
            fake_optical_gain_map,
        )
        monkeypatch.setattr(staged_runner, "luminance_handler_enabled", lambda cfg: True)
        state = SimpleNamespace(config=config, debug_maps={})
        cap_plan = staged_runner._build_stage4_cap_plan(
            state,
            visible_plan,
            filler_plan,
            PlanningDiagnosticsStream(),
        )
        return cap_plan.detail_height_mm

    no_layers = run_case(max_layers=0)
    two_layers = run_case(max_layers=2)
    five_layers = run_case(max_layers=5)

    assert float(np.max(no_layers)) == pytest.approx(0.0)
    assert float(np.max(two_layers)) == pytest.approx(0.16)
    assert float(np.max(five_layers)) == pytest.approx(0.32)


def _stage4_dumbbell_mask() -> np.ndarray:
    """Two 2x2 lobes connected by a 1-pixel-wide bridge.

    At pitch 0.20 mm and minimum extrusion width 0.40 mm, the bbox-based
    grader passes (width 0.40 mm, length 1.40 mm, area 0.44 mm^2), but a 2x2
    morphological opening loses the 1-pixel-wide bridge — the same hard-fail
    criterion the diagnostic applies.
    """
    mask = np.zeros((2, 7), dtype=bool)
    mask[:, 0:2] = True
    mask[:, 5:7] = True
    mask[1, 2:5] = True
    return mask


def _stage4_nonstructural_opening_loss_mask() -> np.ndarray:
    """Shape with raw opening loss that does not split/destroy the component."""
    return np.array(
        [
            [0, 0, 0, 0, 0, 1, 1, 0, 0, 0, 0],
            [0, 0, 0, 0, 0, 1, 1, 0, 0, 0, 0],
            [0, 0, 0, 0, 1, 1, 1, 0, 0, 0, 0],
            [0, 0, 0, 0, 1, 1, 1, 0, 0, 0, 0],
            [0, 0, 0, 1, 1, 1, 1, 0, 0, 0, 0],
            [0, 0, 1, 1, 1, 0, 1, 1, 0, 0, 0],
            [0, 0, 1, 1, 0, 0, 1, 1, 0, 0, 0],
            [0, 0, 0, 0, 0, 0, 0, 1, 1, 0, 0],
            [0, 0, 0, 0, 0, 0, 0, 1, 1, 0, 0],
            [0, 0, 0, 0, 0, 0, 0, 1, 1, 0, 0],
            [0, 0, 0, 0, 0, 0, 0, 1, 1, 0, 0],
        ],
        dtype=bool,
    )


def test_stage4_printability_dumbbell_diagnostic_reports_width_loss():
    """Sanity check: the shared opening-width helpers flag the dumbbell as a
    hard fail with width loss.  The Stage 4 gates must mirror this."""
    settings = _stage4_printability_settings()
    mask = _stage4_dumbbell_mask()

    structure = opening_width_structure(settings)
    width_loss = opening_width_loss(mask, structure=structure)

    assert int(np.count_nonzero(width_loss)) == 3
    # Bbox-only grading would call this a pass; the opening test must fail it.
    from pipeline.staged_printability import grade_blueprint_component
    bbox_grade, _, _, _, _ = grade_blueprint_component(
        pixel_count=int(np.count_nonzero(mask)),
        height_px=int(mask.shape[0]),
        width_px=int(mask.shape[1]),
        settings=settings,
    )
    assert bbox_grade != "hard_fail"


def test_stage4_printability_gates_ignore_nonstructural_opening_loss():
    settings = _stage4_printability_settings()
    mask = _stage4_nonstructural_opening_loss_mask()
    layer_height = np.where(mask, np.float32(0.08), np.float32(0.0)).astype(np.float32)

    cap_result = _apply_stage4_boundary_cap_printability_gate(
        boundary_cap_height_mm=layer_height,
        settings=settings,
    )
    detail_result = _apply_stage4_detail_printability_gate(
        detail_height_mm=layer_height,
        settings=settings,
    )

    assert cap_result.summary.flagged_components == 0
    assert cap_result.summary.flagged_layer_pixels == 0
    np.testing.assert_array_equal(cap_result.boundary_cap_height_mm, layer_height)
    assert int(np.count_nonzero(cap_result.rejection_map)) == 0

    assert detail_result.summary.suppressed_components == 0
    assert detail_result.summary.suppressed_layer_pixels == 0
    np.testing.assert_array_equal(detail_result.detail_height_mm, layer_height)
    assert int(np.count_nonzero(detail_result.rejection_map)) == 0


def test_stage4_boundary_cap_printability_gate_grows_dumbbell_neck():
    """Boundary-cap dumbbell/neck regression: the gate must catch a component
    whose bbox passes but whose 1-pixel-wide bridge fails the same opening
    test the blueprint diagnostic uses (`_opening_width_loss`).  Boundary cap
    should first grow nearby white material into a printable footprint instead
    of punching a hole in the cap."""
    settings = _stage4_printability_settings()
    mask = _stage4_dumbbell_mask()
    boundary_cap = np.where(
        mask, np.float32(0.08), np.float32(0.0)
    ).astype(np.float32)

    result = _apply_stage4_boundary_cap_printability_gate(
        boundary_cap_height_mm=boundary_cap,
        settings=settings,
    )

    assert result.summary.enabled is True
    assert result.summary.flagged_components == 1
    assert result.summary.flagged_layer_pixels == 3
    assert result.summary.grown_components == 1
    assert result.summary.suppressed_optional_layer_pixels == 0
    assert result.summary.rejected_narrow_components == 1
    np.testing.assert_array_equal(
        result.boundary_cap_height_mm,
        np.full_like(boundary_cap, np.float32(0.08)),
    )
    # The narrow-width reason is localized to the bridge pixels, not the lobes.
    rejection_pixels = result.rejection_map[mask]
    # Reason bits are an OR-mask; the narrow_width bit must be present.
    from pipeline.staged_runner import _stage2_printability_reason_bits
    narrow_bit = _stage2_printability_reason_bits(("narrow_width",))
    assert int(np.count_nonzero(rejection_pixels & narrow_bit)) == 3


def test_stage4_detail_printability_gate_rejects_dumbbell_neck():
    """Detail dumbbell/neck regression — same bug, same fix."""
    settings = _stage4_printability_settings()
    mask = _stage4_dumbbell_mask()
    detail = np.where(mask, np.float32(0.08), np.float32(0.0)).astype(np.float32)

    result = _apply_stage4_detail_printability_gate(
        detail_height_mm=detail,
        settings=settings,
    )

    assert result.summary.enabled is True
    assert result.summary.suppressed_components == 3
    assert result.summary.suppressed_layer_pixels == int(np.count_nonzero(mask))
    assert result.summary.rejected_narrow_components == 1
    np.testing.assert_array_equal(
        result.detail_height_mm,
        np.zeros_like(detail),
    )
    from pipeline.staged_runner import _stage2_printability_reason_bits
    narrow_bit = _stage2_printability_reason_bits(("narrow_width",))
    rejection_pixels = result.rejection_map[mask]
    assert int(np.count_nonzero(rejection_pixels & narrow_bit)) == 3


def test_stage2_recipe_pressure_splits_coarse_pruning_and_local_gaps():
    all_oklabs = np.full((3, 1, 3), np.nan, dtype=np.float32)
    all_oklabs[0, 0] = np.array([0.0, 0.0, 0.0], dtype=np.float32)
    all_oklabs[1, 0] = np.array([0.5, 0.0, 0.0], dtype=np.float32)
    all_oklabs[2, 0] = np.array([1.0, 0.0, 0.0], dtype=np.float32)
    targets = np.tile(np.array([[1.0, 0.0, 0.0]], dtype=np.float32), (4, 1))

    pressure = _compute_stage2_recipe_pressure(
        fine_shape=(2, 2),
        coarse_to_fine_scale=2,
        zone_label_map=np.zeros((2, 2), dtype=np.int32),
        zone_flat_indices=(np.array([0, 1, 2, 3], dtype=np.int32),),
        targets=targets,
        pixel_stack_ids=np.array([2, 2, 2, 2], dtype=np.int32),
        preprune_candidate_sets=(
            _ZoneCandidateSet(
                candidate_ids=np.array([0, 1, 2], dtype=np.int32),
                local_scores=np.array([1.0, 0.5, 0.0], dtype=np.float32),
                total_thickness_mm=np.array([0.0, 0.1, 0.2], dtype=np.float32),
            ),
        ),
        pruned_candidate_sets=(
            _ZoneCandidateSet(
                candidate_ids=np.array([0, 1], dtype=np.int32),
                local_scores=np.array([1.0, 0.5], dtype=np.float32),
                total_thickness_mm=np.array([0.0, 0.1], dtype=np.float32),
            ),
        ),
        optimization=SimpleNamespace(selected_stack_ids=np.array([0], dtype=np.int32)),
        all_oklabs=all_oklabs,
        frontier_config_hash="test",
    )

    np.testing.assert_allclose(pressure.coarse_excess, np.full((2, 2), 0.5, dtype=np.float32))
    np.testing.assert_allclose(pressure.pruning_gap, np.full((2, 2), 0.5, dtype=np.float32))
    np.testing.assert_allclose(pressure.local_gap, np.zeros((2, 2), dtype=np.float32))
    np.testing.assert_allclose(pressure.total_excess, np.full((2, 2), 1.0, dtype=np.float32))
    np.testing.assert_array_equal(pressure.frontier_best_stack_id, np.full((2, 2), 1, dtype=np.int32))
    np.testing.assert_array_equal(pressure.preprune_best_stack_id, np.full((2, 2), 2, dtype=np.int32))
    assert pressure.negative_gap_violation_pixels == 0
    assert pressure.whole_zone_pressure_fraction_by_zone[0] == 1.0
    assert pressure.interior_pressure_pixels_by_zone[0] == 0


def test_pressure_diagnostics_are_flag_gated():
    image = np.zeros((8, 8, 3), dtype=np.uint8)
    config = _offline_solve_config(
        palette=["bambu-basic-cyan", "bambu-basic-magenta"],
        white_base="panchroma-matte-cotton-white",
        profiles_dir=_PROFILES_DIR,
        stage1_coarsening_factor=2,
    )

    without_pressure = solve_preview(image, config)
    with_pressure = solve_preview(
        image,
        _offline_solve_config(
            palette=["bambu-basic-cyan", "bambu-basic-magenta"],
            white_base="panchroma-matte-cotton-white",
            profiles_dir=_PROFILES_DIR,
            stage1_coarsening_factor=2,
            emit_pressure_diagnostics=True,
        ),
    )

    assert without_pressure.staged_result.visible_plan.stage2_recipe_pressure is None
    assert "stage2_coarse_excess" not in without_pressure.staged_result.compatibility_bundle.debug_maps
    pressure = with_pressure.staged_result.visible_plan.stage2_recipe_pressure
    assert pressure is not None
    assert pressure.coarse_excess.shape == (8, 8)
    assert "stage2_coarse_excess" in with_pressure.staged_result.compatibility_bundle.debug_maps
    assert "stage2_recipe_pressure_diagnostics_s" in with_pressure.staged_result.performance_profile.timings_s
    assert with_pressure.staged_result.performance_profile.counters[
        "stage2_pressure_negative_gap_violation_pixels"
    ] == 0


def test_geometry_attribution_is_flag_gated():
    image = np.zeros((8, 8, 3), dtype=np.uint8)
    image[:, :4] = np.array([0, 255, 255], dtype=np.uint8)
    image[:, 4:] = np.array([255, 0, 255], dtype=np.uint8)
    base_kwargs = dict(
        palette=["bambu-basic-cyan", "bambu-basic-magenta"],
        white_base="panchroma-matte-cotton-white",
        profiles_dir=_PROFILES_DIR,
        stage1_coarsening_factor=2,
    )

    without_geometry = solve_preview(image, _offline_solve_config(**base_kwargs))
    with_geometry = solve_preview(
        image,
        _offline_solve_config(
            **base_kwargs,
            emit_geometry_attribution=True,
        ),
    )

    assert without_geometry.staged_result.cap_plan.stage2_geometry_pressure_attribution is None
    assert (
        "stage2_geometry_prefine_classes"
        not in without_geometry.staged_result.compatibility_bundle.debug_maps
    )

    attribution = with_geometry.staged_result.cap_plan.stage2_geometry_pressure_attribution
    assert attribution is not None
    assert attribution.component_label_map.shape == (8, 8)
    assert attribution.prefine_class_label_map.shape == (8, 8)
    assert attribution.postfine_class_label_map.shape == (8, 8)
    assert attribution.final_blockiness_heatmap.shape == (8, 8)
    assert attribution.cap_raw_deviation_map.shape == (8, 8)
    assert "cross_boundary_geometry_candidate" in attribution.class_names
    assert "coarse_lattice_boundary_candidate" in attribution.class_names
    assert "stage2_zone_boundary_candidate" in attribution.class_names

    debug_maps = with_geometry.staged_result.compatibility_bundle.debug_maps
    assert "stage2_geometry_component_labels" in debug_maps
    assert "stage2_geometry_prefine_classes" in debug_maps
    assert "stage2_final_blockiness_heatmap" in debug_maps
    assert "stage2_coarse_excess" in debug_maps
    assert "stage2_geometry_pressure_attribution_s" in (
        with_geometry.staged_result.performance_profile.timings_s
    )
    assert (
        with_geometry.staged_result.performance_profile.counters[
            "stage2_geometry_attribution_component_count"
        ]
        >= 0
    )


def test_stage2_source_edge_subzones_split_projected_zone_at_source_edge():
    labels = np.zeros((8, 8), dtype=np.int32)
    targets = np.zeros((8, 8, 3), dtype=np.float32)
    targets[:, 4:, 0] = 1.0

    split_labels, refined_zones, refined_pixels = _split_stage2_source_edge_subzones(
        zone_label_map=labels,
        targets=targets.reshape(-1, 3),
        coarse_to_fine_scale=2,
        min_component_pixels=4,
        max_components_per_zone=16,
    )

    assert refined_zones == 1
    assert refined_pixels >= 4
    assert np.unique(split_labels).size > 1
    assert np.array_equal(np.unique(split_labels), np.arange(np.max(split_labels) + 1))


def test_preview_can_disable_stage2_fine_override():
    checker = (np.indices((16, 16)).sum(axis=0) % 2).astype(np.uint8)
    image = np.zeros((16, 16, 3), dtype=np.uint8)
    image[checker == 0] = np.array([0, 255, 255], dtype=np.uint8)
    image[checker == 1] = np.array([255, 0, 255], dtype=np.uint8)
    config = _offline_solve_config(
        palette=["bambu-basic-cyan", "bambu-basic-magenta"],
        white_base="panchroma-matte-cotton-white",
        profiles_dir=_PROFILES_DIR,
        stage1_coarsening_factor=2,
        stage2_fine_override_enabled=False,
        cell_mode="grid",
        color_region_target_mm=0.20,
    )

    result = solve_preview(image, config)

    visible_plan = result.staged_result.visible_plan
    perf = result.staged_result.performance_profile
    np.testing.assert_array_equal(
        visible_plan.recipe_label_map,
        visible_plan.zone_recipe_labels[visible_plan.zone_label_map],
    )
    assert perf.counters["stage2_fine_override_enabled"] is False
    assert perf.counters["stage2_detail_override_pixels"] == 0
    assert perf.counters["stage2_detail_interior_override_pixels"] == 0


def test_preview_builds_fine_recipe_map_for_coarse_stage1_zones():
    checker = (np.indices((16, 16)).sum(axis=0) % 2).astype(np.uint8)
    image = np.zeros((16, 16, 3), dtype=np.uint8)
    image[checker == 0] = np.array([0, 255, 255], dtype=np.uint8)
    image[checker == 1] = np.array([255, 0, 255], dtype=np.uint8)
    config = _offline_solve_config(
        palette=["bambu-basic-cyan", "bambu-basic-magenta"],
        white_base="panchroma-matte-cotton-white",
        profiles_dir=_PROFILES_DIR,
        stage1_coarsening_factor=2,
        cell_mode="grid",
        color_region_target_mm=0.20,
    )

    result = solve_preview(image, config)

    visible_plan = result.staged_result.visible_plan
    perf = result.staged_result.performance_profile
    assert visible_plan.fine_recipe_label_map is not None
    assert visible_plan.fine_recipe_label_map.shape == visible_plan.evaluation_shape
    assert visible_plan.recipe_label_map.shape == visible_plan.evaluation_shape
    assert perf.counters["stage2_detail_override_pixels"] >= 0
    assert perf.counters["stage2_detail_override_zones"] >= 0
    assert perf.counters["stage2_detail_interior_override_pixels"] >= 0
    assert perf.counters["stage2_detail_interior_override_zones"] >= 0
    assert visible_plan.zone_recipe_labels.shape[0] == np.unique(visible_plan.zone_label_map).size


def test_preview_legacy_maps_are_stage5_bridge_outputs():
    image = np.zeros((8, 8, 3), dtype=np.uint8)
    config = _offline_solve_config(
        palette=["bambu-basic-cyan", "bambu-basic-magenta"],
        white_base="panchroma-matte-cotton-white",
        profiles_dir=_PROFILES_DIR,
    )

    result = solve_preview(image, config)

    bridge = result.staged_result.compatibility_bundle
    bridge_keys = set(bridge.thickness_maps)
    result_keys = set(result.thickness_maps)

    assert result.solved_plan is not None
    assert result.staged_result is not None
    # Task 5.4: thickness_maps mirrors the bridge thickness maps exactly. The
    # solve diagnostics no longer ride in thickness_maps — __gamut_mask__ comes
    # from the bridge diagnostics and __de__ from recompute, both into diagnostics.
    assert result_keys == bridge_keys
    assert "__gamut_mask__" in bridge.diagnostics
    assert "__de__" in result.diagnostics
    assert "__gamut_mask__" in result.diagnostics
    assert "detail_height" in result.staged_result.compatibility_bundle.debug_maps
    assert "boundary_edge_guard" in result.staged_result.compatibility_bundle.debug_maps
    assert "detail_candidate_zone_labels" in result.staged_result.compatibility_bundle.debug_maps
    assert "detail_zone_labels" in result.staged_result.compatibility_bundle.debug_maps
    assert "detail_rejection_reasons" in result.staged_result.compatibility_bundle.debug_maps
    for key in bridge_keys:
        np.testing.assert_allclose(result.thickness_maps[key], bridge.thickness_maps[key])
        assert result.thickness_maps[key] is not bridge.thickness_maps[key]


def test_stage4_authored_detail_zones_filter_tiny_components():
    state = SimpleNamespace(
        config=SimpleNamespace(
            solver_fine_pitch_mm=0.20,
            nozzle_diameter=0.20,
        ),
    )
    detail_mask = np.zeros((4, 4), dtype=bool)
    detail_mask[0, 0] = True
    detail_mask[2, 2] = True
    detail_mask[2, 3] = True
    requested_detail_layers = np.where(detail_mask, np.float32(0.08), np.float32(0.0))
    optical_gain_map = np.where(detail_mask, np.float32(0.02), np.float32(np.nan))
    detail_signal = np.where(detail_mask, np.float32(1.0), np.float32(0.0))

    (
        active_mask,
        label_map,
        candidate_label_map,
        rejection_reason_map,
        summary,
        facts,
    ) = _author_stage4_detail_zones(
        state=state,
        detail_mask=detail_mask,
        requested_detail_layers=requested_detail_layers,
        optical_gain_map=optical_gain_map,
        detail_signal=detail_signal,
        signal_threshold=0.5,
        enabled=True,
    )

    assert summary.zone_count == 1
    assert summary.candidate_zone_count == 2
    assert summary.rejected_zone_count == 1
    assert summary.rejected_too_small_zone_count == 1
    assert summary.candidate_pixels == 3
    assert summary.active_pixels == 2
    assert summary.rejected_pixels == 1
    assert summary.min_zone_pixels == 2
    assert len(facts) == 2
    assert active_mask[2, 2]
    assert active_mask[2, 3]
    assert not active_mask[0, 0]
    assert label_map[2, 2] == label_map[2, 3] == 0
    assert label_map[0, 0] == -1
    assert candidate_label_map[0, 0] >= 0
    assert rejection_reason_map[2, 2] == 0
    assert rejection_reason_map[0, 0] == 1


def test_stage4_authored_detail_zones_accept_and_reject_as_whole_zones():
    state = SimpleNamespace(
        config=SimpleNamespace(
            solver_fine_pitch_mm=0.20,
            nozzle_diameter=0.20,
        ),
    )
    detail_mask = np.zeros((4, 5), dtype=bool)
    detail_mask[0, 0] = True
    detail_mask[0, 1] = True
    detail_mask[3, 3] = True
    detail_mask[3, 4] = True
    requested_detail_layers = np.where(detail_mask, np.float32(0.08), np.float32(0.0))
    optical_gain_map = np.full(detail_mask.shape, np.nan, dtype=np.float32)
    optical_gain_map[0, 0] = np.float32(0.02)
    optical_gain_map[0, 1] = np.float32(-0.001)
    optical_gain_map[3, 3] = np.float32(0.001)
    optical_gain_map[3, 4] = np.float32(0.001)
    detail_signal = np.where(detail_mask, np.float32(1.0), np.float32(0.0))

    (
        active_mask,
        label_map,
        candidate_label_map,
        rejection_reason_map,
        summary,
        facts,
    ) = _author_stage4_detail_zones(
        state=state,
        detail_mask=detail_mask,
        requested_detail_layers=requested_detail_layers,
        optical_gain_map=optical_gain_map,
        detail_signal=detail_signal,
        signal_threshold=0.5,
        enabled=True,
    )

    assert summary.candidate_zone_count == 2
    assert summary.zone_count == 1
    assert summary.rejected_weak_optical_gain_zone_count == 1
    assert active_mask[0, 0]
    assert active_mask[0, 1]
    assert label_map[0, 0] == label_map[0, 1] == 0
    assert not active_mask[3, 3]
    assert not active_mask[3, 4]
    assert candidate_label_map[3, 3] >= 0
    assert rejection_reason_map[0, 0] == 0
    assert rejection_reason_map[3, 3] == 2
    assert {fact.rejection_reason for fact in facts if not fact.accepted} == {"weak_optical_gain"}


def test_stage4_detail_recipe_boundary_support_can_rescue_moderate_signal():
    state = SimpleNamespace(
        config=SimpleNamespace(
            solver_fine_pitch_mm=0.20,
            nozzle_diameter=0.20,
        ),
    )
    detail_mask = np.zeros((3, 4), dtype=bool)
    detail_mask[1, 1:3] = True
    requested_detail_layers = np.where(detail_mask, np.float32(0.08), np.float32(0.0))
    optical_gain_map = np.where(detail_mask, np.float32(0.02), np.float32(np.nan))
    detail_signal = np.where(detail_mask, np.float32(0.40), np.float32(0.0))
    recipe_boundary_support = detail_mask.copy()
    selected = detail_mask.copy()

    (
        active_mask,
        _label_map,
        _candidate_label_map,
        _rejection_reason_map,
        summary,
        facts,
    ) = _author_stage4_detail_zones(
        state=state,
        detail_mask=selected,
        requested_detail_layers=requested_detail_layers,
        optical_gain_map=optical_gain_map,
        detail_signal=detail_signal,
        signal_threshold=0.50,
        enabled=True,
        recipe_boundary_support=recipe_boundary_support,
    )

    assert np.array_equal(selected, detail_mask)
    assert np.array_equal(active_mask, detail_mask)
    assert summary.zone_count == 1
    assert summary.mean_zone_structure_support == 1.0
    assert summary.mean_zone_recipe_boundary_support == 1.0
    assert facts[0].signal_support_fraction == 0.0
    assert facts[0].structure_support_fraction == 1.0


def test_stage4_independent_detail_layer_limiter_honors_user_layer_cap():
    requested = np.array([[0.08, 0.16, 0.32]], dtype=np.float32)
    available = np.full_like(requested, 0.40, dtype=np.float32)

    independent_limited = staged_runner._limit_stage4_independent_detail_layers(
        requested,
        available_detail_mm=available,
        layer_height=0.08,
        max_layers=3,
    )

    expected = np.array([[0.08, 0.16, 0.24]], dtype=np.float32)
    np.testing.assert_allclose(independent_limited, expected, atol=1e-6)


def test_stage4_detail_stack_shaping_reserves_second_layer_for_strong_signal():
    detail_mask = np.array([[True, True, True]], dtype=bool)
    requested = np.array([[0.16, 0.16, 0.08]], dtype=np.float32)
    detail_signal = np.array([[0.12, 0.06, 0.06]], dtype=np.float32)

    shaped = _shape_stage4_detail_stack_layers(
        detail_mask=detail_mask,
        requested_detail_layers=requested,
        detail_signal=detail_signal,
        signal_threshold=0.10,
        layer_height=0.08,
    )

    np.testing.assert_allclose(
        shaped,
        np.array([[0.16, 0.08, 0.08]], dtype=np.float32),
        atol=1e-6,
    )


def test_stage4_smooth_variable_cap_tracks_luminance():
    gradient = np.tile(np.linspace(0, 255, 16, dtype=np.uint8), (16, 1))
    image = np.stack([gradient, gradient, gradient], axis=2)
    config = _offline_solve_config(
        palette=["bambu-basic-cyan", "bambu-basic-magenta"],
        white_base="panchroma-matte-cotton-white",
        profiles_dir=_PROFILES_DIR,
        cap_mode="smooth_variable",
        smooth_kernel=0.0,
    )

    result = solve_preview(image, config)

    cap_map = result.cap_map.astype(np.float32)
    assert float(np.ptp(cap_map)) > 0.0
    assert float(np.mean(cap_map[:, :4])) > float(np.mean(cap_map[:, -4:]))
    diagnostic_codes = {
        entry.code for entry in result.staged_result.planning_diagnostics.entries
    }
    assert "stage4_cap_height_range_mm" in diagnostic_codes


def test_stage4_boundary_edge_guard_tracks_recipe_and_height_edges():
    target = np.zeros((8, 8, 3), dtype=np.float32)
    target[:, 4:, 0] = 1.0
    visible_plan = SimpleNamespace(
        evaluation_shape=(8, 8),
        mapped_target_oklab=target.reshape(-1, 3),
        recipe_label_map=np.pad(
            np.ones((8, 4), dtype=np.int32),
            ((0, 0), (0, 4)),
            constant_values=0,
        ),
    )
    color_ceiling = np.zeros((8, 8), dtype=np.float32)
    color_ceiling[:, 4:] = 0.80
    filler_plan = SimpleNamespace(color_ceiling_mm=color_ceiling)

    guard = _build_stage4_boundary_edge_guard(
        visible_plan=visible_plan,
        filler_plan=filler_plan,
        layer_height=0.08,
        smooth_kernel=3.0,
    )

    assert guard.shape == (8, 8)
    assert float(np.mean(guard[:, 3:5])) > 0.20
    assert float(np.mean(guard[:, :1])) < float(np.mean(guard[:, 3:5]))


def test_stage4_boundary_smoothing_guide_does_not_preserve_source_or_ceiling_texture():
    target = np.zeros((8, 8, 3), dtype=np.float32)
    target[:, ::2, 0] = 1.0
    visible_plan = SimpleNamespace(
        evaluation_shape=(8, 8),
        mapped_target_oklab=target.reshape(-1, 3),
    )
    color_ceiling = np.zeros((8, 8), dtype=np.float32)
    color_ceiling[:, 4:] = 0.80
    filler_plan = SimpleNamespace(color_ceiling_mm=color_ceiling)

    guide = _build_stage4_boundary_smoothing_guide(
        visible_plan=visible_plan,
        filler_plan=filler_plan,
    )

    assert float(np.ptp(guide)) < 1e-6


def test_stage4_edge_aware_boundary_restore_keeps_strong_edge_close_to_raw():
    raw = np.full((5, 7), 0.08, dtype=np.float32)
    raw[:, 4:] = np.float32(0.56)
    smoothed = np.full_like(raw, np.float32(0.32))
    guard = np.zeros_like(raw, dtype=np.float32)
    guard[:, 3:5] = np.float32(1.0)

    restored = _apply_stage4_edge_aware_boundary_restore(
        smoothed_cap=smoothed,
        raw_cap_reference=raw,
        edge_guard_weight=guard,
    )

    assert float(np.mean(np.abs(restored[:, 4] - raw[:, 4]))) < float(
        np.mean(np.abs(smoothed[:, 4] - raw[:, 4]))
    )
    np.testing.assert_allclose(restored[:, 0], smoothed[:, 0])


def test_stage4_structure_guided_boundary_smoothing_limits_cross_edge_bleed():
    raw = np.full((9, 31), np.float32(0.08), dtype=np.float32)
    raw[:, 16:] = np.float32(0.72)
    guide = np.zeros_like(raw, dtype=np.float32)
    guide[:, 16:] = np.float32(1.0)

    smoothed = _smooth_stage4_boundary_cap(
        raw_cap=raw,
        smoothing_guide=guide,
        smooth_kernel=5.0,
    )

    assert float(np.mean(smoothed[:, 14])) < 0.20
    assert float(np.mean(smoothed[:, 16])) > 0.60
    assert float(np.mean(smoothed[:, :4])) < 0.12
    assert float(np.mean(smoothed[:, -4:])) > 0.68


def test_stage4_boundary_cap_is_soft_screen_when_detail_is_disabled():
    gradient = np.tile(np.linspace(0, 255, 16, dtype=np.uint8), (16, 1))
    image = np.stack([gradient, gradient, gradient], axis=2)
    config = _offline_solve_config(
        palette=["bambu-basic-cyan", "bambu-basic-magenta"],
        white_base="panchroma-matte-cotton-white",
        profiles_dir=_PROFILES_DIR,
        cap_mode="smooth_variable",
        smooth_kernel=0.0,
        detail_cap_enabled=False,
        cap_continuity_cleanup=False,
    )

    result = solve_preview(image, config)

    filler_plan = result.staged_result.filler_plan
    cap_plan = result.staged_result.cap_plan
    boundary_cap_height = (
        cap_plan.cap_boundary_top_mm - filler_plan.color_ceiling_mm
    ).astype(np.float32)
    remaining_budget = np.clip(
        float(result.config.t_max) - filler_plan.color_ceiling_mm,
        0.0,
        float(result.config.effective_d_wc_max()),
    ).astype(np.float32)
    floor_mm = (
        np.ceil(float(result.config.d_wc_min) / float(result.config.layer_height) - 1e-9)
        * float(result.config.layer_height)
    )
    expected_boundary = np.minimum(
        np.full_like(boundary_cap_height, np.float32(floor_mm)),
        remaining_budget,
    ).astype(np.float32)

    assert float(np.max(boundary_cap_height)) > float(floor_mm)
    assert np.count_nonzero(boundary_cap_height > expected_boundary + 1e-6) > 0
    assert np.all(boundary_cap_height + 1e-6 >= expected_boundary)
    assert np.all(boundary_cap_height <= remaining_budget + 1e-6)
    np.testing.assert_allclose(cap_plan.final_visible_top_mm, cap_plan.cap_boundary_top_mm, atol=1e-6)
    assert "__white_boundary_cap__" in result.thickness_maps
    assert "__white_detail_cap__" in result.thickness_maps
    np.testing.assert_allclose(result.thickness_maps["__white_boundary_cap__"], boundary_cap_height, atol=1e-6)
    assert np.count_nonzero(result.thickness_maps["__white_detail_cap__"] > 1e-9) == 0


def test_stage4_detail_tier_can_raise_final_surface_above_boundary():
    checker = ((np.indices((16, 16)).sum(axis=0) % 2) * 255).astype(np.uint8)
    image = np.stack([checker, checker, checker], axis=2)
    config = _offline_solve_config(
        palette=["bambu-basic-cyan", "bambu-basic-magenta"],
        white_base="panchroma-matte-cotton-white",
        profiles_dir=_PROFILES_DIR,
        appearance_model_provider="historical_spline",
        model_domain_ingress=False,
        cap_mode="smooth_variable",
        smooth_kernel=1.5,
        detail_cap_enabled=True,
        t_max=4.0,
    )

    result = solve_preview(image, config)

    cap_plan = result.staged_result.cap_plan
    assert float(np.max(cap_plan.detail_height_mm)) > 0.0
    assert np.count_nonzero(cap_plan.final_visible_top_mm > cap_plan.cap_boundary_top_mm + 1e-9) > 0
    assert cap_plan.detail_zone_summary.zone_count > 0
    assert cap_plan.detail_zone_summary.candidate_zone_count >= cap_plan.detail_zone_summary.zone_count
    assert len(cap_plan.detail_zone_facts) == cap_plan.detail_zone_summary.candidate_zone_count
    assert cap_plan.detail_candidate_zone_label_map.shape == cap_plan.detail_zone_label_map.shape
    assert cap_plan.detail_zone_rejection_reason_map.shape == cap_plan.detail_zone_label_map.shape
    assert np.count_nonzero(cap_plan.detail_zone_label_map >= 0) == int(
        np.count_nonzero(cap_plan.detail_height_mm > 1e-9)
    )
    assert result.cap_quality.get("detail_tier_enabled") is True
    assert result.cap_quality.get("detail_tier_active_pixels", 0) > 0
    assert result.cap_quality.get("detail_tier_zone_count", 0) > 0
    diagnostic_codes = {
        entry.code for entry in result.staged_result.planning_diagnostics.entries
    }
    assert "stage4_detail_tier_active_pixels" in diagnostic_codes
    assert "stage4_detail_zone_count" in diagnostic_codes
    assert "stage4_detail_zone_rejection_reasons" in diagnostic_codes


def test_stage4_detail_smoothing_summary_reaches_preview_and_export_metadata(monkeypatch):
    def fake_smoothing(
        *,
        detail_height_mm,
        cfg,
        layer_height,
        boundary_cap_height_mm,
        remaining_cap_budget_mm,
        desired_final_cap_target_mm,
    ):
        _ = cfg, boundary_cap_height_mm, remaining_cap_budget_mm, desired_final_cap_target_mm
        detail = np.asarray(detail_height_mm, dtype=np.float32).copy()
        detail[0, 0] = np.float32(layer_height)
        return detail, {
            "applied": True,
            "changed_px": 1,
            "raised_px": 1,
            "lowered_px": 0,
            "mean_abs_layer_delta": 1.0,
            "p95_abs_layer_delta": 1.0,
            "max_abs_layer_delta": 1,
            "before": {"topology": {}},
            "after": {"topology": {}},
            "delta": {},
            "printability_regated": True,
        }

    monkeypatch.setattr(
        staged_runner,
        "_apply_stage4_detail_cap_smoothing",
        fake_smoothing,
    )
    image = np.zeros((8, 8, 3), dtype=np.uint8)
    config = _offline_solve_config(
        palette=["bambu-basic-cyan", "bambu-basic-magenta"],
        white_base="panchroma-matte-cotton-white",
        luminance_handler_enabled=True,
        detail_cap_enabled=True,
        t_max=4.0,
    )

    result = solve_preview(image, config)

    smoothing = result.cap_quality.get("detail_cap_smoothing")
    assert smoothing is not None
    assert smoothing["changed_px"] == 1
    assert smoothing["printability_regated"] is True
    np.testing.assert_allclose(
        result.thickness_maps["__white_cap__"],
        result.thickness_maps["__white_boundary_cap__"]
        + result.thickness_maps["__white_detail_cap__"],
        atol=1e-7,
    )
    np.testing.assert_allclose(
        result.solved_plan.cap_height_map,
        result.thickness_maps["__white_cap__"],
        atol=1e-7,
    )
    assert (
        result.export_metadata[WHITE_CAP_FIELD_TARGET_METADATA_KEY][
            "detail_smoothing_applied"
        ]
        is True
    )
    _assert_final_visible_white_cap_export_contract(
        result,
        expected_policy=POLICY_LUMINANCE_DETAIL_CANONICAL,
    )


def test_export_contract_invariants_for_standard_smooth_variable():
    gradient = np.tile(np.linspace(0, 255, 8, dtype=np.uint8), (8, 1))
    image = np.stack([gradient, gradient, gradient], axis=2)
    config = _offline_solve_config(
        palette=["bambu-basic-cyan", "bambu-basic-magenta"],
        white_base="panchroma-matte-cotton-white",
        profiles_dir=_PROFILES_DIR,
        cap_mode="smooth_variable",
        d_wc_min=0.08,
        d_wc_max=0.32,
        t_max=1.00,
    )

    result = solve_preview(image, config)

    _assert_final_visible_white_cap_export_contract(
        result,
        expected_policy=POLICY_STANDARD_SMOOTH_VARIABLE_CANONICAL,
    )


def test_stage4_detail_tier_keeps_boundary_screen_stable():
    checker = ((np.indices((16, 16)).sum(axis=0) % 2) * 255).astype(np.uint8)
    image = np.stack([checker, checker, checker], axis=2)
    common = dict(
        palette=["bambu-basic-cyan", "bambu-basic-magenta"],
        white_base="panchroma-matte-cotton-white",
        profiles_dir=_PROFILES_DIR,
        appearance_model_provider="historical_spline",
        model_domain_ingress=False,
        cap_mode="smooth_variable",
        smooth_kernel=1.5,
        t_max=4.0,
    )
    boundary_only = solve_preview(
        image,
        SolveConfig(
            **common,
            detail_cap_enabled=False,
        ),
    )
    detail_result = solve_preview(
        image,
        SolveConfig(
            **common,
            detail_cap_enabled=True,
        ),
    )

    boundary_cap = boundary_only.staged_result.cap_plan
    detail_cap = detail_result.staged_result.cap_plan
    np.testing.assert_allclose(
        detail_cap.cap_boundary_top_mm,
        boundary_cap.cap_boundary_top_mm,
        atol=1e-6,
    )
    assert np.count_nonzero(boundary_cap.detail_height_mm > 1e-9) > 0
    assert np.count_nonzero(detail_cap.detail_height_mm > 1e-9) > 0
    np.testing.assert_allclose(
        detail_cap.final_visible_top_mm,
        boundary_cap.final_visible_top_mm,
        atol=1e-6,
    )
    assert detail_result.cap_quality.get("detail_tier_mode") == "layer_limited"


def test_stage4_boundary_smoothing_targets_visible_top_surface():
    color_ceiling = np.full((8, 8), 0.20, dtype=np.float32)
    color_ceiling[:, 1::2] = np.float32(1.80)
    raw_top = np.full(color_ceiling.shape, 2.40, dtype=np.float32)
    implied_cap = raw_top - color_ceiling

    class _Config(SimpleNamespace):
        def effective_d_wc_max(self):
            return 2.80

    cfg = _Config(
        layer_height=0.08,
        d_wc_min=0.08,
        cap_mode="smooth_variable",
        smooth_kernel=20.0,
        cap_continuity_cleanup=False,
    )
    visible_plan = SimpleNamespace(
        evaluation_shape=color_ceiling.shape,
        implied_cap_height_mm=implied_cap.reshape(-1),
        mapped_target_oklab=np.zeros(color_ceiling.shape + (3,), dtype=np.float32).reshape(-1, 3),
        recipe_label_map=np.zeros(color_ceiling.shape, dtype=np.int32),
    )
    filler_plan = SimpleNamespace(color_ceiling_mm=color_ceiling)

    state = SimpleNamespace(config=cfg)
    requested, detail_reference, edge_guard = _requested_stage4_cap_maps(
        state,
        visible_plan,
        filler_plan,
        PlanningDiagnosticsStream(),
    )

    visible_top = color_ceiling + requested
    assert float(np.ptp(visible_top)) <= float(cfg.layer_height) + 1e-6
    assert float(np.ptp(detail_reference)) > 1.0
    assert not np.any(edge_guard > 1e-9)
    debug_maps = state.debug_maps
    expected_debug_keys = {
        "stage4_boundary_raw_requested_cap_mm",
        "stage4_boundary_raw_top_reference_mm",
        "stage4_boundary_smoothed_top_pre_restore_mm",
        "stage4_boundary_smoothed_top_post_restore_mm",
        "stage4_boundary_unquantized_requested_cap_mm",
        "stage4_boundary_quantized_requested_cap_mm",
        "stage4_color_ceiling_mm",
        "stage4_boundary_edge_guard_weight",
    }
    assert expected_debug_keys <= set(debug_maps)
    for key in expected_debug_keys:
        assert debug_maps[key].shape == color_ceiling.shape
        assert debug_maps[key].dtype == np.float32
        assert np.all(np.isfinite(debug_maps[key]))
    np.testing.assert_allclose(
        debug_maps["stage4_boundary_smoothed_top_post_restore_mm"],
        color_ceiling + debug_maps["stage4_boundary_unquantized_requested_cap_mm"],
        atol=1e-6,
    )


def test_stage4_boundary_field_debug_keeps_prequantized_smoothing():
    color_ceiling = np.zeros((9, 21), dtype=np.float32)
    raw_cap = np.full(color_ceiling.shape, 0.08, dtype=np.float32)
    raw_cap[:, 10:] = np.float32(1.20)

    class _Config(SimpleNamespace):
        def effective_d_wc_max(self):
            return 2.80

    cfg = _Config(
        layer_height=0.08,
        d_wc_min=0.08,
        cap_mode="smooth_variable",
        smooth_kernel=3.0,
        cap_continuity_cleanup=False,
    )
    visible_plan = SimpleNamespace(
        evaluation_shape=color_ceiling.shape,
        implied_cap_height_mm=raw_cap.reshape(-1),
        mapped_target_oklab=np.zeros(color_ceiling.shape + (3,), dtype=np.float32).reshape(-1, 3),
        recipe_label_map=np.zeros(color_ceiling.shape, dtype=np.int32),
    )
    filler_plan = SimpleNamespace(color_ceiling_mm=color_ceiling)
    state = SimpleNamespace(config=cfg)

    requested, _, _ = _requested_stage4_cap_maps(
        state,
        visible_plan,
        filler_plan,
        PlanningDiagnosticsStream(),
    )

    post_restore = state.debug_maps["stage4_boundary_smoothed_top_post_restore_mm"]
    unquantized = state.debug_maps["stage4_boundary_unquantized_requested_cap_mm"]
    quantized = state.debug_maps["stage4_boundary_quantized_requested_cap_mm"]

    np.testing.assert_allclose(post_restore, unquantized, atol=1e-6)
    np.testing.assert_allclose(requested, quantized, atol=1e-6)
    assert np.count_nonzero(
        np.abs(unquantized / float(cfg.layer_height) - np.rint(unquantized / float(cfg.layer_height)))
        > 1e-4
    ) > 0
    assert not np.allclose(unquantized, quantized)


def test_stage4_boundary_edge_restore_decreases_with_smoothness():
    low_smooth = _stage4_boundary_edge_restore_weight(1.0)
    mid_smooth = _stage4_boundary_edge_restore_weight(8.0)
    high_smooth = _stage4_boundary_edge_restore_weight(20.0)

    assert low_smooth > mid_smooth > high_smooth
    assert high_smooth == 0.0


def test_stage4_detail_tier_raises_top_without_moving_boundary():
    checker = ((np.indices((16, 16)).sum(axis=0) % 2) * 255).astype(np.uint8)
    image = np.stack([checker, checker, checker], axis=2)
    common = dict(
        palette=["bambu-basic-cyan", "bambu-basic-magenta"],
        white_base="panchroma-matte-cotton-white",
        profiles_dir=_PROFILES_DIR,
        appearance_model_provider="historical_spline",
        model_domain_ingress=False,
        cap_mode="smooth_variable",
        smooth_kernel=1.5,
        t_max=4.0,
    )
    boundary_only = solve_preview(
        image,
        SolveConfig(
            **common,
            detail_cap_enabled=False,
        ),
    )
    detail_result = solve_preview(
        image,
        SolveConfig(
            **common,
            detail_cap_enabled=True,
        ),
    )

    boundary_cap = boundary_only.staged_result.cap_plan
    detail_cap = detail_result.staged_result.cap_plan
    np.testing.assert_allclose(
        detail_cap.cap_boundary_top_mm,
        boundary_cap.cap_boundary_top_mm,
        atol=1e-6,
    )
    assert np.count_nonzero(boundary_cap.detail_height_mm > 1e-9) > 0
    assert np.count_nonzero(detail_cap.detail_height_mm > 1e-9) > 0
    np.testing.assert_allclose(
        detail_cap.final_visible_top_mm,
        boundary_cap.final_visible_top_mm,
        atol=1e-6,
    )
    assert detail_result.cap_quality.get("detail_tier_mode") == "layer_limited"
    diagnostic_codes = {
        entry.code for entry in detail_result.staged_result.planning_diagnostics.entries
    }
    assert "stage4_detail_tier_active_pixels" in diagnostic_codes


def test_stage4_detail_tier_selects_detail_above_boundary():
    checker = ((np.indices((16, 16)).sum(axis=0) % 2) * 255).astype(np.uint8)
    image = np.stack([checker, checker, checker], axis=2)
    common = dict(
        palette=["bambu-basic-cyan", "bambu-basic-magenta"],
        white_base="panchroma-matte-cotton-white",
        profiles_dir=_PROFILES_DIR,
        appearance_model_provider="historical_spline",
        model_domain_ingress=False,
        cap_mode="smooth_variable",
        smooth_kernel=1.5,
    )
    boundary_only = solve_preview(
        image,
        SolveConfig(
            **common,
            detail_cap_enabled=False,
        ),
    )
    detail_result = solve_preview(
        image,
        SolveConfig(
            **common,
            detail_cap_enabled=True,
        ),
    )

    boundary_cap = boundary_only.staged_result.cap_plan
    detail_cap = detail_result.staged_result.cap_plan
    np.testing.assert_allclose(
        detail_cap.cap_boundary_top_mm,
        boundary_cap.cap_boundary_top_mm,
        atol=1e-6,
    )
    assert np.count_nonzero(boundary_cap.detail_height_mm > 1e-9) > 0
    assert np.count_nonzero(detail_cap.detail_height_mm > 1e-9) > 0
    np.testing.assert_allclose(
        detail_cap.final_visible_top_mm,
        boundary_cap.final_visible_top_mm,
        atol=1e-6,
    )
    assert detail_result.cap_quality.get("detail_tier_mode") == "layer_limited"
    diagnostic_codes = {
        entry.code for entry in detail_result.staged_result.planning_diagnostics.entries
    }
    assert "stage4_detail_tier_active_pixels" in diagnostic_codes


def test_stage4_detail_tier_stays_quiet_on_smooth_gradient():
    gradient = np.tile(np.linspace(0, 255, 16, dtype=np.uint8), (16, 1))
    image = np.stack([gradient, gradient, gradient], axis=2)
    config = _offline_solve_config(
        palette=["bambu-basic-cyan", "bambu-basic-magenta"],
        white_base="panchroma-matte-cotton-white",
        profiles_dir=_PROFILES_DIR,
        appearance_model_provider="historical_spline",
        model_domain_ingress=False,
        cap_mode="smooth_variable",
        smooth_kernel=1.5,
        detail_cap_enabled=True,
    )

    result = solve_preview(image, config)

    active_pixels = int(np.count_nonzero(result.staged_result.cap_plan.detail_height_mm > 1e-9))
    assert active_pixels <= 64


def test_stage4_detail_tier_cannot_be_disabled_through_facade():
    checker = ((np.indices((16, 16)).sum(axis=0) % 2) * 255).astype(np.uint8)
    image = np.stack([checker, checker, checker], axis=2)
    config = SolveConfig(
        palette=["bambu-basic-cyan", "bambu-basic-magenta"],
        white_base="panchroma-matte-cotton-white",
        profiles_dir=_PROFILES_DIR,
        appearance_model_provider="historical_spline",
        model_domain_ingress=False,
        cap_mode="smooth_variable",
        smooth_kernel=1.5,
        detail_cap_enabled=False,
    )

    result = solve_preview(image, config)

    cap_plan = result.staged_result.cap_plan
    assert np.count_nonzero(cap_plan.detail_height_mm > 1e-9) > 0
    assert cap_plan.detail_zone_summary.zone_count > 0
    assert np.count_nonzero(cap_plan.detail_zone_label_map >= 0) > 0
    assert result.cap_quality.get("detail_tier_enabled") is True
    assert result.cap_quality.get("detail_tier_mode") == "layer_limited"
    assert result.cap_quality.get("detail_tier_active_pixels", 0) > 0
    assert result.cap_quality.get("detail_tier_zone_count", 0) > 0


def test_full_solve_uses_staged_backend_with_export_solved_plan():
    gradient = np.tile(np.linspace(0, 255, 12, dtype=np.uint8), (12, 1))
    image = np.stack([gradient, np.flipud(gradient), gradient], axis=2)
    config = _offline_solve_config(
        palette=["bambu-basic-cyan", "bambu-basic-magenta"],
        white_base="panchroma-matte-cotton-white",
        profiles_dir=_PROFILES_DIR,
        cap_mode="smooth_variable",
        smooth_kernel=1.5,
    )

    result = solve_full(image, config)

    assert result.staged_result is not None
    assert result.solved_plan is not None
    assert result.solved_plan.shape == result.cap_map.shape
    assert result.cap_quality.get("cap_quality_mode") == "experimental"
    assert result.cap_quality.get("solver_name") == "staged_backend"
    assert "__white_cap__" in result.thickness_maps
    assert float(np.ptp(result.cap_map.astype(np.float32))) > 0.0
