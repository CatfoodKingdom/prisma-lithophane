"""Stage 1 and Stage 2 solver optimization contracts."""

import sys
from types import SimpleNamespace
from pathlib import Path

import numpy as np
import pytest

_GEN_DIR = Path(__file__).resolve().parents[3] / "Prisma" / "generator"
if str(_GEN_DIR) not in sys.path:
    sys.path.insert(0, str(_GEN_DIR))

from tests.generator.profile_fixture import PROFILES_DIR as _PROFILES_DIR
from tests.generator.support.staged_backend import (
    offline_solve_config as _offline_solve_config,
)

from facade import SolveConfig, solve_preview
from pipeline.staged.stage2 import objective as stage2_objective
from pipeline.staged_artifacts import LateralZonePlan
from pipeline.staged_solver_helpers import generate_stage1_zone_labels
from pipeline.staged.stage2.contracts import _ZoneCandidateSet
from pipeline.staged.stage2.refinement import (
    _apply_stage2_boundary_recipe_mutation,
    _iterate_stage2_boundary_recipe_mutation,
    _apply_stage2_fine_override_seam_gate,
    _build_stage2_fine_recipe_assignments,
)
from pipeline.staged.stage2.printability import (
    _apply_stage2_final_color_printability_gate,
    _apply_stage2_fine_override_printability_gate,
    _apply_stage2_localized_width_loss_boundary_nudge,
    _stage2_printability_failure_snapshot_from_stack_ids,
)
from pipeline.staged.stage2.candidates import (
    _augment_zone_candidates_with_neighbor_local_bests,
    _prune_zone_candidate_frontiers,
    _rescue_stage2_optical_frontier_candidates,
)
from pipeline.staged.stage2.optimization import (
    _build_stage2_objective_summary,
    _optimize_zone_recipe_labels,
    _run_coord_descent,
    _seed_zone_recipe_labels_with_beam,
)
from pipeline.staged.zone_geometry import _summarize_zone_targets
from pipeline.staged.coarse_grid import (
    _downsample_rgb_image,
    _project_zone_labels_to_fine,
)
from pipeline.staged.stage1_zones import _effective_color_region_target_mm
from pipeline.staged.stage2.objective import (
    _score_pixels_against_stack_ids,
    _score_zone_pixels_against_candidates,
)
from pipeline.staged.cap_prediction import _stage4_lookup_oklab_by_count
from pipeline.staged_printability import BlueprintPrintabilitySettings


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

    neighbors = stage2_objective._build_zone_neighbors(3, adjacency, edge_lengths)
    before = []
    after = []
    for zone_id in range(3):
        before.append(
            stage2_objective._zone_objective_breakdown(
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
            stage2_objective._zone_objective_breakdown(
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
        selected_totals = stage2_objective._selected_total_thicknesses(
            selected,
            candidate_sets,
        )
        retaining_penalties = stage2_objective._candidate_retaining_penalties(
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
