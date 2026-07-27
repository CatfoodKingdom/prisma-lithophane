"""Stage 2 visible-recipe planning service."""
from __future__ import annotations

import math
import time

import numpy as np

from ...staged_solver_helpers import (
    _precompute_cap_oklabs,
    _precompute_cap_oklabs_vectorized,
    _vectorized_stack_ids,
)

from ...staged_artifacts import (
    StagedPerformanceProfile,
    LateralZonePlan,
    PlanningDiagnosticEntry,
    PlanningDiagnosticsStream,
    QuantizedDirectiveSet,
    VisibleRecipe,
    VisibleRecipeRawGeometryPlan,
)
from ...staged_printability import resolve_blueprint_printability_settings
from ...material_exposure import lateral_boundary_shield_floor_layers

from ..coarse_grid import (
    _stage1_lattice_offset_px,
    _project_zone_labels_to_fine,
)
from ..printability_enforcement import _printability_enforcement_enabled
from ..recipe_pressure import _STAGE2_PRESSURE_ACTIVE_THRESHOLD
from ..telemetry import (
    _record_timing,
    _set_counter,
)
from ..zone_geometry import (
    _build_zone_adjacency,
    _zone_flat_indices,
    _summarize_zone_targets,
)

from .candidates import (
    _query_stage2_pixel_stacks,
    _enumerate_zone_candidates,
    _augment_zone_candidates_with_neighbor_local_bests,
    _prune_zone_candidate_frontiers,
    _rescue_stage2_optical_frontier_candidates,
)
from .contracts import _Stage2PrintabilityFailureSnapshot
from .metrics import record_stage2_diagnostics
from .objective import (
    _STAGE2_RETAINING_WALL_WEIGHT,
    _stage2_continuity_weight,
    _zone_local_cost_weights,
)
from .optimization import (
    _STAGE2_BEAM_WIDTH,
    _seed_zone_recipe_labels_with_beam,
    _optimize_zone_recipe_labels,
    _build_stage2_objective_summary,
    _selected_zone_stack_ids,
)
from .pressure import (
    _stage2_frontier_config_hash,
    _compute_stage2_recipe_pressure,
)
from .printability import (
    _STAGE2_PRINTABILITY_REPAIR_MIN_MEAN_GAIN,
    _stage2_printability_ledger_diagnostics_enabled,
    _stage2_printability_failure_snapshot_from_stack_ids,
    _record_stage2_printability_ledger_snapshot,
    _apply_stage2_fine_override_printability_gate,
    _apply_stage2_localized_width_loss_boundary_nudge,
    _apply_stage2_final_color_printability_gate,
)
from .refinement import (
    _STAGE2_BOUNDARY_MUTATION_MIN_GAIN,
    _stage2_fine_override_seam_penalty_weight,
    _split_stage2_source_edge_subzones,
    _infer_implied_cap_heights,
    _selected_color_layer_count_map,
    _apply_stage2_exterior_white_guard,
    _clamp_stage2_boundary_mutation_max_passes,
    _iterate_stage2_boundary_recipe_mutation,
    _build_stage2_fine_recipe_assignments,
    _count_stage2_fine_overrides,
    _apply_stage2_fine_override_seam_gate,
)

def _materialize_recipe_assignments(
    *,
    zone_selected_stack_ids: np.ndarray,
    fine_stack_id_map: np.ndarray,
    unique_stack_dicts: dict[int, dict[str, float]],
    filament_order: tuple[str, ...] | list[str] | None = None,
) -> tuple[np.ndarray, np.ndarray, tuple[VisibleRecipe, ...], np.ndarray]:
    """Convert coarse-zone and fine-grid stack assignments into recipe labels."""
    stack_id_to_recipe_label: dict[int, int] = {}
    recipes: list[VisibleRecipe] = []
    recipe_stack_ids: list[int] = []

    def ensure_recipe_label(stack_id: int) -> int:
        recipe_label = stack_id_to_recipe_label.get(int(stack_id))
        if recipe_label is None:
            recipe_label = len(recipes)
            stack_id_to_recipe_label[int(stack_id)] = recipe_label
            recipe_stack_ids.append(int(stack_id))
            recipes.append(
                VisibleRecipe.from_mapping(
                    unique_stack_dicts[int(stack_id)],
                    filament_order=filament_order,
                )
            )
        return recipe_label

    zone_recipe_labels = np.zeros(zone_selected_stack_ids.shape[0], dtype=np.int32)
    for zone_id, stack_id in enumerate(zone_selected_stack_ids):
        if int(stack_id) < 0:
            continue
        zone_recipe_labels[zone_id] = ensure_recipe_label(int(stack_id))

    fine_recipe_label_map = np.zeros_like(fine_stack_id_map, dtype=np.int32)
    valid_mask = fine_stack_id_map >= 0
    if np.any(valid_mask):
        flat_valid_stack_ids = fine_stack_id_map[valid_mask].astype(np.int32, copy=False)
        unique_fine_stack_ids, inverse = np.unique(flat_valid_stack_ids, return_inverse=True)
        fine_recipe_labels = np.array(
            [ensure_recipe_label(int(stack_id)) for stack_id in unique_fine_stack_ids.tolist()],
            dtype=np.int32,
        )
        fine_recipe_label_map[valid_mask] = fine_recipe_labels[inverse]

    return (
        zone_recipe_labels,
        fine_recipe_label_map.astype(np.int32, copy=False),
        tuple(recipes),
        np.asarray(recipe_stack_ids, dtype=np.int32),
    )

def build_visible_plan(
    state,
    compiled_directives: QuantizedDirectiveSet,
    zone_plan: LateralZonePlan,
    diagnostics: PlanningDiagnosticsStream,
    performance_profile: StagedPerformanceProfile | None = None,
) -> VisibleRecipeRawGeometryPlan:
    """Produce the Stage 2 visible recipe + raw geometry artifact."""
    cfg = state.config
    targets = state.solve_target_oklab
    if targets is None:
        raise RuntimeError("Staged Stage 2 requires solve_target_oklab from the runner.")
    # Phase 1: project Stage 1 zones and construct candidate frontiers.
    continuity_weight = _stage2_continuity_weight(cfg)
    area_weighted_zone_choice = bool(
        cfg.stage2_area_weighted_zone_choice
    )
    pressure_frontier_rescue = bool(
        cfg.stage2_pressure_frontier_rescue
    )
    source_edge_subzones = bool(
        cfg.stage2_source_edge_subzones
    )
    fine_override_enabled = bool(
        cfg.stage2_fine_override_enabled
    )
    offset_y_px, offset_x_px = _stage1_lattice_offset_px(cfg)
    if zone_plan.coarse_to_fine_scale <= 1:
        offset_y_px = 0
        offset_x_px = 0
    frontier_config_hash = _stage2_frontier_config_hash(
        continuity_weight=continuity_weight,
        area_weighted_zone_choice=area_weighted_zone_choice,
        pressure_frontier_rescue=pressure_frontier_rescue,
        source_edge_subzones=source_edge_subzones,
        lattice_offset_y_px=offset_y_px,
        lattice_offset_x_px=offset_x_px,
    )
    zone_analysis_start = time.perf_counter()
    evaluation_shape = tuple(int(dim) for dim in compiled_directives.solver_shape)
    reuse_stage1_zone_analysis = (
        int(zone_plan.coarse_to_fine_scale) == 1
        and not source_edge_subzones
        and tuple(zone_plan.planning_shape) == evaluation_shape
    )
    if reuse_stage1_zone_analysis:
        # Factor-one Stage 1 already analyzed this exact target lattice.  Keep a
        # distinct label-map array (matching the prior artifact ownership), but
        # reuse its immutable membership, adjacency, counts, and target moments.
        evaluation_zone_label_map = np.asarray(
            zone_plan.zone_label_map,
            dtype=np.int32,
        ).copy()
    else:
        evaluation_zone_label_map = _project_zone_labels_to_fine(
            zone_plan.zone_label_map,
            zone_plan.coarse_to_fine_scale,
            evaluation_shape,
            offset_y_px=offset_y_px,
            offset_x_px=offset_x_px,
        )
    subzone_refined_zone_count = 0
    subzone_refined_pixels = 0
    if source_edge_subzones:
        (
            evaluation_zone_label_map,
            subzone_refined_zone_count,
            subzone_refined_pixels,
        ) = _split_stage2_source_edge_subzones(
            zone_label_map=evaluation_zone_label_map,
            targets=targets,
            coarse_to_fine_scale=zone_plan.coarse_to_fine_scale,
            lattice_offset_y_px=int(offset_y_px),
            lattice_offset_x_px=int(offset_x_px),
        )
    if reuse_stage1_zone_analysis:
        evaluation_zone_flat_indices = zone_plan.zone_flat_indices
        evaluation_adjacency_edges = zone_plan.adjacency_edges
        evaluation_adjacency_lengths = zone_plan.adjacency_edge_lengths_px
        evaluation_zone_pixel_counts = zone_plan.zone_pixel_counts
        evaluation_target_oklab_var_by_zone = zone_plan.target_oklab_var_by_zone
    else:
        evaluation_zone_flat_indices = _zone_flat_indices(evaluation_zone_label_map)
        evaluation_adjacency_edges, evaluation_adjacency_lengths = _build_zone_adjacency(
            evaluation_zone_label_map
        )
        evaluation_zone_pixel_counts = np.array(
            [indices.size for indices in evaluation_zone_flat_indices],
            dtype=np.int32,
        )
        _, evaluation_target_oklab_var_by_zone = _summarize_zone_targets(
            evaluation_zone_flat_indices,
            targets,
        )
    evaluation_zone_count = int(len(evaluation_zone_flat_indices))
    if performance_profile is not None:
        _record_timing(
            performance_profile,
            "stage2_zone_analysis_s",
            time.perf_counter() - zone_analysis_start,
        )

    stage2_start = time.perf_counter()

    step_start = time.perf_counter()
    thickness_result, de_flat, gamut_mask = _query_stage2_pixel_stacks(state)
    if performance_profile is not None:
        _record_timing(
            performance_profile,
            "stage2_query_pixel_stacks_s",
            time.perf_counter() - step_start,
        )
    # Config materialization canonicalizes color filaments once; stack ids must
    # inherit that exact order for both appearance lanes.
    palette = [str(fid) for fid in cfg.palette]
    step_start = time.perf_counter()
    pixel_stack_ids, _, unique_stack_dicts = _vectorized_stack_ids(
        thickness_result,
        palette,
        float(cfg.layer_height),
        max_layers=cfg.effective_max_layers(),
    )
    exterior_white_guard_stack_id: int | None = None
    if performance_profile is not None:
        _record_timing(
            performance_profile,
            "stage2_vectorized_stack_ids_s",
            time.perf_counter() - step_start,
        )
        _set_counter(
            performance_profile,
            "stage2_unique_stack_count",
            int(len(unique_stack_dicts)),
        )
        _set_counter(
            performance_profile,
            "stage2_exterior_white_guard_stack_id",
            -1
            if exterior_white_guard_stack_id is None
            else int(exterior_white_guard_stack_id),
        )

    step_start = time.perf_counter()
    if getattr(getattr(state, "appearance_provider", None), "model_kind", "") == "photo_stack_bundle":
        swap_grouping = getattr(state, "swap_grouping", None) or {}
        cap_values, all_oklabs, dense_cap_oklabs = _precompute_cap_oklabs_vectorized(
            unique_stack_dicts,
            state.appearance_provider,
            state.luts or (),
            cfg,
            palette,
            white_fill_profile=(
                state.profiles.wc_profile if swap_grouping.get("groups") else None
            ),
            band_groups=swap_grouping.get("groups"),
            band_layers=swap_grouping.get("band_layers"),
        )
    else:
        swap_grouping = getattr(state, "swap_grouping", None) or {}
        cap_values, all_oklabs = _precompute_cap_oklabs(
            unique_stack_dicts,
            state.profiles,
            cfg,
            band_groups=swap_grouping.get("groups"),
            band_layers=swap_grouping.get("band_layers"),
        )
        dense_cap_oklabs = all_oklabs
    if performance_profile is not None:
        _record_timing(
            performance_profile,
            "stage2_precompute_cap_oklabs_s",
            time.perf_counter() - step_start,
        )

    step_start = time.perf_counter()
    candidate_sets = _enumerate_zone_candidates(
        zone_flat_indices=evaluation_zone_flat_indices,
        targets=targets,
        pixel_stack_ids=pixel_stack_ids,
        unique_stack_dicts=unique_stack_dicts,
        all_oklabs=all_oklabs,
    )
    if performance_profile is not None:
        _record_timing(
            performance_profile,
            "stage2_candidate_enumeration_s",
            time.perf_counter() - step_start,
        )
        _set_counter(
            performance_profile,
            "stage2_candidate_total_count_pre_augmentation",
            int(sum(candidate_set.candidate_ids.size for candidate_set in candidate_sets)),
        )
    step_start = time.perf_counter()
    candidate_sets, augmented_zone_hits, augmented_candidate_count = (
        _augment_zone_candidates_with_neighbor_local_bests(
            zone_count=evaluation_zone_count,
            zone_flat_indices=evaluation_zone_flat_indices,
            target_oklab_var_by_zone=evaluation_target_oklab_var_by_zone,
            adjacency_edges=evaluation_adjacency_edges,
            adjacency_edge_lengths_px=evaluation_adjacency_lengths,
            candidate_sets=candidate_sets,
            targets=targets,
            unique_stack_dicts=unique_stack_dicts,
            all_oklabs=all_oklabs,
        )
    )
    if performance_profile is not None:
        _record_timing(
            performance_profile,
            "stage2_neighbor_augmentation_s",
            time.perf_counter() - step_start,
        )
        _set_counter(
            performance_profile,
            "stage2_neighbor_augmented_zone_hits",
            int(augmented_zone_hits),
        )
        _set_counter(
            performance_profile,
            "stage2_neighbor_augmented_candidate_count",
            int(augmented_candidate_count),
        )

    preprune_candidate_sets = candidate_sets
    step_start = time.perf_counter()
    candidate_sets, frontier_neighbor_match_zone_hits = _prune_zone_candidate_frontiers(
        candidate_sets,
        adjacency_edges=evaluation_adjacency_edges,
        adjacency_edge_lengths_px=evaluation_adjacency_lengths,
    )
    if performance_profile is not None:
        _record_timing(
            performance_profile,
            "stage2_frontier_pruning_s",
            time.perf_counter() - step_start,
        )
        frontier_sizes = [candidate_set.local_scores.size for candidate_set in candidate_sets]
        _set_counter(
            performance_profile,
            "stage2_frontier_total_count_post_prune",
            int(sum(frontier_sizes)),
        )
        _set_counter(
            performance_profile,
            "stage2_frontier_mean_size",
            float(np.mean(frontier_sizes)) if frontier_sizes else 0.0,
        )
        _set_counter(
            performance_profile,
            "stage2_frontier_max_size",
            int(max(frontier_sizes)) if frontier_sizes else 0,
        )
        _set_counter(
            performance_profile,
            "stage2_frontier_neighbor_match_zone_hits",
            int(frontier_neighbor_match_zone_hits),
        )

    step_start = time.perf_counter()
    (
        candidate_sets,
        frontier_optical_rescue_zone_hits,
        frontier_optical_rescue_candidate_count,
        frontier_pressure_rescue_candidate_count,
    ) = (
        _rescue_stage2_optical_frontier_candidates(
            preprune_candidate_sets=preprune_candidate_sets,
            pruned_candidate_sets=candidate_sets,
            zone_flat_indices=evaluation_zone_flat_indices if pressure_frontier_rescue else None,
            targets=targets if pressure_frontier_rescue else None,
            all_oklabs=all_oklabs if pressure_frontier_rescue else None,
        )
    )
    if performance_profile is not None:
        _record_timing(
            performance_profile,
            "stage2_frontier_optical_rescue_s",
            time.perf_counter() - step_start,
        )
        rescued_frontier_sizes = [candidate_set.local_scores.size for candidate_set in candidate_sets]
        _set_counter(
            performance_profile,
            "stage2_frontier_optical_rescue_zone_hits",
            int(frontier_optical_rescue_zone_hits),
        )
        _set_counter(
            performance_profile,
            "stage2_frontier_optical_rescue_candidate_count",
            int(frontier_optical_rescue_candidate_count),
        )
        _set_counter(
            performance_profile,
            "stage2_frontier_pressure_rescue_candidate_count",
            int(frontier_pressure_rescue_candidate_count),
        )
        _set_counter(
            performance_profile,
            "stage2_frontier_total_count_post_rescue",
            int(sum(rescued_frontier_sizes)),
        )
        _set_counter(
            performance_profile,
            "stage2_frontier_mean_size_post_rescue",
            float(np.mean(rescued_frontier_sizes)) if rescued_frontier_sizes else 0.0,
        )
        _set_counter(
            performance_profile,
            "stage2_frontier_max_size_post_rescue",
            int(max(rescued_frontier_sizes)) if rescued_frontier_sizes else 0,
        )

    # Phase 2: select one visible recipe per zone.
    step_start = time.perf_counter()
    zone_local_cost_weights = (
        _zone_local_cost_weights(
            evaluation_zone_pixel_counts,
            evaluation_zone_count,
        )
        if area_weighted_zone_choice
        else np.ones(evaluation_zone_count, dtype=np.float32)
    )
    beam_seed = _seed_zone_recipe_labels_with_beam(
        zone_count=evaluation_zone_count,
        adjacency_edges=evaluation_adjacency_edges,
        adjacency_edge_lengths_px=evaluation_adjacency_lengths,
        zone_pixel_counts=evaluation_zone_pixel_counts,
        target_oklab_var_by_zone=evaluation_target_oklab_var_by_zone,
        candidate_sets=candidate_sets,
        local_cost_weights=zone_local_cost_weights,
        continuity_weight=continuity_weight,
    )
    if performance_profile is not None:
        _record_timing(
            performance_profile,
            "stage2_beam_seed_s",
            time.perf_counter() - step_start,
        )
        _set_counter(
            performance_profile,
            "stage2_beam_width",
            int(_STAGE2_BEAM_WIDTH),
        )
        _set_counter(
            performance_profile,
            "stage2_continuity_weight",
            float(continuity_weight),
        )
        _set_counter(
            performance_profile,
            "stage2_area_weighted_zone_choice_enabled",
            bool(area_weighted_zone_choice),
        )
        _set_counter(
            performance_profile,
            "stage2_pressure_frontier_rescue_enabled",
            bool(pressure_frontier_rescue),
        )
        _set_counter(
            performance_profile,
            "stage2_source_edge_subzones_enabled",
            bool(source_edge_subzones),
        )
        _set_counter(
            performance_profile,
            "stage2_fine_override_enabled",
            bool(fine_override_enabled),
        )
        _set_counter(
            performance_profile,
            "stage1_lattice_offset_y_px",
            int(offset_y_px),
        )
        _set_counter(
            performance_profile,
            "stage1_lattice_offset_x_px",
            int(offset_x_px),
        )
        _set_counter(
            performance_profile,
            "stage2_source_edge_subzone_refined_zones",
            int(subzone_refined_zone_count),
        )
        _set_counter(
            performance_profile,
            "stage2_source_edge_subzone_refined_pixels",
            int(subzone_refined_pixels),
        )
        _set_counter(
            performance_profile,
            "stage2_beam_expansion_count",
            int(beam_seed.expansion_count),
        )
        _set_counter(
            performance_profile,
            "stage2_beam_max_size",
            int(beam_seed.max_beam_size),
        )
        _set_counter(
            performance_profile,
            "stage2_zone_local_cost_weight_min",
            float(np.min(zone_local_cost_weights)) if zone_local_cost_weights.size else 1.0,
        )
        _set_counter(
            performance_profile,
            "stage2_zone_local_cost_weight_max",
            float(np.max(zone_local_cost_weights)) if zone_local_cost_weights.size else 1.0,
        )

    step_start = time.perf_counter()
    optimization = _optimize_zone_recipe_labels(
        candidate_sets=candidate_sets,
        adjacency_edges=evaluation_adjacency_edges,
        adjacency_edge_lengths_px=evaluation_adjacency_lengths,
        zone_pixel_counts=evaluation_zone_pixel_counts,
        local_cost_weights=zone_local_cost_weights,
        initial_selected_stack_ids=beam_seed.selected_stack_ids,
        continuity_weight=continuity_weight,
    )
    if performance_profile is not None:
        _record_timing(
            performance_profile,
            "stage2_optimize_assignments_s",
            time.perf_counter() - step_start,
        )
        _record_timing(
            performance_profile,
            "stage2_coord_descent_s",
            optimization.coord_descent_elapsed_s,
        )
        _record_timing(
            performance_profile,
            "stage2_pair_repair_s",
            optimization.pair_repair_elapsed_s,
        )
        _set_counter(
            performance_profile,
            "stage2_changed_zone_count",
            int(optimization.changed_zone_count),
        )
        _set_counter(
            performance_profile,
            "stage2_coord_descent_pass_count",
            int(optimization.coord_descent_pass_count),
        )
        _set_counter(
            performance_profile,
            "stage2_coord_descent_eval_count",
            int(optimization.coord_descent_eval_count),
        )
        _set_counter(
            performance_profile,
            "stage2_pair_repair_pass_count",
            int(optimization.pair_repair_pass_count),
        )
        _set_counter(
            performance_profile,
            "stage2_pair_repair_trial_count",
            int(optimization.pair_repair_trial_count),
        )
        _set_counter(
            performance_profile,
            "stage2_pair_repair_zone_changes",
            int(optimization.pair_repair_zone_changes),
        )

    step_start = time.perf_counter()
    objective_summary = _build_stage2_objective_summary(
        zone_count=evaluation_zone_count,
        zone_pixel_counts=evaluation_zone_pixel_counts,
        target_oklab_var_by_zone=evaluation_target_oklab_var_by_zone,
        adjacency_edges=evaluation_adjacency_edges,
        adjacency_edge_lengths_px=evaluation_adjacency_lengths,
        candidate_sets=candidate_sets,
        optimization=optimization,
        continuity_weight=continuity_weight,
        retaining_wall_weight=_STAGE2_RETAINING_WALL_WEIGHT,
        local_cost_weights=zone_local_cost_weights,
    )
    if performance_profile is not None:
        _record_timing(
            performance_profile,
            "stage2_objective_summary_s",
            time.perf_counter() - step_start,
        )

    pressure_diagnostic = None
    if bool(cfg.emit_pressure_diagnostics) or bool(
        cfg.emit_geometry_attribution
    ):
        step_start = time.perf_counter()
        pressure_diagnostic = _compute_stage2_recipe_pressure(
            fine_shape=evaluation_shape,
            coarse_to_fine_scale=zone_plan.coarse_to_fine_scale,
            lattice_offset_y_px=int(offset_y_px),
            lattice_offset_x_px=int(offset_x_px),
            zone_label_map=evaluation_zone_label_map,
            zone_flat_indices=evaluation_zone_flat_indices,
            targets=targets,
            pixel_stack_ids=pixel_stack_ids,
            preprune_candidate_sets=preprune_candidate_sets,
            pruned_candidate_sets=candidate_sets,
            optimization=optimization,
            all_oklabs=all_oklabs,
            frontier_config_hash=frontier_config_hash,
        )
        pressure_elapsed = time.perf_counter() - step_start
        if performance_profile is not None:
            _record_timing(
                performance_profile,
                "stage2_recipe_pressure_diagnostics_s",
                pressure_elapsed,
            )
            _set_counter(
                performance_profile,
                "stage2_pressure_coarse_excess_pixels",
                int(np.count_nonzero(pressure_diagnostic.coarse_excess > _STAGE2_PRESSURE_ACTIVE_THRESHOLD)),
            )
            _set_counter(
                performance_profile,
                "stage2_pressure_pruning_gap_pixels",
                int(np.count_nonzero(pressure_diagnostic.pruning_gap > _STAGE2_PRESSURE_ACTIVE_THRESHOLD)),
            )
            _set_counter(
                performance_profile,
                "stage2_pressure_local_gap_pixels",
                int(np.count_nonzero(pressure_diagnostic.local_gap > _STAGE2_PRESSURE_ACTIVE_THRESHOLD)),
            )
            _set_counter(
                performance_profile,
                "stage2_pressure_total_excess_pixels",
                int(np.count_nonzero(pressure_diagnostic.total_excess > _STAGE2_PRESSURE_ACTIVE_THRESHOLD)),
            )
            _set_counter(
                performance_profile,
                "stage2_pressure_negative_gap_violation_pixels",
                int(pressure_diagnostic.negative_gap_violation_pixels),
            )
            _set_counter(
                performance_profile,
                "stage2_pressure_cross_boundary_pixels",
                int(pressure_diagnostic.cross_boundary_pressure_pixels),
            )
            _set_counter(
                performance_profile,
                "stage2_pressure_blockiness_energy_ratio",
                float(pressure_diagnostic.blockiness_energy_ratio),
            )
            _set_counter(
                performance_profile,
                "stage2_pressure_x_image_edge_corr",
                float(pressure_diagnostic.pressure_x_image_edge_corr),
            )
        diagnostics.entries.append(
            PlanningDiagnosticEntry(
                code="stage2_recipe_pressure_diagnostics",
                severity="warning"
                if pressure_diagnostic.negative_gap_violation_pixels
                else "info",
                message=(
                    "Stage 2 recipe-pressure diagnostics emitted "
                    f"coarse_excess_pixels={int(np.count_nonzero(pressure_diagnostic.coarse_excess > _STAGE2_PRESSURE_ACTIVE_THRESHOLD))}, "
                    f"pruning_gap_pixels={int(np.count_nonzero(pressure_diagnostic.pruning_gap > _STAGE2_PRESSURE_ACTIVE_THRESHOLD))}, "
                    f"local_gap_pixels={int(np.count_nonzero(pressure_diagnostic.local_gap > _STAGE2_PRESSURE_ACTIVE_THRESHOLD))}."
                ),
            )
        )

    # Phase 3: refine the zone solution onto the fine solve lattice.
    step_start = time.perf_counter()
    detail_step_start = time.perf_counter()
    zone_selected_stack_ids = _selected_zone_stack_ids(candidate_sets, optimization)
    if fine_override_enabled:
        (
            fine_stack_id_map,
            detail_override_pixels,
            detail_override_zones,
            interior_override_pixels,
            interior_override_zones,
        ) = _build_stage2_fine_recipe_assignments(
            fine_shape=evaluation_shape,
            coarse_to_fine_scale=zone_plan.coarse_to_fine_scale,
            zone_flat_indices=evaluation_zone_flat_indices,
            target_oklab_var_by_zone=evaluation_target_oklab_var_by_zone,
            targets=targets,
            pixel_stack_ids=pixel_stack_ids,
            candidate_sets=candidate_sets,
            optimization=optimization,
            all_oklabs=all_oklabs,
        )
    else:
        fine_stack_id_map = np.full(evaluation_shape, -1, dtype=np.int32)
        flat_fine_stack_ids = fine_stack_id_map.reshape(-1)
        for zone_id, indices in enumerate(evaluation_zone_flat_indices):
            if zone_id >= zone_selected_stack_ids.size or indices.size == 0:
                continue
            flat_fine_stack_ids[indices] = int(zone_selected_stack_ids[zone_id])
        detail_override_pixels = 0
        detail_override_zones = 0
        interior_override_pixels = 0
        interior_override_zones = 0
    enforce_printability = _printability_enforcement_enabled(cfg)
    stage2_printability_ledger_enabled = bool(
        performance_profile is not None
        and _stage2_printability_ledger_diagnostics_enabled(cfg)
    )
    stage2_printability_ledger_settings = (
        resolve_blueprint_printability_settings(
            cfg,
            pitch_mm=float(cfg.solver_fine_pitch_mm),
        )
        if stage2_printability_ledger_enabled
        else None
    )
    stage2_printability_ledger_previous: _Stage2PrintabilityFailureSnapshot | None = None

    def _record_stage2_printability_ledger(
        label: str,
        stack_map: np.ndarray,
    ) -> None:
        nonlocal stage2_printability_ledger_previous
        if (
            not stage2_printability_ledger_enabled
            or performance_profile is None
            or stage2_printability_ledger_settings is None
        ):
            return
        snapshot = _stage2_printability_failure_snapshot_from_stack_ids(
            fine_stack_id_map=stack_map,
            unique_stack_dicts=unique_stack_dicts,
            palette_order=tuple(str(fid) for fid in cfg.palette),
            layer_height_mm=float(cfg.layer_height),
            settings=stage2_printability_ledger_settings,
            minimum_cap_height_mm=float(cfg.d_wc_min),
        )
        stage2_printability_ledger_previous = _record_stage2_printability_ledger_snapshot(
            performance_profile,
            label=str(label),
            snapshot=snapshot,
            previous=stage2_printability_ledger_previous,
        )

    # Phase 4: enforce visible-material printability and boundary policy.
    _record_stage2_printability_ledger("after_fine_assignment", fine_stack_id_map)
    seam_gate_rejected_pixels = 0
    seam_gate_rejected_components = 0
    seam_gate_accepted_components = 0
    printability_gate_rejection_map: np.ndarray | None = None
    printability_gate_repair_map: np.ndarray | None = None
    printability_gate_rejected_pixels = 0
    printability_gate_rejected_components = 0
    printability_gate_accepted_components = 0
    printability_gate_repaired_components = 0
    printability_gate_repaired_original_pixels = 0
    printability_gate_repaired_added_pixels = 0
    printability_gate_repair_rejected_components = 0
    printability_gate_repair_rejected_pixels = 0
    printability_gate_rejected_tiny_pixels = 0
    printability_gate_rejected_tiny_components = 0
    printability_gate_rejected_narrow_pixels = 0
    printability_gate_rejected_narrow_components = 0
    printability_gate_rejected_short_pixels = 0
    printability_gate_rejected_short_components = 0
    final_substrate_repair_map: np.ndarray | None = None
    localized_width_nudge_map: np.ndarray | None = None
    exterior_white_guard_map: np.ndarray | None = None
    localized_width_nudge_candidate_pixels = 0
    localized_width_nudge_accepted_pixels = 0
    localized_width_nudge_accepted_components = 0
    localized_width_nudge_rejected_pixels = 0
    localized_width_nudge_rejected_components = 0
    localized_width_nudge_edge_delta = 0
    localized_width_nudge_pass_count = 0
    exterior_white_guard_pixels = 0
    exterior_white_guard_changed_pixels = 0
    final_substrate_absorbed_pixels = 0
    final_substrate_absorbed_components = 0
    final_substrate_unresolved_components = 0
    boundary_mutation_map: np.ndarray | None = None
    boundary_mutation_candidate_pixels = 0
    boundary_mutation_accepted_pixels = 0
    boundary_mutation_accepted_components = 0
    boundary_mutation_min_component_pixels = 0
    boundary_mutation_rejected_small_pixels = 0
    boundary_mutation_rejected_small_components = 0
    boundary_mutation_rejected_weak_pixels = 0
    boundary_mutation_rejected_weak_components = 0
    boundary_mutation_current_de_threshold = 0.0
    boundary_mutation_current_de_eligible_pixels = 0
    boundary_mutation_mean_gain = 0.0
    boundary_mutation_p95_gain = 0.0
    boundary_mutation_passes_run = 0
    boundary_mutation_pass_accepted_pixels: list[int] = []
    printability_repair_min_mean_gain = _STAGE2_PRINTABILITY_REPAIR_MIN_MEAN_GAIN
    if bool(cfg.stage2_seam_aware_fine_override):
        (
            fine_stack_id_map,
            seam_gate_rejected_pixels,
            seam_gate_rejected_components,
            seam_gate_accepted_components,
        ) = _apply_stage2_fine_override_seam_gate(
            fine_stack_id_map=fine_stack_id_map,
            fine_shape=evaluation_shape,
            zone_flat_indices=evaluation_zone_flat_indices,
            selected_zone_stack_ids=zone_selected_stack_ids,
            targets=targets,
            unique_stack_dicts=unique_stack_dicts,
            all_oklabs=all_oklabs,
            seam_penalty_weight=_stage2_fine_override_seam_penalty_weight(cfg),
        )
        _record_stage2_printability_ledger("after_seam_gate", fine_stack_id_map)
    printability_gate_requested = bool(
        cfg.stage2_printability_gate_fine_override
    ) or bool(enforce_printability)
    printability_repair_enabled = bool(
        cfg.stage2_printability_repair_fine_override
    ) or bool(enforce_printability)
    printability_gate_enabled = bool(printability_gate_requested or printability_repair_enabled)
    if printability_gate_enabled:
        printability_gate_start = time.perf_counter()
        printability_settings = resolve_blueprint_printability_settings(
            cfg,
            pitch_mm=float(cfg.solver_fine_pitch_mm),
        )
        repair_min_mean_gain = cfg.stage2_printability_repair_min_mean_gain
        if repair_min_mean_gain is None:
            repair_min_mean_gain = _STAGE2_PRINTABILITY_REPAIR_MIN_MEAN_GAIN
        printability_repair_min_mean_gain = float(repair_min_mean_gain)
        printability_gate = _apply_stage2_fine_override_printability_gate(
            fine_stack_id_map=fine_stack_id_map,
            fine_shape=evaluation_shape,
            zone_flat_indices=evaluation_zone_flat_indices,
            selected_zone_stack_ids=zone_selected_stack_ids,
            settings=printability_settings,
            repair_enabled=printability_repair_enabled,
            targets=targets,
            all_oklabs=all_oklabs,
            repair_min_mean_gain=float(printability_repair_min_mean_gain),
        )
        fine_stack_id_map = printability_gate.fine_stack_id_map
        printability_gate_rejection_map = printability_gate.rejection_map
        printability_gate_repair_map = printability_gate.repair_map
        printability_gate_rejected_pixels = int(printability_gate.rejected_pixels)
        printability_gate_rejected_components = int(printability_gate.rejected_components)
        printability_gate_accepted_components = int(printability_gate.accepted_components)
        printability_gate_repaired_components = int(printability_gate.repaired_components)
        printability_gate_repaired_original_pixels = int(
            printability_gate.repaired_original_pixels
        )
        printability_gate_repaired_added_pixels = int(printability_gate.repaired_added_pixels)
        printability_gate_repair_rejected_components = int(
            printability_gate.repair_rejected_components
        )
        printability_gate_repair_rejected_pixels = int(printability_gate.repair_rejected_pixels)
        printability_gate_rejected_tiny_pixels = int(printability_gate.rejected_tiny_pixels)
        printability_gate_rejected_tiny_components = int(
            printability_gate.rejected_tiny_components
        )
        printability_gate_rejected_narrow_pixels = int(printability_gate.rejected_narrow_pixels)
        printability_gate_rejected_narrow_components = int(
            printability_gate.rejected_narrow_components
        )
        printability_gate_rejected_short_pixels = int(printability_gate.rejected_short_pixels)
        printability_gate_rejected_short_components = int(
            printability_gate.rejected_short_components
        )
        if performance_profile is not None:
            _record_timing(
                performance_profile,
                "stage2_fine_override_printability_gate_s",
                time.perf_counter() - printability_gate_start,
            )
        _record_stage2_printability_ledger(
            "after_fine_override_printability_gate",
            fine_stack_id_map,
        )

    # Printability chain of custody:
    # - The fine-override gate owns optional Stage 2 detail islands. It may grow
    #   a useful island into a printable footprint; otherwise it reverts the
    #   island to the owning coarse-zone recipe.
    # - Boundary mutation is an optional contour refinement. With global
    #   enforcement on, its edge-run mode keeps accepted moves attached to
    #   printable boundary runs.
    # - The final substrate repair owns the Stage 2 -> Stage 4 handoff. It
    #   absorbs any remaining hard-fail color components, plus color-height
    #   pits/cliffs that would force unprintable mandatory white-cap islands.
    #   Stage 4 then owns boundary-cap repair and optional-detail suppression.
    boundary_mutation_enabled = bool(
        cfg.stage2_boundary_mutation_enabled
    )
    boundary_mutation_segment_mode = False
    boundary_mutation_edge_run_mode = True
    boundary_mutation_current_de_percentile = cfg.stage2_boundary_mutation_current_de_percentile
    boundary_mutation_max_passes = _clamp_stage2_boundary_mutation_max_passes(
        getattr(cfg, "stage2_boundary_mutation_max_passes", 1)
    )
    if boundary_mutation_enabled:
        boundary_mutation_start = time.perf_counter()
        min_gain = cfg.stage2_boundary_mutation_min_gain
        if min_gain is None:
            min_gain = _STAGE2_BOUNDARY_MUTATION_MIN_GAIN
        min_component_mm = cfg.stage2_boundary_mutation_min_component_mm
        if min_component_mm is not None and float(min_component_mm) > 0.0:
            pitch_mm = max(float(cfg.solver_fine_pitch_mm), 1e-9)
            boundary_mutation_min_component_pixels = int(
                math.ceil(float(min_component_mm) / pitch_mm)
            )
        (
            boundary_mutation,
            boundary_mutation_passes_run,
            boundary_mutation_pass_accepted_pixels,
        ) = _iterate_stage2_boundary_recipe_mutation(
            fine_stack_id_map=fine_stack_id_map,
            targets=targets,
            all_oklabs=all_oklabs,
            min_gain=float(min_gain),
            min_component_pixels=int(boundary_mutation_min_component_pixels),
            current_de_percentile=boundary_mutation_current_de_percentile,
            max_passes=boundary_mutation_max_passes,
        )
        fine_stack_id_map = boundary_mutation.fine_stack_id_map
        boundary_mutation_map = boundary_mutation.mutation_map
        boundary_mutation_candidate_pixels = int(boundary_mutation.candidate_pixels)
        boundary_mutation_accepted_pixels = int(boundary_mutation.accepted_pixels)
        boundary_mutation_accepted_components = int(boundary_mutation.accepted_components)
        boundary_mutation_rejected_small_pixels = int(
            boundary_mutation.rejected_small_pixels
        )
        boundary_mutation_rejected_small_components = int(
            boundary_mutation.rejected_small_components
        )
        boundary_mutation_rejected_weak_pixels = int(boundary_mutation.rejected_weak_pixels)
        boundary_mutation_rejected_weak_components = int(
            boundary_mutation.rejected_weak_components
        )
        boundary_mutation_current_de_threshold = float(
            boundary_mutation.current_de_threshold
        )
        boundary_mutation_current_de_eligible_pixels = int(
            boundary_mutation.current_de_eligible_pixels
        )
        boundary_mutation_mean_gain = float(boundary_mutation.mean_gain)
        boundary_mutation_p95_gain = float(boundary_mutation.p95_gain)
        if performance_profile is not None:
            _record_timing(
                performance_profile,
                "stage2_boundary_mutation_s",
                time.perf_counter() - boundary_mutation_start,
            )
        _record_stage2_printability_ledger("after_boundary_mutation", fine_stack_id_map)
    final_substrate_repair_enabled = bool(
        cfg.stage2_final_printability_gate_fine_override
    ) or bool(_printability_enforcement_enabled(cfg))
    localized_width_nudge_enabled = bool(
        final_substrate_repair_enabled
        and _printability_enforcement_enabled(cfg)
    )
    if final_substrate_repair_enabled:
        final_substrate_repair_start = time.perf_counter()
        printability_settings = resolve_blueprint_printability_settings(
            cfg,
            pitch_mm=float(cfg.solver_fine_pitch_mm),
        )
        final_substrate_repair = _apply_stage2_final_color_printability_gate(
            fine_stack_id_map=fine_stack_id_map,
            fine_shape=evaluation_shape,
            zone_flat_indices=evaluation_zone_flat_indices,
            selected_zone_stack_ids=zone_selected_stack_ids,
            unique_stack_dicts=unique_stack_dicts,
            palette_order=tuple(str(fid) for fid in cfg.palette),
            layer_height_mm=float(cfg.layer_height),
            settings=printability_settings,
            minimum_cap_height_mm=float(cfg.d_wc_min),
            targets=targets,
            all_oklabs=all_oklabs,
            apply_changes=True,
        )
        fine_stack_id_map = final_substrate_repair.fine_stack_id_map
        final_substrate_repair_map = final_substrate_repair.absorption_map
        final_substrate_absorbed_pixels = int(final_substrate_repair.absorbed_pixels)
        final_substrate_absorbed_components = int(
            final_substrate_repair.absorbed_components
        )
        final_substrate_unresolved_components = int(
            final_substrate_repair.unresolved_components
        )
        if performance_profile is not None:
            _record_timing(
                performance_profile,
                "stage2_final_substrate_repair_s",
                time.perf_counter() - final_substrate_repair_start,
            )
        _record_stage2_printability_ledger(
            "after_final_substrate_repair",
            fine_stack_id_map,
        )
    if localized_width_nudge_enabled:
        localized_width_nudge_start = time.perf_counter()
        printability_settings = resolve_blueprint_printability_settings(
            cfg,
            pitch_mm=float(cfg.solver_fine_pitch_mm),
        )
        for _localized_pass in range(4):
            localized_width_nudge = _apply_stage2_localized_width_loss_boundary_nudge(
                fine_stack_id_map=fine_stack_id_map,
                unique_stack_dicts=unique_stack_dicts,
                palette_order=tuple(str(fid) for fid in cfg.palette),
                layer_height_mm=float(cfg.layer_height),
                minimum_cap_height_mm=float(cfg.d_wc_min),
                settings=printability_settings,
            )
            localized_width_nudge_pass_count += 1
            localized_width_nudge_candidate_pixels += int(
                localized_width_nudge.candidate_pixels
            )
            localized_width_nudge_accepted_pixels += int(
                localized_width_nudge.accepted_pixels
            )
            localized_width_nudge_accepted_components += int(
                localized_width_nudge.accepted_components
            )
            localized_width_nudge_rejected_pixels += int(
                localized_width_nudge.rejected_pixels
            )
            localized_width_nudge_rejected_components += int(
                localized_width_nudge.rejected_components
            )
            localized_width_nudge_edge_delta += int(localized_width_nudge.edge_delta)
            fine_stack_id_map = localized_width_nudge.fine_stack_id_map
            if localized_width_nudge_map is None:
                localized_width_nudge_map = localized_width_nudge.mutation_map
            else:
                localized_width_nudge_map = np.maximum(
                    localized_width_nudge_map.astype(np.uint8, copy=False),
                    localized_width_nudge.mutation_map.astype(np.uint8, copy=False),
                )
            if (
                int(localized_width_nudge.candidate_pixels) <= 0
                or int(localized_width_nudge.accepted_pixels) <= 0
            ):
                break
        if performance_profile is not None:
            _record_timing(
                performance_profile,
                "stage2_localized_width_nudge_s",
                time.perf_counter() - localized_width_nudge_start,
            )
        _record_stage2_printability_ledger("after_localized_width_nudge", fine_stack_id_map)
    guard_step_start = time.perf_counter()
    (
        fine_stack_id_map,
        exterior_white_guard_map,
        exterior_white_guard_pixels,
        exterior_white_guard_changed_pixels,
    ) = _apply_stage2_exterior_white_guard(
        fine_stack_id_map=fine_stack_id_map,
        white_guard_stack_id=exterior_white_guard_stack_id,
        config=cfg,
    )
    if performance_profile is not None:
        _record_timing(
            performance_profile,
            "stage2_exterior_white_guard_s",
            time.perf_counter() - guard_step_start,
        )
        _set_counter(
            performance_profile,
            "stage2_exterior_white_guard_pixels",
            int(exterior_white_guard_pixels),
        )
        _set_counter(
            performance_profile,
            "stage2_exterior_white_guard_changed_pixels",
            int(exterior_white_guard_changed_pixels),
        )
    if int(exterior_white_guard_changed_pixels) > 0:
        diagnostics.entries.append(
            PlanningDiagnosticEntry(
                code="stage2_exterior_white_guard",
                severity="info",
                message=(
                    "Stage 2 reserved exterior guard pixels as white-only "
                    f"material: {int(exterior_white_guard_changed_pixels)} changed "
                    f"of {int(exterior_white_guard_pixels)} guard pixels."
                ),
            )
        )
        _record_stage2_printability_ledger("after_exterior_white_guard", fine_stack_id_map)
    elif int(exterior_white_guard_pixels) > 0:
        diagnostics.entries.append(
            PlanningDiagnosticEntry(
                code="stage2_exterior_white_guard",
                severity="info",
                message=(
                    "Stage 2 marked exterior guard pixels without changing "
                    f"material assignments: {int(exterior_white_guard_pixels)} "
                    "guard pixels."
                ),
            )
        )
    _record_stage2_printability_ledger("final", fine_stack_id_map)
    final_detail_override_pixels, final_detail_override_zones = _count_stage2_fine_overrides(
        fine_stack_id_map=fine_stack_id_map,
        zone_flat_indices=evaluation_zone_flat_indices,
        selected_zone_stack_ids=zone_selected_stack_ids,
    )
    # Phase 5: materialize recipes and the geometry fields consumed by Stage 4.
    detail_assignment_elapsed = time.perf_counter() - detail_step_start
    label_step_start = time.perf_counter()
    (
        zone_recipe_labels,
        fine_recipe_label_map,
        recipes,
        recipe_stack_ids,
    ) = _materialize_recipe_assignments(
        zone_selected_stack_ids=zone_selected_stack_ids,
        fine_stack_id_map=fine_stack_id_map,
        unique_stack_dicts=unique_stack_dicts,
        filament_order=tuple(str(fid) for fid in cfg.palette),
    )
    label_materialization_elapsed = time.perf_counter() - label_step_start
    shield_floor_step_start = time.perf_counter()
    selected_color_layers = _selected_color_layer_count_map(
        fine_stack_id_map=fine_stack_id_map,
        unique_stack_dicts=unique_stack_dicts,
        layer_height_mm=float(cfg.layer_height),
    )
    mandatory_lateral_shield_floor_layers = lateral_boundary_shield_floor_layers(
        selected_color_layers
    )
    mandatory_lateral_shield_floor_mm = (
        mandatory_lateral_shield_floor_layers.astype(np.float32)
        * np.float32(float(cfg.layer_height))
    ).astype(np.float32, copy=False)
    shield_floor_elapsed = time.perf_counter() - shield_floor_step_start
    cap_step_start = time.perf_counter()
    implied_cap_height = _infer_implied_cap_heights(
        fine_shape=evaluation_shape,
        targets=targets,
        fine_stack_id_map=fine_stack_id_map,
        all_oklabs=all_oklabs,
        cap_values=cap_values,
        minimum_cap_height_mm=mandatory_lateral_shield_floor_mm,
    )
    implied_cap_elapsed = time.perf_counter() - cap_step_start
    # Projection only: preserve the established counters and diagnostics after
    # all solver decisions have been made.
    if performance_profile is not None:
        _record_timing(
            performance_profile,
            "stage2_materialize_recipes_s",
            time.perf_counter() - step_start,
        )
        _record_timing(
            performance_profile,
            "stage2_detail_override_assignment_s",
            detail_assignment_elapsed,
        )
        _record_timing(
            performance_profile,
            "stage2_recipe_label_materialization_s",
            label_materialization_elapsed,
        )
        _record_timing(
            performance_profile,
            "stage2_implied_cap_map_s",
            implied_cap_elapsed,
        )
        _record_timing(
            performance_profile,
            "stage2_lateral_boundary_shield_floor_s",
            shield_floor_elapsed,
        )
        _set_counter(
            performance_profile,
            "stage2_lateral_boundary_shield_floor_layer_pixels",
            int(np.sum(mandatory_lateral_shield_floor_layers)),
        )
        _set_counter(
            performance_profile,
            "stage2_lateral_boundary_shield_floor_active_pixels",
            int(np.count_nonzero(mandatory_lateral_shield_floor_layers > 0)),
        )
        _set_counter(
            performance_profile,
            "stage2_lateral_boundary_shield_floor_max_layers",
            int(np.max(mandatory_lateral_shield_floor_layers, initial=0)),
        )
        _set_counter(
            performance_profile,
            "stage2_recipe_count",
            int(len(recipes)),
        )
        _set_counter(
            performance_profile,
            "stage2_detail_override_pixels",
            int(final_detail_override_pixels),
        )
        _set_counter(
            performance_profile,
            "stage2_detail_override_zones",
            int(final_detail_override_zones),
        )
        _set_counter(
            performance_profile,
            "stage2_fine_override_enabled",
            bool(fine_override_enabled),
        )
        _set_counter(
            performance_profile,
            "stage2_detail_override_pixels_before_seam_gate",
            int(detail_override_pixels),
        )
        _set_counter(
            performance_profile,
            "stage2_detail_override_zones_before_seam_gate",
            int(detail_override_zones),
        )
        _set_counter(
            performance_profile,
            "stage2_seam_aware_fine_override_enabled",
            bool(cfg.stage2_seam_aware_fine_override),
        )
        _set_counter(
            performance_profile,
            "stage2_fine_override_seam_penalty_weight",
            float(_stage2_fine_override_seam_penalty_weight(cfg)),
        )
        _set_counter(
            performance_profile,
            "stage2_seam_aware_fine_override_rejected_pixels",
            int(seam_gate_rejected_pixels),
        )
        _set_counter(
            performance_profile,
            "stage2_seam_aware_fine_override_rejected_components",
            int(seam_gate_rejected_components),
        )
        _set_counter(
            performance_profile,
            "stage2_seam_aware_fine_override_accepted_components",
            int(seam_gate_accepted_components),
        )
        _set_counter(
            performance_profile,
            "stage2_printability_gate_fine_override_enabled",
            bool(printability_gate_enabled),
        )
        _set_counter(
            performance_profile,
            "stage2_printability_repair_fine_override_enabled",
            bool(printability_repair_enabled),
        )
        _set_counter(
            performance_profile,
            "stage2_printability_repair_min_mean_gain",
            float(printability_repair_min_mean_gain),
        )
        _set_counter(
            performance_profile,
            "stage2_printability_gate_rejected_pixels",
            int(printability_gate_rejected_pixels),
        )
        _set_counter(
            performance_profile,
            "stage2_printability_gate_rejected_components",
            int(printability_gate_rejected_components),
        )
        _set_counter(
            performance_profile,
            "stage2_printability_gate_accepted_components",
            int(printability_gate_accepted_components),
        )
        _set_counter(
            performance_profile,
            "stage2_printability_repair_components",
            int(printability_gate_repaired_components),
        )
        _set_counter(
            performance_profile,
            "stage2_printability_repair_original_pixels",
            int(printability_gate_repaired_original_pixels),
        )
        _set_counter(
            performance_profile,
            "stage2_printability_repair_added_pixels",
            int(printability_gate_repaired_added_pixels),
        )
        _set_counter(
            performance_profile,
            "stage2_printability_repair_rejected_components",
            int(printability_gate_repair_rejected_components),
        )
        _set_counter(
            performance_profile,
            "stage2_printability_repair_rejected_pixels",
            int(printability_gate_repair_rejected_pixels),
        )
        _set_counter(
            performance_profile,
            "stage2_printability_gate_rejected_tiny_pixels",
            int(printability_gate_rejected_tiny_pixels),
        )
        _set_counter(
            performance_profile,
            "stage2_printability_gate_rejected_tiny_components",
            int(printability_gate_rejected_tiny_components),
        )
        _set_counter(
            performance_profile,
            "stage2_printability_gate_rejected_narrow_pixels",
            int(printability_gate_rejected_narrow_pixels),
        )
        _set_counter(
            performance_profile,
            "stage2_printability_gate_rejected_narrow_components",
            int(printability_gate_rejected_narrow_components),
        )
        _set_counter(
            performance_profile,
            "stage2_printability_gate_rejected_short_pixels",
            int(printability_gate_rejected_short_pixels),
        )
        _set_counter(
            performance_profile,
            "stage2_printability_gate_rejected_short_components",
            int(printability_gate_rejected_short_components),
        )
        _set_counter(
            performance_profile,
            "stage2_final_substrate_repair_enabled",
            bool(final_substrate_repair_enabled),
        )
        _set_counter(
            performance_profile,
            "stage2_localized_width_nudge_candidate_pixels",
            int(localized_width_nudge_candidate_pixels),
        )
        _set_counter(
            performance_profile,
            "stage2_localized_width_nudge_enabled",
            bool(localized_width_nudge_enabled),
        )
        _set_counter(
            performance_profile,
            "stage2_localized_width_nudge_accepted_pixels",
            int(localized_width_nudge_accepted_pixels),
        )
        _set_counter(
            performance_profile,
            "stage2_localized_width_nudge_accepted_components",
            int(localized_width_nudge_accepted_components),
        )
        _set_counter(
            performance_profile,
            "stage2_localized_width_nudge_rejected_pixels",
            int(localized_width_nudge_rejected_pixels),
        )
        _set_counter(
            performance_profile,
            "stage2_localized_width_nudge_rejected_components",
            int(localized_width_nudge_rejected_components),
        )
        _set_counter(
            performance_profile,
            "stage2_localized_width_nudge_edge_delta",
            int(localized_width_nudge_edge_delta),
        )
        _set_counter(
            performance_profile,
            "stage2_localized_width_nudge_pass_count",
            int(localized_width_nudge_pass_count),
        )
        _set_counter(
            performance_profile,
            "stage2_final_substrate_absorbed_pixels",
            int(final_substrate_absorbed_pixels),
        )
        _set_counter(
            performance_profile,
            "stage2_final_substrate_absorbed_components",
            int(final_substrate_absorbed_components),
        )
        _set_counter(
            performance_profile,
            "stage2_final_substrate_unresolved_components",
            int(final_substrate_unresolved_components),
        )
        _set_counter(
            performance_profile,
            "stage2_boundary_mutation_enabled",
            bool(boundary_mutation_enabled),
        )
        _set_counter(
            performance_profile,
            "stage2_boundary_mutation_segment_mode",
            bool(boundary_mutation_segment_mode),
        )
        _set_counter(
            performance_profile,
            "stage2_boundary_mutation_edge_run_mode",
            bool(boundary_mutation_edge_run_mode),
        )
        _set_counter(
            performance_profile,
            "stage2_boundary_mutation_max_passes",
            int(boundary_mutation_max_passes),
        )
        _set_counter(
            performance_profile,
            "stage2_boundary_mutation_passes_run",
            int(boundary_mutation_passes_run),
        )
        _set_counter(
            performance_profile,
            "stage2_boundary_mutation_pass_accepted_pixels",
            [int(value) for value in boundary_mutation_pass_accepted_pixels],
        )
        _set_counter(
            performance_profile,
            "stage2_boundary_mutation_current_de_percentile",
            (
                -1.0
                if boundary_mutation_current_de_percentile is None
                else float(boundary_mutation_current_de_percentile)
            ),
        )
        _set_counter(
            performance_profile,
            "stage2_boundary_mutation_current_de_threshold",
            float(boundary_mutation_current_de_threshold),
        )
        _set_counter(
            performance_profile,
            "stage2_boundary_mutation_current_de_eligible_pixels",
            int(boundary_mutation_current_de_eligible_pixels),
        )
        _set_counter(
            performance_profile,
            "stage2_boundary_mutation_candidate_pixels",
            int(boundary_mutation_candidate_pixels),
        )
        _set_counter(
            performance_profile,
            "stage2_boundary_mutation_accepted_pixels",
            int(boundary_mutation_accepted_pixels),
        )
        _set_counter(
            performance_profile,
            "stage2_boundary_mutation_accepted_components",
            int(boundary_mutation_accepted_components),
        )
        _set_counter(
            performance_profile,
            "stage2_boundary_mutation_min_component_pixels",
            int(boundary_mutation_min_component_pixels),
        )
        _set_counter(
            performance_profile,
            "stage2_boundary_mutation_rejected_small_pixels",
            int(boundary_mutation_rejected_small_pixels),
        )
        _set_counter(
            performance_profile,
            "stage2_boundary_mutation_rejected_small_components",
            int(boundary_mutation_rejected_small_components),
        )
        _set_counter(
            performance_profile,
            "stage2_boundary_mutation_rejected_weak_pixels",
            int(boundary_mutation_rejected_weak_pixels),
        )
        _set_counter(
            performance_profile,
            "stage2_boundary_mutation_rejected_weak_components",
            int(boundary_mutation_rejected_weak_components),
        )
        _set_counter(
            performance_profile,
            "stage2_boundary_mutation_accepted_boundary_contact_pixels",
            int(
                0
                if boundary_mutation_map is None
                else getattr(boundary_mutation, "accepted_boundary_contact_pixels", 0)
            ),
        )
        _set_counter(
            performance_profile,
            "stage2_boundary_mutation_rejected_short_run_pixels",
            int(
                0
                if boundary_mutation_map is None
                else getattr(boundary_mutation, "rejected_short_run_pixels", 0)
            ),
        )
        _set_counter(
            performance_profile,
            "stage2_boundary_mutation_rejected_short_run_components",
            int(
                0
                if boundary_mutation_map is None
                else getattr(boundary_mutation, "rejected_short_run_components", 0)
            ),
        )
        _set_counter(
            performance_profile,
            "stage2_boundary_mutation_mean_gain",
            float(boundary_mutation_mean_gain),
        )
        _set_counter(
            performance_profile,
            "stage2_boundary_mutation_p95_gain",
            float(boundary_mutation_p95_gain),
        )
        _set_counter(
            performance_profile,
            "stage2_detail_interior_override_pixels",
            int(interior_override_pixels),
        )
        _set_counter(
            performance_profile,
            "stage2_detail_interior_override_zones",
            int(interior_override_zones),
        )

    step_start = time.perf_counter()
    recipe_totals = np.array(
        [recipe.total_color_thickness_mm for recipe in recipes],
        dtype=np.float32,
    )
    recipe_label_map = fine_recipe_label_map
    raw_color_ceiling = (
        np.float32(cfg.d_wb) + recipe_totals[recipe_label_map]
    ).astype(np.float32, copy=False)
    base_top = np.full_like(raw_color_ceiling, np.float32(cfg.d_wb), dtype=np.float32)
    if performance_profile is not None:
        _record_timing(
            performance_profile,
            "stage2_surface_materialization_s",
            time.perf_counter() - step_start,
        )
        _record_timing(
            performance_profile,
            "stage2_total_s",
            time.perf_counter() - stage2_start,
        )
        _set_counter(
            performance_profile,
            "stage2_zone_count",
            int(evaluation_zone_count),
        )
        _set_counter(
            performance_profile,
            "stage2_adjacency_edge_count",
            int(len(evaluation_adjacency_edges)),
        )
        _set_counter(
            performance_profile,
            "stage2_solve_pixel_count",
            int(evaluation_zone_label_map.size),
        )

    record_stage2_diagnostics(
        diagnostics,
        source_edge_subzones=source_edge_subzones,
        subzone_refined_zone_count=subzone_refined_zone_count,
        subzone_refined_pixels=subzone_refined_pixels,
        gamut_mask=gamut_mask,
        de_flat=de_flat,
        candidate_sets=candidate_sets,
        augmented_zone_hits=augmented_zone_hits,
        augmented_candidate_count=augmented_candidate_count,
        frontier_optical_rescue_zone_hits=frontier_optical_rescue_zone_hits,
        frontier_optical_rescue_candidate_count=(
            frontier_optical_rescue_candidate_count
        ),
        frontier_pressure_rescue_candidate_count=(
            frontier_pressure_rescue_candidate_count
        ),
        frontier_neighbor_match_zone_hits=frontier_neighbor_match_zone_hits,
        optimization=optimization,
        objective_summary=objective_summary,
        detail_override_pixels=detail_override_pixels,
        detail_override_zones=detail_override_zones,
        interior_override_pixels=interior_override_pixels,
        interior_override_zones=interior_override_zones,
        boundary_mutation_enabled=boundary_mutation_enabled,
        boundary_mutation_accepted_pixels=boundary_mutation_accepted_pixels,
        boundary_mutation_candidate_pixels=boundary_mutation_candidate_pixels,
        boundary_mutation_mean_gain=boundary_mutation_mean_gain,
    )

    return VisibleRecipeRawGeometryPlan(
        evaluation_shape=evaluation_shape,
        evaluation_pitch_mm=float(cfg.solver_fine_pitch_mm),
        zone_label_map=evaluation_zone_label_map.astype(np.int32, copy=True),
        zone_recipe_labels=zone_recipe_labels,
        fine_recipe_label_map=fine_recipe_label_map.astype(np.int32, copy=True),
        recipe_table=recipes,
        base_top_mm=base_top,
        raw_color_ceiling_mm=raw_color_ceiling,
        implied_cap_height_mm=implied_cap_height.astype(np.float32, copy=True),
        gamut_mask=gamut_mask.reshape(evaluation_shape),
        mapped_target_oklab=targets.astype(np.float32, copy=True),
        stage2_objective_summary=objective_summary,
        recipe_stack_ids=recipe_stack_ids.astype(np.int32, copy=True),
        stage2_cap_values_mm=np.asarray(cap_values, dtype=np.float32).copy(),
        # Stage 4 reads the dense view: every grid cell finite, budget masking
        # is scoring-only (all_oklabs) so cap lookups never fall back to the
        # slow row-shaped predictor.
        stage2_stack_cap_oklab=np.asarray(dense_cap_oklabs, dtype=np.float32).copy(),
        stage2_recipe_pressure=pressure_diagnostic,
        stage2_fine_override_printability_rejection_map=(
            None
            if printability_gate_rejection_map is None
            else printability_gate_rejection_map.astype(np.uint8, copy=True)
        ),
        stage2_final_substrate_repair_map=(
            None
            if final_substrate_repair_map is None
            else final_substrate_repair_map.astype(np.uint8, copy=True)
        ),
        stage2_fine_override_printability_repair_map=(
            None
            if printability_gate_repair_map is None
            else printability_gate_repair_map.astype(np.uint8, copy=True)
        ),
        stage2_boundary_mutation_map=(
            None
            if boundary_mutation_map is None
            else boundary_mutation_map.astype(np.uint8, copy=True)
        ),
        stage2_exterior_white_guard_map=(
            None
            if exterior_white_guard_map is None
            else exterior_white_guard_map.astype(np.uint8, copy=True)
        ),
        mandatory_lateral_boundary_shield_floor_mm=(
            None
            if mandatory_lateral_shield_floor_mm is None
            else mandatory_lateral_shield_floor_mm.astype(np.float32, copy=True)
        ),
        mandatory_lateral_boundary_shield_floor_layer_pixels=int(
            np.sum(mandatory_lateral_shield_floor_layers)
        ),
        mandatory_lateral_boundary_shield_floor_active_pixels=int(
            np.count_nonzero(mandatory_lateral_shield_floor_layers > 0)
        ),
        mandatory_lateral_boundary_shield_floor_max_layers=int(
            np.max(mandatory_lateral_shield_floor_layers, initial=0)
        ),
        stage2_exterior_white_guard_pixels=int(exterior_white_guard_pixels),
        stage2_exterior_white_guard_changed_pixels=int(
            exterior_white_guard_changed_pixels
        ),
    )

__all__ = (
    '_materialize_recipe_assignments',
    'build_visible_plan',
)
