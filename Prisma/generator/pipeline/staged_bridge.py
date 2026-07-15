"""Read-only Stage 5 compatibility bridge for the experimental backend."""
from __future__ import annotations

from typing import Dict

import numpy as np

from .staged_artifacts import (
    CapSynthesisPlan,
    CompatibilityBundle,
    FillerGeometryPlan,
    VisibleRecipeRawGeometryPlan,
)
from .solved_material_plan import SolvedMaterialPlan, StackDefinition
from white_cap_contract import (
    PHYSICAL_GEOMETRY_METADATA_KEY,
    POLICY_LUMINANCE_DETAIL_CANONICAL,
    POLICY_STANDARD_APPEARANCE_BOUNDED_STRUCTURAL_SPLIT,
    POLICY_STANDARD_SMOOTH_VARIABLE_CANONICAL,
    WHITE_CAP_FIELD_TARGET_METADATA_KEY,
    WHITE_CAP_FIELD_TARGET_SCHEMA,
    WHITE_CAP_FIELD_TARGET_UPPER_SURFACE_KEY,
    quantized_cover_floor_mm,
)


def build_solved_material_plan_for_export(
    *,
    solver_fine_pitch_mm: float,
    color_region_target_mm: float,
    visible_plan: VisibleRecipeRawGeometryPlan,
    cap_plan: CapSynthesisPlan,
) -> SolvedMaterialPlan:
    """Project final experimental recipes into the solved-plan export contract.

    The experimental backend's authoritative color blueprint is already the
    final fine recipe-label map plus recipe table.  The legacy Stage 5 bridge
    still emits raster thickness maps for preview compatibility, but export can
    use this plan to recover connected same-stack color regions and avoid the
    per-pixel raster mesh path.
    """
    recipe_label_map = np.asarray(visible_plan.recipe_label_map, dtype=np.int32)
    h, w = recipe_label_map.shape
    if recipe_label_map.ndim != 2:
        raise ValueError(
            f"recipe_label_map must be 2-D, got shape {recipe_label_map.shape}"
        )
    if h <= 0 or w <= 0:
        raise ValueError("recipe_label_map must be non-empty")
    if np.any(recipe_label_map < 0):
        raise ValueError("recipe_label_map contains negative recipe labels")

    used_recipe_labels = np.unique(recipe_label_map)
    stack_table: list[StackDefinition] = []
    stack_index_by_definition: dict[StackDefinition, int] = {}
    segment_stack_id = np.empty(used_recipe_labels.shape[0], dtype=np.int32)
    segment_id_map = np.empty(recipe_label_map.shape, dtype=np.int32)

    for segment_id, recipe_label in enumerate(used_recipe_labels.tolist()):
        recipe_idx = int(recipe_label)
        if recipe_idx >= len(visible_plan.recipe_table):
            raise ValueError(
                "recipe_label_map references recipe "
                f"{recipe_idx}, but recipe_table only has "
                f"{len(visible_plan.recipe_table)} entries"
            )
        stack = StackDefinition.from_mapping(
            visible_plan.recipe_table[recipe_idx].to_mapping()
        )
        stack_idx = stack_index_by_definition.get(stack)
        if stack_idx is None:
            stack_idx = len(stack_table)
            stack_index_by_definition[stack] = stack_idx
            stack_table.append(stack)
        segment_id_map[recipe_label_map == recipe_idx] = np.int32(segment_id)
        segment_stack_id[segment_id] = np.int32(stack_idx)

    cap_height_map = np.asarray(cap_plan.cap_height_mm, dtype=np.float32)
    if cap_height_map.shape != recipe_label_map.shape:
        raise ValueError(
            "cap_height_mm shape "
            f"{cap_height_map.shape} does not match recipe_label_map shape "
            f"{recipe_label_map.shape}"
        )

    pitch = float(solver_fine_pitch_mm)
    return SolvedMaterialPlan(
        image_domain_width_mm=float(w) * pitch,
        image_domain_height_mm=float(h) * pitch,
        image_sample_pitch_mm=pitch,
        solver_fine_pitch_mm=pitch,
        color_region_target_mm=float(color_region_target_mm),
        segment_id_map=segment_id_map,
        segment_stack_id=segment_stack_id,
        stack_table=tuple(stack_table),
        cap_height_map=cap_height_map.copy(),
    )


def build_compatibility_bundle(
    *,
    palette: list[str],
    solver_fine_pitch_mm: float,
    layer_height_mm: float,
    d_wb_mm: float,
    d_wc_min_mm: float,
    t_max_mm: float,
    effective_d_wc_max_mm: float,
    effective_boundary_d_wc_max_mm: float,
    luminance_mode: str,
    cap_mode: str,
    visible_plan: VisibleRecipeRawGeometryPlan,
    filler_plan: FillerGeometryPlan,
    cap_plan: CapSynthesisPlan,
) -> CompatibilityBundle:
    """Project staged artifacts into legacy-compatible read-only maps."""
    recipe_label_map = visible_plan.recipe_label_map.astype(np.int32, copy=False)
    zone_label_map = visible_plan.zone_label_map.astype(np.int32, copy=False)
    h, w = recipe_label_map.shape

    thickness_maps: Dict[str, np.ndarray] = {
        fid: np.zeros((h, w), dtype=np.float32) for fid in palette
    }
    for recipe_label, recipe in enumerate(visible_plan.recipe_table):
        mask = recipe_label_map == recipe_label
        if not np.any(mask):
            continue
        for fid, thickness in recipe.thickness_by_filament:
            if fid in thickness_maps:
                thickness_maps[fid][mask] = np.float32(thickness)

    boundary_cap_height = (
        np.asarray(cap_plan.cap_boundary_top_mm, dtype=np.float32)
        - np.asarray(filler_plan.color_ceiling_mm, dtype=np.float32)
    )
    boundary_cap_height = np.maximum(boundary_cap_height, np.float32(0.0)).astype(
        np.float32,
        copy=False,
    )
    thickness_maps["__white_cap__"] = cap_plan.cap_height_mm.astype(np.float32, copy=True)
    thickness_maps["__white_boundary_cap__"] = boundary_cap_height.astype(
        np.float32,
        copy=True,
    )
    thickness_maps["__white_detail_cap__"] = cap_plan.detail_height_mm.astype(
        np.float32,
        copy=True,
    )

    diagnostics: Dict[str, np.ndarray] = {
        "__gamut_mask__": visible_plan.gamut_mask.astype(np.float32, copy=True),
    }
    if str(luminance_mode or "standard") == "luminance_detail":
        policy = POLICY_LUMINANCE_DETAIL_CANONICAL
        solve_mode = "luminance"
    elif str(cap_mode or "smooth_variable") == "appearance_bounded_smooth":
        policy = POLICY_STANDARD_APPEARANCE_BOUNDED_STRUCTURAL_SPLIT
        solve_mode = "standard"
    else:
        policy = POLICY_STANDARD_SMOOTH_VARIABLE_CANONICAL
        solve_mode = "standard"
    required_cover_floor_mm = quantized_cover_floor_mm(d_wc_min_mm, layer_height_mm)
    export_maps: Dict[str, np.ndarray] = {
        WHITE_CAP_FIELD_TARGET_UPPER_SURFACE_KEY: cap_plan.final_visible_top_mm.astype(
            np.float32,
            copy=True,
        ),
    }
    export_metadata: dict[str, object] = {
        PHYSICAL_GEOMETRY_METADATA_KEY: {
            "pitch_mm": float(solver_fine_pitch_mm),
            "solver_fine_pitch_mm": float(solver_fine_pitch_mm),
            "layer_height_mm": float(layer_height_mm),
            "d_wb_mm": float(d_wb_mm),
            "d_wc_min_mm": float(d_wc_min_mm),
            "t_max_mm": float(t_max_mm),
            "luminance_mode": str(luminance_mode or "standard"),
            "cap_mode": str(cap_mode or "smooth_variable"),
        },
        WHITE_CAP_FIELD_TARGET_METADATA_KEY: {
            "schema": WHITE_CAP_FIELD_TARGET_SCHEMA,
            "field_key": WHITE_CAP_FIELD_TARGET_UPPER_SURFACE_KEY,
            "policy": policy,
            "solve_mode": solve_mode,
            "luminance_mode": str(luminance_mode or "standard"),
            "cap_mode": str(cap_mode or "smooth_variable"),
            "detail_smoothing_applied": bool(cap_plan.detail_cap_smoothing_summary),
            "effective_d_wc_max_mm": float(effective_d_wc_max_mm),
            "effective_boundary_d_wc_max_mm": float(effective_boundary_d_wc_max_mm),
            "required_cover_floor_mm": float(required_cover_floor_mm),
        },
    }
    debug_maps = {
        "zone_labels": zone_label_map.astype(np.float32, copy=True),
        "recipe_labels": recipe_label_map.astype(np.float32, copy=True),
        "raw_color_ceiling": visible_plan.raw_color_ceiling_mm.astype(
            np.float32,
            copy=True,
        ),
        "color_ceiling": filler_plan.color_ceiling_mm.astype(
            np.float32,
            copy=True,
        ),
        "cap_boundary_top": cap_plan.cap_boundary_top_mm.astype(
            np.float32,
            copy=True,
        ),
        "boundary_cap_height": boundary_cap_height.astype(
            np.float32,
            copy=True,
        ),
        "boundary_edge_guard": cap_plan.boundary_edge_guard_weight.astype(
            np.float32,
            copy=True,
        ),
        "detail_height": cap_plan.detail_height_mm.astype(
            np.float32,
            copy=True,
        ),
        "detail_candidate_zone_labels": cap_plan.detail_candidate_zone_label_map.astype(
            np.float32,
            copy=True,
        ),
        "detail_zone_labels": cap_plan.detail_zone_label_map.astype(
            np.float32,
            copy=True,
        ),
        "detail_rejection_reasons": cap_plan.detail_zone_rejection_reason_map.astype(
            np.float32,
            copy=True,
        ),
        "final_visible_top": cap_plan.final_visible_top_mm.astype(
            np.float32,
            copy=True,
        ),
    }
    pressure = visible_plan.stage2_recipe_pressure
    if pressure is not None:
        debug_maps.update(
            {
                "stage2_coarse_excess": pressure.coarse_excess.astype(
                    np.float32,
                    copy=True,
                ),
                "stage2_pruning_gap": pressure.pruning_gap.astype(
                    np.float32,
                    copy=True,
                ),
                "stage2_local_gap": pressure.local_gap.astype(
                    np.float32,
                    copy=True,
                ),
                "stage2_total_excess": pressure.total_excess.astype(
                    np.float32,
                    copy=True,
                ),
                "stage2_frontier_best_labels": pressure.frontier_best_stack_id.astype(
                    np.float32,
                    copy=True,
                ),
                "stage2_local_best_labels": pressure.local_best_stack_id.astype(
                    np.float32,
                    copy=True,
                ),
                "stage2_blockiness_heatmap": pressure.blockiness_heatmap.astype(
                    np.float32,
                    copy=True,
                ),
            }
        )
    attribution = cap_plan.stage2_geometry_pressure_attribution
    if attribution is not None:
        debug_maps.update(
            {
                "stage2_geometry_component_labels": attribution.component_label_map.astype(
                    np.float32,
                    copy=True,
                ),
                "stage2_geometry_prefine_classes": attribution.prefine_class_label_map.astype(
                    np.float32,
                    copy=True,
                ),
                "stage2_geometry_postfine_classes": attribution.postfine_class_label_map.astype(
                    np.float32,
                    copy=True,
                ),
                "stage2_final_blockiness_heatmap": attribution.final_blockiness_heatmap.astype(
                    np.float32,
                    copy=True,
                ),
                "stage2_cap_raw_deviation": attribution.cap_raw_deviation_map.astype(
                    np.float32,
                    copy=True,
                ),
                "stage2_cap_transition_width": attribution.cap_transition_width_map.astype(
                    np.float32,
                    copy=True,
                ),
                "stage2_source_edge_support": attribution.source_edge_support_map.astype(
                    np.float32,
                    copy=True,
                ),
            }
        )
    printability_gate = visible_plan.stage2_fine_override_printability_rejection_map
    if printability_gate is not None:
        debug_maps.update(
            {
                "stage2_printability_gate_rejections": printability_gate.astype(
                    np.float32,
                    copy=True,
                )
            }
        )
    final_substrate_repair = visible_plan.stage2_final_substrate_repair_map
    if final_substrate_repair is not None:
        debug_maps.update(
            {
                "stage2_final_substrate_repair_absorptions": final_substrate_repair.astype(
                    np.float32,
                    copy=True,
                )
            }
        )
    printability_repair = visible_plan.stage2_fine_override_printability_repair_map
    if printability_repair is not None:
        debug_maps.update(
            {
                "stage2_printability_repair": printability_repair.astype(
                    np.float32,
                    copy=True,
                )
            }
        )
    boundary_mutation = visible_plan.stage2_boundary_mutation_map
    if boundary_mutation is not None:
        debug_maps.update(
            {
                "stage2_boundary_mutation": boundary_mutation.astype(
                    np.float32,
                    copy=True,
                )
            }
        )
    exterior_guard = visible_plan.stage2_exterior_white_guard_map
    if exterior_guard is not None:
        debug_maps.update(
            {
                "stage2_exterior_white_guard": exterior_guard.astype(
                    np.float32,
                    copy=True,
                )
            }
        )
    detail_authoring_printability = cap_plan.detail_authoring_printability_rejection_map
    detail_printability = cap_plan.detail_printability_suppression_map
    boundary_cap_printability = cap_plan.boundary_cap_printability_repair_map
    if boundary_cap_printability is not None:
        debug_maps.update(
            {
                "stage4_boundary_cap_printability_repairs": (
                    boundary_cap_printability.astype(np.float32, copy=True)
                )
            }
        )
    if detail_printability is not None:
        debug_maps.update(
            {
                "stage4_detail_printability_suppression": detail_printability.astype(
                    np.float32,
                    copy=True,
                )
            }
        )
    if detail_authoring_printability is not None:
        debug_maps.update(
            {
                "stage4_detail_authoring_printability_prevention": (
                    detail_authoring_printability.astype(np.float32, copy=True)
                )
            }
        )
    printability = cap_plan.blueprint_printability_diagnostic
    if printability is not None:
        debug_maps.update(
            {
                "blueprint_printability_hard_fail": printability.hard_fail_map.astype(
                    np.float32,
                    copy=True,
                ),
                "blueprint_printability_narrow_width": printability.narrow_width_map.astype(
                    np.float32,
                    copy=True,
                ),
                "blueprint_printability_short_component": printability.short_component_map.astype(
                    np.float32,
                    copy=True,
                ),
                "blueprint_printability_color_hard_fail": printability.color_hard_fail_map.astype(
                    np.float32,
                    copy=True,
                ),
                "blueprint_printability_cap_hard_fail": printability.cap_hard_fail_map.astype(
                    np.float32,
                    copy=True,
                ),
                "blueprint_printability_boundary_cap_hard_fail": printability.cap_hard_fail_map.astype(
                    np.float32,
                    copy=True,
                ),
                "blueprint_printability_detail_hard_fail": printability.detail_hard_fail_map.astype(
                    np.float32,
                    copy=True,
                ),
                "blueprint_printability_center_clearance": printability.center_clearance_map.astype(
                    np.float32,
                    copy=True,
                ),
                "blueprint_printability_width_loss": printability.width_loss_map.astype(
                    np.float32,
                    copy=True,
                ),
                "blueprint_printability_low_support": printability.low_support_map.astype(
                    np.float32,
                    copy=True,
                ),
            }
        )
    return CompatibilityBundle(
        thickness_maps=thickness_maps,
        diagnostics=diagnostics,
        debug_maps=debug_maps,
        export_maps=export_maps,
        export_metadata=export_metadata,
        compatibility_label_map=recipe_label_map.astype(np.int32, copy=True),
        boundary_label_map=zone_label_map.astype(np.int32, copy=True),
        image_domain_width_mm=float(w) * float(solver_fine_pitch_mm),
        image_domain_height_mm=float(h) * float(solver_fine_pitch_mm),
    )


__all__ = [
    "build_compatibility_bundle",
    "build_solved_material_plan_for_export",
]
