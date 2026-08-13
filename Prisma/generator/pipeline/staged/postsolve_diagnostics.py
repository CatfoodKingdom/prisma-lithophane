"""Optional diagnostics that consume multiple staged artifacts."""
from __future__ import annotations

import time

import numpy as np
from scipy.ndimage import (
    distance_transform_edt,
    generate_binary_structure,
    label as nd_label,
)

from model import (
    compose_stack,
    predict_transmission,
    to_oklab,
)

from ..staged_artifacts import (
    CapSynthesisPlan,
    FillerGeometryPlan,
    PlanningDiagnosticEntry,
    Stage2GeometryAttributionComponentFacts,
    Stage2GeometryPressureAttribution,
    Stage2RecipePressure,
    VisibleRecipeRawGeometryPlan,
)
from ..staged_printability import (
    build_layered_blueprint_view,
    resolve_blueprint_printability_settings,
    run_blueprint_printability_diagnostic,
)

from .cap_prediction import (
    _stage4_provider_enabled,
    _increment_diagnostic_counter,
    _stage4_provider_cap_oklab_lookup,
    _stage4_precomputed_cap_oklab_lookup,
)
from .cap_surface import (
    _quantize_cap_map,
    _stage4_gradient_magnitude,
    _stage4_detail_recipe_boundary_support,
)
from .coarse_grid import (
    _stage1_lattice_offset_px,
    _stage2_coarse_lattice_edge_masks,
    _stage2_coarse_lattice_pixel_mask,
)
from .image_analysis import _compute_target_edge_strength
from .recipe_pressure import (
    _STAGE2_PRESSURE_ACTIVE_THRESHOLD,
    _STAGE2_GEOMETRY_ATTR_SOURCE_EDGE_PERCENTILE,
)
from .telemetry import (
    _record_timing,
    _set_counter,
)

_STAGE2_GEOMETRY_ATTR_CAP_EDGE_PERCENTILE = 75.0

_STAGE2_GEOMETRY_ATTR_CAP_RAW_DEVIATION_LAYERS = 1.0

_STAGE2_GEOMETRY_ATTR_CAP_SIDE_PRESSURE_OVERLAP_MAX = 0.25

_STAGE2_GEOMETRY_ATTR_CAP_SIDE_RAW_DEVIATION_MIN = 0.50

_STAGE2_GEOMETRY_ATTR_WHOLE_ZONE_PRESSURE_FRACTION = 0.65

_STAGE2_GEOMETRY_ATTR_WHOLE_ZONE_MODAL_ALT_FRACTION = 0.70

_STAGE2_GEOMETRY_ATTR_SOURCE_EDGE_SUPPORT = 0.40

_STAGE2_GEOMETRY_ATTR_LATTICE_OVERLAP = 0.30

_STAGE2_GEOMETRY_ATTR_INTERIOR_MIN_PIXELS = 8

_STAGE2_GEOMETRY_ATTR_INTERIOR_MODAL_ALT_FRACTION = 0.60

_STAGE2_GEOMETRY_CLASS_NAMES = (
    "background",
    "cap_side_candidate",
    "whole_zone_choice",
    "source_edge_adaptive_stage1_candidate",
    "interior_subzone_candidate",
    "coarse_lattice_boundary_candidate",
    "stage2_zone_boundary_candidate",
    "cross_boundary_geometry_candidate",
    "ambiguous",
)

_STAGE2_GEOMETRY_CLASS_BACKGROUND = 0

_STAGE2_GEOMETRY_CLASS_CAP_SIDE = 1

_STAGE2_GEOMETRY_CLASS_WHOLE_ZONE = 2

_STAGE2_GEOMETRY_CLASS_SOURCE_EDGE = 3

_STAGE2_GEOMETRY_CLASS_INTERIOR_SUBZONE = 4

_STAGE2_GEOMETRY_CLASS_COARSE_LATTICE_BOUNDARY = 5

_STAGE2_GEOMETRY_CLASS_STAGE2_ZONE_BOUNDARY = 6

_STAGE2_GEOMETRY_CLASS_CROSS_BOUNDARY = 7

_STAGE2_GEOMETRY_CLASS_AMBIGUOUS = 8

def _predict_stage4_oklab_map(
    *,
    state,
    visible_plan: VisibleRecipeRawGeometryPlan,
    cap_height_mm: np.ndarray,
) -> np.ndarray:
    """Predict final OKLab from staged artifacts for read-only attribution."""
    shape = visible_plan.evaluation_shape
    recipe_label_map = np.asarray(visible_plan.recipe_label_map, dtype=np.int32)
    cap_map = np.asarray(cap_height_mm, dtype=np.float32).reshape(shape)
    out = np.zeros(shape + (3,), dtype=np.float32)
    if _stage4_provider_enabled(state):
        for recipe_label in np.unique(recipe_label_map).tolist():
            if int(recipe_label) < 0 or int(recipe_label) >= len(visible_plan.recipe_table):
                continue
            recipe_mask = recipe_label_map == int(recipe_label)
            if not np.any(recipe_mask):
                continue
            recipe = visible_plan.recipe_table[int(recipe_label)].to_mapping()
            cap_values = np.unique(cap_map[recipe_mask])
            if cap_values.size == 0:
                continue
            cap_oklab_lookup = _stage4_precomputed_cap_oklab_lookup(
                state=state,
                visible_plan=visible_plan,
                recipe_label=int(recipe_label),
                cap_values=cap_values,
            )
            if cap_oklab_lookup is None:
                _increment_diagnostic_counter(state, "__stage4_provider_final_oklab_fallbacks__")
                cap_oklab_lookup = _stage4_provider_cap_oklab_lookup(
                    state=state,
                    recipe=recipe,
                    cap_values=cap_values,
                )
            for cap_value in cap_values.tolist():
                pixel_mask = recipe_mask & (cap_map == np.float32(cap_value))
                if np.any(pixel_mask):
                    out[pixel_mask] = cap_oklab_lookup[float(cap_value)]
        return out.astype(np.float32, copy=False)

    wb_profile = state.profiles.wb_profile
    wc_profile = state.profiles.wc_profile
    color_profiles = state.profiles.color_profiles
    d_wb = float(state.config.d_wb)

    for recipe_label in np.unique(recipe_label_map).tolist():
        if int(recipe_label) < 0 or int(recipe_label) >= len(visible_plan.recipe_table):
            continue
        recipe_mask = recipe_label_map == int(recipe_label)
        if not np.any(recipe_mask):
            continue
        recipe = visible_plan.recipe_table[int(recipe_label)].to_mapping()
        layers = [(wb_profile, d_wb)]
        for fid, thickness in recipe.items():
            if float(thickness) > 1e-9:
                layers.append((color_profiles[fid], float(thickness)))
        base_t = compose_stack(layers).astype(np.float32)
        cap_values = np.unique(cap_map[recipe_mask])
        for cap_value in cap_values.tolist():
            cap_t = np.asarray(
                predict_transmission(wc_profile, float(cap_value)),
                dtype=np.float32,
            )
            cap_mask = recipe_mask & (cap_map == np.float32(cap_value))
            out[cap_mask] = to_oklab((base_t * cap_t).reshape(1, 3))[0]
    return out.astype(np.float32, copy=False)

def _stage2_edge_excess_ratio_and_heatmap(
    *,
    predicted_oklab: np.ndarray,
    target_oklab: np.ndarray,
    coarse_to_fine_scale: int,
    lattice_offset_y_px: int = 0,
    lattice_offset_x_px: int = 0,
) -> tuple[float, np.ndarray]:
    """Return final-output excess-gradient blockiness on projected coarse-cell edges."""
    pred = np.asarray(predicted_oklab, dtype=np.float32)
    target = np.asarray(target_oklab, dtype=np.float32)
    h, w = pred.shape[:2]
    scale = int(coarse_to_fine_scale)
    heatmap = np.zeros((h, w), dtype=np.float32)
    if scale <= 1 or h == 0 or w == 0:
        return 0.0, heatmap

    source_edges: list[np.ndarray] = []
    pred_edges: list[np.ndarray] = []
    lattice_edges: list[np.ndarray] = []
    heat_targets: list[tuple[np.ndarray, tuple[slice, slice], tuple[slice, slice]]] = []
    y_lattice, x_lattice = _stage2_coarse_lattice_edge_masks(
        (h, w),
        scale,
        offset_y_px=int(lattice_offset_y_px),
        offset_x_px=int(lattice_offset_x_px),
    )

    if h > 1:
        source_y = np.sqrt(np.sum((target[1:, :, :] - target[:-1, :, :]) ** 2, axis=2))
        pred_y = np.sqrt(np.sum((pred[1:, :, :] - pred[:-1, :, :]) ** 2, axis=2))
        source_edges.append(source_y.astype(np.float32, copy=False))
        pred_edges.append(pred_y.astype(np.float32, copy=False))
        lattice_edges.append(y_lattice)
        heat_targets.append((y_lattice, (slice(1, None), slice(None)), (slice(None, -1), slice(None))))
    if w > 1:
        source_x = np.sqrt(np.sum((target[:, 1:, :] - target[:, :-1, :]) ** 2, axis=2))
        pred_x = np.sqrt(np.sum((pred[:, 1:, :] - pred[:, :-1, :]) ** 2, axis=2))
        source_edges.append(source_x.astype(np.float32, copy=False))
        pred_edges.append(pred_x.astype(np.float32, copy=False))
        lattice_edges.append(x_lattice)
        heat_targets.append((x_lattice, (slice(None), slice(1, None)), (slice(None), slice(None, -1))))

    if not source_edges:
        return 0.0, heatmap
    all_source = np.concatenate([edge.reshape(-1) for edge in source_edges])
    positive_source = all_source[all_source > 1e-9]
    source_threshold = (
        float(np.percentile(positive_source, _STAGE2_GEOMETRY_ATTR_SOURCE_EDGE_PERCENTILE))
        if positive_source.size
        else float("inf")
    )
    lattice_values: list[np.ndarray] = []
    control_values: list[np.ndarray] = []
    for source_edge, pred_edge, lattice_edge, (_, hi_slice, lo_slice) in zip(
        source_edges,
        pred_edges,
        lattice_edges,
        heat_targets,
        strict=False,
    ):
        eligible = source_edge < np.float32(source_threshold)
        excess = np.maximum(pred_edge - source_edge, np.float32(0.0)).astype(np.float32)
        lattice_mask = eligible & lattice_edge
        control_mask = eligible & ~lattice_edge
        if np.any(lattice_mask):
            lattice_values.append(excess[lattice_mask])
            contribution = np.where(lattice_mask, excess, np.float32(0.0))
            heatmap[hi_slice] = np.maximum(heatmap[hi_slice], contribution)
            heatmap[lo_slice] = np.maximum(heatmap[lo_slice], contribution)
        if np.any(control_mask):
            control_values.append(excess[control_mask])
    if not lattice_values or not control_values:
        return 0.0, heatmap
    lattice_mean = float(np.mean(np.concatenate(lattice_values)))
    control_mean = float(np.mean(np.concatenate(control_values)))
    return lattice_mean / max(control_mean, 1e-9), heatmap.astype(np.float32, copy=False)

def _stage2_label_boundary_pixel_mask(labels: np.ndarray) -> np.ndarray:
    """Return pixels adjacent to a label transition."""
    arr = np.asarray(labels, dtype=np.int32)
    boundary = np.zeros(arr.shape, dtype=bool)
    if arr.shape[0] > 1:
        dy = arr[:-1, :] != arr[1:, :]
        boundary[:-1, :] |= dy
        boundary[1:, :] |= dy
    if arr.shape[1] > 1:
        dx = arr[:, :-1] != arr[:, 1:]
        boundary[:, :-1] |= dx
        boundary[:, 1:] |= dx
    return boundary

def _stage2_internal_recipe_edge_density(
    *,
    recipe_labels: np.ndarray,
    zone_labels: np.ndarray,
    component_mask: np.ndarray,
) -> float:
    """Return internal recipe-edge count divided by component pixels."""
    recipes = np.asarray(recipe_labels, dtype=np.int32)
    zones = np.asarray(zone_labels, dtype=np.int32)
    component = np.asarray(component_mask, dtype=bool)
    edge_count = 0
    if recipes.shape[0] > 1:
        dy = recipes[:-1, :] != recipes[1:, :]
        same_zone = zones[:-1, :] == zones[1:, :]
        touches = component[:-1, :] | component[1:, :]
        edge_count += int(np.count_nonzero(dy & same_zone & touches))
    if recipes.shape[1] > 1:
        dx = recipes[:, :-1] != recipes[:, 1:]
        same_zone = zones[:, :-1] == zones[:, 1:]
        touches = component[:, :-1] | component[:, 1:]
        edge_count += int(np.count_nonzero(dx & same_zone & touches))
    return edge_count / float(max(1, int(np.count_nonzero(component))))

def _stage2_geometry_class_name(class_label: int) -> str:
    idx = int(class_label)
    if 0 <= idx < len(_STAGE2_GEOMETRY_CLASS_NAMES):
        return str(_STAGE2_GEOMETRY_CLASS_NAMES[idx])
    return "ambiguous"

def _stage2_geometry_attribution_for_mode(
    *,
    mode: str,
    component_label_map: np.ndarray,
    component_count: int,
    zone_label_map: np.ndarray,
    recipe_label_map: np.ndarray,
    pressure: Stage2RecipePressure,
    active_pressure: np.ndarray,
    active_blockiness: np.ndarray,
    active_cap_deviation: np.ndarray,
    source_edge_support: np.ndarray,
    cap_transition_width_map: np.ndarray,
    coarse_lattice_mask: np.ndarray,
    zone_boundary_mask: np.ndarray,
    stage4_detail_mask: np.ndarray,
    recipe_boundary_detail_mask: np.ndarray,
) -> tuple[np.ndarray, tuple[Stage2GeometryAttributionComponentFacts, ...]]:
    """Classify Phase C components for either prefine or postfine recipe geometry."""
    class_map = np.zeros(component_label_map.shape, dtype=np.int32)
    facts: list[Stage2GeometryAttributionComponentFacts] = []
    flat_zones = np.asarray(zone_label_map, dtype=np.int32).reshape(-1)
    flat_frontier_alt = np.asarray(pressure.frontier_best_stack_id, dtype=np.int32).reshape(-1)
    flat_preprune_alt = np.asarray(pressure.preprune_best_stack_id, dtype=np.int32).reshape(-1)
    total_pressure = np.asarray(pressure.total_excess, dtype=np.float32)
    coarse_excess = np.asarray(pressure.coarse_excess, dtype=np.float32)
    pruning_gap = np.asarray(pressure.pruning_gap, dtype=np.float32)
    local_gap = np.asarray(pressure.local_gap, dtype=np.float32)

    for component_id in range(1, int(component_count) + 1):
        component_mask = component_label_map == component_id
        pixel_count = int(np.count_nonzero(component_mask))
        if pixel_count <= 0:
            continue
        ys, xs = np.nonzero(component_mask)
        component_indices = np.flatnonzero(component_mask.reshape(-1)).astype(np.int64, copy=False)
        zone_ids, zone_counts = np.unique(flat_zones[component_indices], return_counts=True)
        dominant_zone_index = int(np.argmax(zone_counts)) if zone_counts.size else 0
        dominant_zone_id = int(zone_ids[dominant_zone_index]) if zone_ids.size else -1
        zone_mask = zone_label_map == dominant_zone_id if dominant_zone_id >= 0 else component_mask
        pressure_in_zone = active_pressure & zone_mask
        zone_pressure_fraction = (
            float(np.count_nonzero(pressure_in_zone)) / float(max(1, int(np.count_nonzero(zone_mask))))
        )
        pressure_fraction = float(np.count_nonzero(active_pressure & component_mask)) / float(pixel_count)
        coarse_fraction = float(np.count_nonzero((coarse_excess > _STAGE2_PRESSURE_ACTIVE_THRESHOLD) & component_mask)) / float(pixel_count)
        pruning_fraction = float(np.count_nonzero((pruning_gap > _STAGE2_PRESSURE_ACTIVE_THRESHOLD) & component_mask)) / float(pixel_count)
        local_fraction = float(np.count_nonzero((local_gap > _STAGE2_PRESSURE_ACTIVE_THRESHOLD) & component_mask)) / float(pixel_count)
        block_fraction = float(np.count_nonzero(active_blockiness & component_mask)) / float(pixel_count)
        source_fraction = float(np.count_nonzero(source_edge_support & component_mask)) / float(pixel_count)
        cap_fraction = float(np.count_nonzero(active_cap_deviation & component_mask)) / float(pixel_count)
        lattice_fraction = float(np.count_nonzero(coarse_lattice_mask & component_mask)) / float(pixel_count)
        boundary_fraction = float(np.count_nonzero(zone_boundary_mask & component_mask)) / float(pixel_count)
        cap_width_values = cap_transition_width_map[component_mask & active_cap_deviation]
        cap_width_mean = float(np.mean(cap_width_values)) if cap_width_values.size else 0.0
        alt_ids = np.where(
            pruning_gap.reshape(-1)[component_indices] > _STAGE2_PRESSURE_ACTIVE_THRESHOLD,
            flat_preprune_alt[component_indices],
            flat_frontier_alt[component_indices],
        )
        alt_ids = alt_ids[alt_ids >= 0]
        if alt_ids.size:
            unique_alts, alt_counts = np.unique(alt_ids, return_counts=True)
            alt_index = int(np.argmax(alt_counts))
            modal_alt_stack_id = int(unique_alts[alt_index])
            modal_alt_fraction = float(alt_counts[alt_index]) / float(max(1, alt_ids.size))
        else:
            modal_alt_stack_id = -1
            modal_alt_fraction = 0.0
        internal_density = _stage2_internal_recipe_edge_density(
            recipe_labels=recipe_label_map,
            zone_labels=zone_label_map,
            component_mask=component_mask,
        )
        detail_fraction = float(np.count_nonzero(stage4_detail_mask & component_mask)) / float(pixel_count)
        recipe_detail_fraction = (
            float(np.count_nonzero(recipe_boundary_detail_mask & component_mask)) / float(pixel_count)
        )

        if (
            block_fraction > 0.0
            and pressure_fraction < _STAGE2_GEOMETRY_ATTR_CAP_SIDE_PRESSURE_OVERLAP_MAX
            and cap_fraction >= _STAGE2_GEOMETRY_ATTR_CAP_SIDE_RAW_DEVIATION_MIN
        ):
            class_label = _STAGE2_GEOMETRY_CLASS_CAP_SIDE
        elif (
            zone_pressure_fraction >= _STAGE2_GEOMETRY_ATTR_WHOLE_ZONE_PRESSURE_FRACTION
            and modal_alt_fraction >= _STAGE2_GEOMETRY_ATTR_WHOLE_ZONE_MODAL_ALT_FRACTION
        ):
            class_label = _STAGE2_GEOMETRY_CLASS_WHOLE_ZONE
        elif (
            source_fraction >= _STAGE2_GEOMETRY_ATTR_SOURCE_EDGE_SUPPORT
            and lattice_fraction >= _STAGE2_GEOMETRY_ATTR_LATTICE_OVERLAP
        ):
            class_label = _STAGE2_GEOMETRY_CLASS_SOURCE_EDGE
        elif (
            pixel_count >= _STAGE2_GEOMETRY_ATTR_INTERIOR_MIN_PIXELS
            and boundary_fraction <= 0.25
            and lattice_fraction <= 0.35
            and modal_alt_fraction >= _STAGE2_GEOMETRY_ATTR_INTERIOR_MODAL_ALT_FRACTION
            and source_fraction < _STAGE2_GEOMETRY_ATTR_SOURCE_EDGE_SUPPORT
        ):
            class_label = _STAGE2_GEOMETRY_CLASS_INTERIOR_SUBZONE
        elif boundary_fraction > 0.25 and lattice_fraction > 0.35:
            class_label = _STAGE2_GEOMETRY_CLASS_CROSS_BOUNDARY
        elif lattice_fraction > 0.35:
            class_label = _STAGE2_GEOMETRY_CLASS_COARSE_LATTICE_BOUNDARY
        elif boundary_fraction > 0.25:
            class_label = _STAGE2_GEOMETRY_CLASS_STAGE2_ZONE_BOUNDARY
        else:
            class_label = _STAGE2_GEOMETRY_CLASS_AMBIGUOUS

        class_map[component_mask] = int(class_label)
        facts.append(
            Stage2GeometryAttributionComponentFacts(
                component_id=int(component_id),
                mode=str(mode),
                class_label=int(class_label),
                class_name=_stage2_geometry_class_name(class_label),
                pixel_count=pixel_count,
                y_min=int(np.min(ys)),
                x_min=int(np.min(xs)),
                y_max=int(np.max(ys)),
                x_max=int(np.max(xs)),
                dominant_zone_id=dominant_zone_id,
                zone_pressure_fraction=float(zone_pressure_fraction),
                pressure_fraction=float(pressure_fraction),
                coarse_excess_fraction=float(coarse_fraction),
                pruning_gap_fraction=float(pruning_fraction),
                local_gap_fraction=float(local_fraction),
                final_blockiness_fraction=float(block_fraction),
                source_edge_support_fraction=float(source_fraction),
                cap_raw_deviation_fraction=float(cap_fraction),
                cap_transition_width_mean_px=float(cap_width_mean),
                coarse_lattice_overlap_fraction=float(lattice_fraction),
                zone_boundary_overlap_fraction=float(boundary_fraction),
                modal_alt_stack_id=int(modal_alt_stack_id),
                modal_alt_fraction=float(modal_alt_fraction),
                internal_recipe_edge_density=float(internal_density),
                stage4_detail_fraction=float(detail_fraction),
                recipe_boundary_detail_fraction=float(recipe_detail_fraction),
            )
        )
    return class_map.astype(np.int32, copy=False), tuple(facts)

def _compute_stage2_geometry_pressure_attribution(
    *,
    state,
    visible_plan: VisibleRecipeRawGeometryPlan,
    filler_plan: FillerGeometryPlan,
    cap_plan: CapSynthesisPlan,
) -> Stage2GeometryPressureAttribution | None:
    """Build Phase C read-only attribution over pressure, final output, and cap maps."""
    pressure = visible_plan.stage2_recipe_pressure
    if pressure is None:
        return None
    shape = visible_plan.evaluation_shape
    scale = int(state.config.stage1_coarsening_factor)
    offset_y_px, offset_x_px = _stage1_lattice_offset_px(state.config)
    if scale <= 1:
        offset_y_px = 0
        offset_x_px = 0
    target_oklab = np.asarray(visible_plan.mapped_target_oklab, dtype=np.float32).reshape(shape + (3,))
    predicted_oklab = _predict_stage4_oklab_map(
        state=state,
        visible_plan=visible_plan,
        cap_height_mm=cap_plan.cap_height_mm,
    )
    final_ratio, final_heatmap = _stage2_edge_excess_ratio_and_heatmap(
        predicted_oklab=predicted_oklab,
        target_oklab=target_oklab,
        coarse_to_fine_scale=scale,
        lattice_offset_y_px=int(offset_y_px),
        lattice_offset_x_px=int(offset_x_px),
    )
    boundary_cap = (
        np.asarray(cap_plan.cap_boundary_top_mm, dtype=np.float32)
        - np.asarray(filler_plan.color_ceiling_mm, dtype=np.float32)
    ).astype(np.float32, copy=False)
    raw_cap_reference = _quantize_cap_map(
        np.asarray(visible_plan.implied_cap_height_mm, dtype=np.float32).reshape(shape),
        layer_height=float(state.config.layer_height),
        d_wc_min=float(state.config.d_wc_min),
        d_wc_max=float(state.config.effective_d_wc_max()),
    )
    cap_raw_deviation = np.abs(boundary_cap - raw_cap_reference).astype(np.float32, copy=False)
    cap_ratio, cap_heatmap = _stage2_edge_excess_ratio_and_heatmap(
        predicted_oklab=boundary_cap[..., np.newaxis].repeat(3, axis=2),
        target_oklab=raw_cap_reference[..., np.newaxis].repeat(3, axis=2),
        coarse_to_fine_scale=scale,
        lattice_offset_y_px=int(offset_y_px),
        lattice_offset_x_px=int(offset_x_px),
    )
    target_edge_strength = _compute_target_edge_strength(
        visible_plan.mapped_target_oklab,
        shape,
    )
    positive_source = target_edge_strength[target_edge_strength > 1e-9]
    source_threshold = (
        float(np.percentile(positive_source, _STAGE2_GEOMETRY_ATTR_SOURCE_EDGE_PERCENTILE))
        if positive_source.size
        else float("inf")
    )
    source_edge_support = target_edge_strength >= np.float32(source_threshold)
    ceiling_gradient = _stage4_gradient_magnitude(filler_plan.color_ceiling_mm)
    positive_ceiling = ceiling_gradient[ceiling_gradient > 1e-9]
    ceiling_threshold = (
        max(
            float(np.percentile(positive_ceiling, _STAGE2_GEOMETRY_ATTR_CAP_EDGE_PERCENTILE)),
            0.5 * float(state.config.layer_height),
        )
        if positive_ceiling.size
        else float("inf")
    )
    ceiling_edges = ceiling_gradient >= np.float32(ceiling_threshold)
    cap_transition_distance = distance_transform_edt(~ceiling_edges).astype(np.float32)
    active_pressure = np.asarray(pressure.total_excess, dtype=np.float32) > np.float32(
        _STAGE2_PRESSURE_ACTIVE_THRESHOLD
    )
    positive_blockiness = final_heatmap[final_heatmap > 1e-9]
    blockiness_threshold = (
        float(np.percentile(positive_blockiness, 75.0)) if positive_blockiness.size else float("inf")
    )
    active_blockiness = final_heatmap >= np.float32(blockiness_threshold)
    cap_deviation_threshold = np.float32(
        float(_STAGE2_GEOMETRY_ATTR_CAP_RAW_DEVIATION_LAYERS) * float(state.config.layer_height)
    )
    active_cap_deviation = cap_raw_deviation >= cap_deviation_threshold
    seed_mask = active_pressure | active_blockiness
    if not np.any(seed_mask):
        empty_labels = np.full(shape, -1, dtype=np.int32)
        return Stage2GeometryPressureAttribution(
            component_label_map=empty_labels,
            prefine_class_label_map=np.zeros(shape, dtype=np.int32),
            postfine_class_label_map=np.zeros(shape, dtype=np.int32),
            final_blockiness_heatmap=final_heatmap.astype(np.float32, copy=True),
            cap_raw_deviation_map=cap_raw_deviation.astype(np.float32, copy=True),
            cap_transition_width_map=np.zeros(shape, dtype=np.float32),
            source_edge_support_map=source_edge_support.astype(np.float32),
            prefine_component_facts=(),
            postfine_component_facts=(),
            class_names=tuple(_STAGE2_GEOMETRY_CLASS_NAMES),
            final_blockiness_ratio=float(final_ratio),
            cap_blockiness_ratio=float(cap_ratio),
            active_component_count=0,
            active_pressure_pixels=0,
            active_final_blockiness_pixels=0,
            active_cap_deviation_pixels=0,
        )

    zone_label_map = np.asarray(visible_plan.zone_label_map, dtype=np.int32)
    raw_component_labels = np.zeros(shape, dtype=np.int32)
    next_component_id = 1
    for zone_id in np.unique(zone_label_map).tolist():
        zone_seed = seed_mask & (zone_label_map == int(zone_id))
        if not np.any(zone_seed):
            continue
        local_labels, local_count = nd_label(
            zone_seed,
            structure=generate_binary_structure(2, 1),
        )
        for local_id in range(1, int(local_count) + 1):
            raw_component_labels[local_labels == local_id] = int(next_component_id)
            next_component_id += 1
    component_count = int(next_component_id - 1)
    component_label_map = raw_component_labels.astype(np.int32, copy=False)
    component_label_map[component_label_map == 0] = -1
    prefine_recipe_map = visible_plan.zone_recipe_labels[zone_label_map]
    postfine_recipe_map = np.asarray(visible_plan.recipe_label_map, dtype=np.int32)
    zone_boundary_mask = _stage2_label_boundary_pixel_mask(zone_label_map)
    coarse_lattice_mask = _stage2_coarse_lattice_pixel_mask(
        shape,
        scale,
        offset_y_px=int(offset_y_px),
        offset_x_px=int(offset_x_px),
    )
    stage4_detail_mask = np.asarray(cap_plan.detail_height_mm, dtype=np.float32) > 1e-9
    recipe_boundary_support = _stage4_detail_recipe_boundary_support(visible_plan)
    recipe_boundary_detail_mask = recipe_boundary_support & stage4_detail_mask

    prefine_class_map, prefine_facts = _stage2_geometry_attribution_for_mode(
        mode="prefine",
        component_label_map=raw_component_labels,
        component_count=int(component_count),
        zone_label_map=zone_label_map,
        recipe_label_map=prefine_recipe_map,
        pressure=pressure,
        active_pressure=active_pressure,
        active_blockiness=active_blockiness,
        active_cap_deviation=active_cap_deviation,
        source_edge_support=source_edge_support,
        cap_transition_width_map=cap_transition_distance,
        coarse_lattice_mask=coarse_lattice_mask,
        zone_boundary_mask=zone_boundary_mask,
        stage4_detail_mask=stage4_detail_mask,
        recipe_boundary_detail_mask=recipe_boundary_detail_mask,
    )
    postfine_class_map, postfine_facts = _stage2_geometry_attribution_for_mode(
        mode="postfine",
        component_label_map=raw_component_labels,
        component_count=int(component_count),
        zone_label_map=zone_label_map,
        recipe_label_map=postfine_recipe_map,
        pressure=pressure,
        active_pressure=active_pressure,
        active_blockiness=active_blockiness,
        active_cap_deviation=active_cap_deviation,
        source_edge_support=source_edge_support,
        cap_transition_width_map=cap_transition_distance,
        coarse_lattice_mask=coarse_lattice_mask,
        zone_boundary_mask=zone_boundary_mask,
        stage4_detail_mask=stage4_detail_mask,
        recipe_boundary_detail_mask=recipe_boundary_detail_mask,
    )
    return Stage2GeometryPressureAttribution(
        component_label_map=component_label_map.astype(np.int32, copy=True),
        prefine_class_label_map=prefine_class_map.astype(np.int32, copy=True),
        postfine_class_label_map=postfine_class_map.astype(np.int32, copy=True),
        final_blockiness_heatmap=final_heatmap.astype(np.float32, copy=True),
        cap_raw_deviation_map=cap_raw_deviation.astype(np.float32, copy=True),
        cap_transition_width_map=np.where(
            active_cap_deviation,
            cap_transition_distance,
            np.float32(0.0),
        ).astype(np.float32, copy=True),
        source_edge_support_map=source_edge_support.astype(np.float32),
        prefine_component_facts=prefine_facts,
        postfine_component_facts=postfine_facts,
        class_names=tuple(_STAGE2_GEOMETRY_CLASS_NAMES),
        final_blockiness_ratio=float(final_ratio),
        cap_blockiness_ratio=float(cap_ratio),
        active_component_count=int(component_count),
        active_pressure_pixels=int(np.count_nonzero(active_pressure)),
        active_final_blockiness_pixels=int(np.count_nonzero(active_blockiness)),
        active_cap_deviation_pixels=int(np.count_nonzero(active_cap_deviation)),
    )


def _run_postsolve_diagnostics(
    *,
    state,
    visible_plan,
    filler_plan,
    cap_plan,
    diagnostics,
    performance_profile,
    emit,
) -> None:
    _emit = emit
    if bool(state.config.emit_geometry_attribution):
        _emit("Computing geometry attribution diagnostics...", 78)
        stage_start = time.perf_counter()
        geometry_attribution = _compute_stage2_geometry_pressure_attribution(
            state=state,
            visible_plan=visible_plan,
            filler_plan=filler_plan,
            cap_plan=cap_plan,
        )
        cap_plan.stage2_geometry_pressure_attribution = geometry_attribution
        _record_timing(
            performance_profile,
            "stage2_geometry_pressure_attribution_s",
            time.perf_counter() - stage_start,
        )
        if geometry_attribution is not None:
            _set_counter(
                performance_profile,
                "stage2_geometry_attribution_component_count",
                int(geometry_attribution.active_component_count),
            )
            _set_counter(
                performance_profile,
                "stage2_geometry_final_blockiness_ratio",
                float(geometry_attribution.final_blockiness_ratio),
            )
            _set_counter(
                performance_profile,
                "stage2_geometry_cap_blockiness_ratio",
                float(geometry_attribution.cap_blockiness_ratio),
            )
            for label, class_name in enumerate(geometry_attribution.class_names):
                if int(label) == _STAGE2_GEOMETRY_CLASS_BACKGROUND:
                    continue
                _set_counter(
                    performance_profile,
                    f"stage2_geometry_prefine_{class_name}_pixels",
                    int(np.count_nonzero(geometry_attribution.prefine_class_label_map == int(label))),
                )
                _set_counter(
                    performance_profile,
                    f"stage2_geometry_postfine_{class_name}_pixels",
                    int(np.count_nonzero(geometry_attribution.postfine_class_label_map == int(label))),
                )
            diagnostics.entries.append(
                PlanningDiagnosticEntry(
                    code="stage2_geometry_pressure_attribution",
                    severity="info",
                    message=(
                        "Phase C geometry attribution emitted "
                        f"{int(geometry_attribution.active_component_count)} components; "
                        f"final blockiness ratio={float(geometry_attribution.final_blockiness_ratio):.4f}."
                    ),
                )
            )

    if bool(state.config.emit_blueprint_printability):
        _emit("Checking blueprint printability...", 84)
        stage_start = time.perf_counter()
        printability_settings = resolve_blueprint_printability_settings(
            state.config,
            pitch_mm=float(state.config.solver_fine_pitch_mm),
        )
        layered_view = build_layered_blueprint_view(
            visible_plan=visible_plan,
            cap_plan=cap_plan,
            palette_order=list(state.config.palette),
            d_wb_mm=float(state.config.d_wb),
            layer_height_mm=float(state.config.layer_height),
        )
        blueprint_printability = run_blueprint_printability_diagnostic(
            layered_view,
            printability_settings,
        )
        cap_plan.blueprint_printability_diagnostic = blueprint_printability
        _record_timing(
            performance_profile,
            "blueprint_printability_diagnostic_s",
            time.perf_counter() - stage_start,
        )
        _set_counter(
            performance_profile,
            "blueprint_printability_runtime_s",
            float(blueprint_printability.runtime_s),
        )
        _set_counter(
            performance_profile,
            "blueprint_printability_extrusion_width_mm",
            float(blueprint_printability.extrusion_width_mm),
        )
        _set_counter(
            performance_profile,
            "blueprint_printability_minimum_line_length_mm",
            float(blueprint_printability.minimum_line_length_mm),
        )
        _set_counter(
            performance_profile,
            "blueprint_printability_layer_count",
            int(
                blueprint_printability.color_layer_count
                + blueprint_printability.cap_layer_count
                + blueprint_printability.detail_layer_count
            ),
        )
        _set_counter(
            performance_profile,
            "blueprint_printability_color_layer_count",
            int(blueprint_printability.color_layer_count),
        )
        _set_counter(
            performance_profile,
            "blueprint_printability_cap_layer_count",
            int(blueprint_printability.cap_layer_count),
        )
        _set_counter(
            performance_profile,
            "blueprint_printability_detail_layer_count",
            int(blueprint_printability.detail_layer_count),
        )
        _set_counter(
            performance_profile,
            "blueprint_printability_checked_masks",
            int(blueprint_printability.checked_mask_count),
        )
        _set_counter(
            performance_profile,
            "blueprint_printability_cap_checked_masks",
            int(blueprint_printability.cap_checked_mask_count),
        )
        _set_counter(
            performance_profile,
            "blueprint_printability_detail_checked_masks",
            int(blueprint_printability.detail_checked_mask_count),
        )
        _set_counter(
            performance_profile,
            "blueprint_printability_hard_fail_component_count",
            int(blueprint_printability.hard_fail_component_count),
        )
        _set_counter(
            performance_profile,
            "blueprint_printability_hard_fail_pixels",
            int(blueprint_printability.hard_fail_pixels),
        )
        _set_counter(
            performance_profile,
            "blueprint_printability_color_hard_fail_pixels",
            int(blueprint_printability.color_hard_fail_pixels),
        )
        _set_counter(
            performance_profile,
            "blueprint_printability_cap_hard_fail_pixels",
            int(blueprint_printability.cap_hard_fail_pixels),
        )
        _set_counter(
            performance_profile,
            "blueprint_printability_boundary_cap_hard_fail_pixels",
            int(blueprint_printability.cap_hard_fail_pixels),
        )
        _set_counter(
            performance_profile,
            "blueprint_printability_detail_hard_fail_pixels",
            int(blueprint_printability.detail_hard_fail_pixels),
        )
        _set_counter(
            performance_profile,
            "blueprint_printability_tiny_component_count",
            int(blueprint_printability.tiny_component_count),
        )
        _set_counter(
            performance_profile,
            "blueprint_printability_tiny_component_pixels",
            int(blueprint_printability.tiny_component_pixels),
        )
        _set_counter(
            performance_profile,
            "blueprint_printability_narrow_width_pixels",
            int(blueprint_printability.narrow_width_pixels),
        )
        _set_counter(
            performance_profile,
            "blueprint_printability_opening_width_failure_component_count",
            int(blueprint_printability.opening_width_failure_component_count),
        )
        _set_counter(
            performance_profile,
            "blueprint_printability_opening_width_failure_pixels",
            int(blueprint_printability.opening_width_failure_pixels),
        )
        _set_counter(
            performance_profile,
            "blueprint_printability_short_component_count",
            int(blueprint_printability.short_component_count),
        )
        _set_counter(
            performance_profile,
            "blueprint_printability_low_support_component_count",
            int(blueprint_printability.low_support_component_count),
        )
        _set_counter(
            performance_profile,
            "blueprint_printability_low_support_pixels",
            int(blueprint_printability.low_support_pixels),
        )
        _set_counter(
            performance_profile,
            "blueprint_printability_min_component_support_fraction",
            float(blueprint_printability.min_component_support_fraction),
        )
        _set_counter(
            performance_profile,
            "blueprint_printability_max_center_clearance_mm",
            float(blueprint_printability.max_center_clearance_mm),
        )
        _set_counter(
            performance_profile,
            "blueprint_printability_hard_fail_projected_component_count",
            int(blueprint_printability.hard_fail_projected_component_count),
        )
        _set_counter(
            performance_profile,
            "blueprint_printability_hard_fail_projected_largest_component_fraction",
            float(blueprint_printability.hard_fail_projected_largest_component_fraction),
        )
        _set_counter(
            performance_profile,
            "blueprint_printability_color_hard_fail_projected_component_count",
            int(blueprint_printability.color_hard_fail_projected_component_count),
        )
        _set_counter(
            performance_profile,
            "blueprint_printability_color_hard_fail_projected_largest_component_fraction",
            float(blueprint_printability.color_hard_fail_projected_largest_component_fraction),
        )
        _set_counter(
            performance_profile,
            "blueprint_printability_color_hard_fail_cluster_radius_px",
            int(blueprint_printability.color_hard_fail_cluster_radius_px),
        )
        _set_counter(
            performance_profile,
            "blueprint_printability_color_hard_fail_cluster_component_count",
            int(blueprint_printability.color_hard_fail_cluster_component_count),
        )
        _set_counter(
            performance_profile,
            "blueprint_printability_color_hard_fail_largest_cluster_fraction",
            float(blueprint_printability.color_hard_fail_largest_cluster_fraction),
        )
        _set_counter(
            performance_profile,
            "blueprint_printability_worst_layer_index",
            int(blueprint_printability.worst_layer_index),
        )
        _set_counter(
            performance_profile,
            "blueprint_printability_worst_surface",
            str(blueprint_printability.worst_surface),
        )
        diagnostics.entries.append(
            PlanningDiagnosticEntry(
                code="blueprint_printability_diagnostic",
                severity=(
                    "warning"
                    if blueprint_printability.hard_fail_component_count > 0
                    else "info"
                ),
                message=(
                    "Blueprint printability diagnostic found "
                    f"{int(blueprint_printability.hard_fail_component_count)} hard-fail "
                    "components."
                ),
            )
        )

__all__ = (
    '_run_postsolve_diagnostics',
    '_STAGE2_GEOMETRY_ATTR_CAP_EDGE_PERCENTILE',
    '_STAGE2_GEOMETRY_ATTR_CAP_RAW_DEVIATION_LAYERS',
    '_STAGE2_GEOMETRY_ATTR_CAP_SIDE_PRESSURE_OVERLAP_MAX',
    '_STAGE2_GEOMETRY_ATTR_CAP_SIDE_RAW_DEVIATION_MIN',
    '_STAGE2_GEOMETRY_ATTR_WHOLE_ZONE_PRESSURE_FRACTION',
    '_STAGE2_GEOMETRY_ATTR_WHOLE_ZONE_MODAL_ALT_FRACTION',
    '_STAGE2_GEOMETRY_ATTR_SOURCE_EDGE_SUPPORT',
    '_STAGE2_GEOMETRY_ATTR_LATTICE_OVERLAP',
    '_STAGE2_GEOMETRY_ATTR_INTERIOR_MIN_PIXELS',
    '_STAGE2_GEOMETRY_ATTR_INTERIOR_MODAL_ALT_FRACTION',
    '_STAGE2_GEOMETRY_CLASS_NAMES',
    '_STAGE2_GEOMETRY_CLASS_BACKGROUND',
    '_STAGE2_GEOMETRY_CLASS_CAP_SIDE',
    '_STAGE2_GEOMETRY_CLASS_WHOLE_ZONE',
    '_STAGE2_GEOMETRY_CLASS_SOURCE_EDGE',
    '_STAGE2_GEOMETRY_CLASS_INTERIOR_SUBZONE',
    '_STAGE2_GEOMETRY_CLASS_COARSE_LATTICE_BOUNDARY',
    '_STAGE2_GEOMETRY_CLASS_STAGE2_ZONE_BOUNDARY',
    '_STAGE2_GEOMETRY_CLASS_CROSS_BOUNDARY',
    '_STAGE2_GEOMETRY_CLASS_AMBIGUOUS',
    '_predict_stage4_oklab_map',
    '_stage2_edge_excess_ratio_and_heatmap',
    '_stage2_label_boundary_pixel_mask',
    '_stage2_internal_recipe_edge_density',
    '_stage2_geometry_class_name',
    '_stage2_geometry_attribution_for_mode',
    '_compute_stage2_geometry_pressure_attribution',
)
