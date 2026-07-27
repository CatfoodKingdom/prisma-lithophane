"""Stage 2 candidate-pressure analysis."""
from __future__ import annotations

import hashlib

import numpy as np


from ...staged_artifacts import Stage2RecipePressure

from ..coarse_grid import _stage2_coarse_lattice_edge_masks
from ..image_analysis import _compute_target_edge_strength
from ..recipe_pressure import _STAGE2_PRESSURE_ACTIVE_THRESHOLD

from .contracts import (
    _ZoneCandidateSet,
    _ZoneRecipeOptimizationResult,
)
from .candidates import (
    _STAGE2_FRONTIER_SIZE,
    _STAGE2_FRONTIER_OPTICAL_RESCUE_MAX_EXTRA,
    _STAGE2_FRONTIER_OPTICAL_RESCUE_RANK_BUDGET,
    _STAGE2_FRONTIER_OPTICAL_RESCUE_MIN_SCORE_GAP,
    _STAGE2_FRONTIER_PRESSURE_RESCUE_MIN_PIXELS,
)
from .objective import (
    _STAGE2_RETAINING_WALL_WEIGHT,
    _STAGE2_ZONE_LOCAL_WEIGHT_EXPONENT,
    _STAGE2_ZONE_LOCAL_WEIGHT_MIN,
    _STAGE2_ZONE_LOCAL_WEIGHT_MAX,
    _score_zone_pixels_against_candidates,
    _score_pixels_best_against_candidates,
)
from .optimization import (
    _STAGE2_MAX_COORD_DESCENT_PASSES,
    _STAGE2_BEAM_WIDTH,
    _STAGE2_PAIR_REPAIR_PASSES,
    _STAGE2_PAIR_REPAIR_EDGE_PROBE_COUNT,
    _selected_zone_stack_ids,
)

def _stage2_frontier_config_hash(
    *,
    continuity_weight: float,
    area_weighted_zone_choice: bool,
    pressure_frontier_rescue: bool,
    source_edge_subzones: bool,
    lattice_offset_y_px: int,
    lattice_offset_x_px: int,
) -> str:
    """Return a short stamp for Stage 2 frontier/optimizer settings."""
    payload = "|".join(
        (
            f"continuity={float(continuity_weight):.8g}",
            f"area_weighted_zone_choice={bool(area_weighted_zone_choice)}",
            f"pressure_frontier_rescue={bool(pressure_frontier_rescue)}",
            f"source_edge_subzones={bool(source_edge_subzones)}",
            f"lattice_offset_y_px={int(lattice_offset_y_px)}",
            f"lattice_offset_x_px={int(lattice_offset_x_px)}",
            f"retaining={_STAGE2_RETAINING_WALL_WEIGHT:.8g}",
            f"frontier={_STAGE2_FRONTIER_SIZE}",
            f"frontier_rescue_extra={_STAGE2_FRONTIER_OPTICAL_RESCUE_MAX_EXTRA}",
            f"frontier_rescue_rank={_STAGE2_FRONTIER_OPTICAL_RESCUE_RANK_BUDGET}",
            f"frontier_rescue_gap={_STAGE2_FRONTIER_OPTICAL_RESCUE_MIN_SCORE_GAP:.8g}",
            f"frontier_pressure_rescue_min_pixels={_STAGE2_FRONTIER_PRESSURE_RESCUE_MIN_PIXELS}",
            f"zone_local_weight_exp={_STAGE2_ZONE_LOCAL_WEIGHT_EXPONENT:.8g}",
            f"zone_local_weight_min={_STAGE2_ZONE_LOCAL_WEIGHT_MIN:.8g}",
            f"zone_local_weight_max={_STAGE2_ZONE_LOCAL_WEIGHT_MAX:.8g}",
            f"beam={_STAGE2_BEAM_WIDTH}",
            f"coord={_STAGE2_MAX_COORD_DESCENT_PASSES}",
            f"pair={_STAGE2_PAIR_REPAIR_PASSES}",
            f"pair_probe={_STAGE2_PAIR_REPAIR_EDGE_PROBE_COUNT}",
        )
    )
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]

def _clip_stage2_pressure_gap(values: np.ndarray) -> tuple[np.ndarray, int]:
    """Clip tiny negative diagnostic gaps and count true violations."""
    arr = np.asarray(values, dtype=np.float32)
    finite = np.isfinite(arr)
    violation_mask = finite & (arr < np.float32(-1e-5))
    clipped = np.where(finite, np.maximum(arr, np.float32(0.0)), np.float32(0.0))
    return clipped.astype(np.float32, copy=False), int(np.count_nonzero(violation_mask))

def _stage2_edge_gradient_values(values: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Return vertical and horizontal neighbor-edge absolute gradients."""
    arr = np.asarray(values, dtype=np.float32)
    y_grad = np.abs(arr[:-1, :] - arr[1:, :]).astype(np.float32, copy=False)
    x_grad = np.abs(arr[:, :-1] - arr[:, 1:]).astype(np.float32, copy=False)
    return y_grad, x_grad

def _stage2_target_edge_values(
    targets: np.ndarray,
    fine_shape: tuple[int, int],
) -> tuple[np.ndarray, np.ndarray]:
    """Return target OKLab 4-neighbor edge magnitudes."""
    target_grid = np.asarray(targets, dtype=np.float32).reshape(fine_shape + (3,))
    y_edge = np.sqrt(
        np.sum((target_grid[:-1, :, :] - target_grid[1:, :, :]) ** 2, axis=2),
        dtype=np.float32,
    )
    x_edge = np.sqrt(
        np.sum((target_grid[:, :-1, :] - target_grid[:, 1:, :]) ** 2, axis=2),
        dtype=np.float32,
    )
    return y_edge.astype(np.float32, copy=False), x_edge.astype(np.float32, copy=False)

def _stage2_pressure_blockiness(
    *,
    pressure: np.ndarray,
    targets: np.ndarray,
    fine_shape: tuple[int, int],
    coarse_to_fine_scale: int,
    lattice_offset_y_px: int = 0,
    lattice_offset_x_px: int = 0,
) -> tuple[float, np.ndarray]:
    """Return a coarse-lattice pressure-edge ratio and full-size heatmap."""
    shape = tuple(int(dim) for dim in fine_shape)
    heatmap = np.zeros(shape, dtype=np.float32)
    scale = int(coarse_to_fine_scale)
    if scale <= 1 or shape[0] == 0 or shape[1] == 0:
        return 0.0, heatmap

    y_grad, x_grad = _stage2_edge_gradient_values(np.asarray(pressure, dtype=np.float32).reshape(shape))
    y_source, x_source = _stage2_target_edge_values(targets, shape)
    source_edges = np.concatenate([y_source.reshape(-1), x_source.reshape(-1)])
    positive_source = source_edges[source_edges > 1e-9]
    source_cutoff = (
        float(np.percentile(positive_source, 85.0))
        if positive_source.size
        else float("inf")
    )
    y_eligible = y_source <= np.float32(source_cutoff)
    x_eligible = x_source <= np.float32(source_cutoff)

    y_lattice, x_lattice = _stage2_coarse_lattice_edge_masks(
        shape,
        scale,
        offset_y_px=int(lattice_offset_y_px),
        offset_x_px=int(lattice_offset_x_px),
    )

    y_lattice_values = y_grad[y_lattice & y_eligible]
    x_lattice_values = x_grad[x_lattice & x_eligible]
    y_control_values = y_grad[(~y_lattice) & y_eligible]
    x_control_values = x_grad[(~x_lattice) & x_eligible]
    lattice_values = np.concatenate([y_lattice_values, x_lattice_values])
    control_values = np.concatenate([y_control_values, x_control_values])
    lattice_mean = float(np.mean(lattice_values)) if lattice_values.size else 0.0
    control_mean = float(np.mean(control_values)) if control_values.size else 0.0
    ratio = lattice_mean / max(control_mean, 1e-9) if lattice_mean > 0.0 else 0.0

    y_heat = np.where(y_lattice & y_eligible, y_grad, np.float32(0.0))
    x_heat = np.where(x_lattice & x_eligible, x_grad, np.float32(0.0))
    heatmap[:-1, :] = np.maximum(heatmap[:-1, :], y_heat)
    heatmap[1:, :] = np.maximum(heatmap[1:, :], y_heat)
    heatmap[:, :-1] = np.maximum(heatmap[:, :-1], x_heat)
    heatmap[:, 1:] = np.maximum(heatmap[:, 1:], x_heat)
    return float(ratio), heatmap.astype(np.float32, copy=False)

def _compute_stage2_recipe_pressure(
    *,
    fine_shape: tuple[int, int],
    coarse_to_fine_scale: int,
    lattice_offset_y_px: int = 0,
    lattice_offset_x_px: int = 0,
    zone_label_map: np.ndarray,
    zone_flat_indices: tuple[np.ndarray, ...],
    targets: np.ndarray,
    pixel_stack_ids: np.ndarray,
    preprune_candidate_sets: tuple[_ZoneCandidateSet, ...],
    pruned_candidate_sets: tuple[_ZoneCandidateSet, ...],
    optimization: _ZoneRecipeOptimizationResult,
    all_oklabs: np.ndarray,
    frontier_config_hash: str,
) -> Stage2RecipePressure:
    """Compute read-only pressure diagnostics without changing assignments."""
    total_pixels = int(fine_shape[0] * fine_shape[1])
    selected_score = np.full(total_pixels, np.float32(np.inf), dtype=np.float32)
    frontier_best_score = np.full(total_pixels, np.float32(np.inf), dtype=np.float32)
    preprune_best_score = np.full(total_pixels, np.float32(np.inf), dtype=np.float32)
    local_best_score = np.full(total_pixels, np.float32(np.inf), dtype=np.float32)
    frontier_best_stack_id = np.full(total_pixels, -1, dtype=np.int32)
    preprune_best_stack_id = np.full(total_pixels, -1, dtype=np.int32)
    local_best_stack_id = np.full(total_pixels, -1, dtype=np.int32)
    selected_zone_stack_ids = _selected_zone_stack_ids(pruned_candidate_sets, optimization)
    valid_stack_ids = np.flatnonzero(
        np.any(np.isfinite(np.asarray(all_oklabs, dtype=np.float32)[..., 0]), axis=1)
    ).astype(np.int32, copy=False)
    if valid_stack_ids.size == 0:
        valid_stack_ids = np.unique(
            np.asarray(pixel_stack_ids, dtype=np.int32).reshape(-1)
        ).astype(np.int32, copy=False)
        valid_stack_ids = valid_stack_ids[valid_stack_ids >= 0]
    if valid_stack_ids.size:
        computed_local_scores, computed_local_stack_ids = _score_pixels_best_against_candidates(
            targets,
            valid_stack_ids,
            all_oklabs,
        )
        local_best_score[:] = computed_local_scores
        local_best_stack_id[:] = computed_local_stack_ids

    for zone_id, indices in enumerate(zone_flat_indices):
        if indices.size == 0:
            continue
        flat_indices = indices.astype(np.int64, copy=False)
        zone_targets = np.asarray(targets[flat_indices], dtype=np.float32)
        selected_stack_id = int(selected_zone_stack_ids[zone_id])
        if selected_stack_id >= 0:
            selected_score[flat_indices] = _score_zone_pixels_against_candidates(
                zone_targets,
                np.array([selected_stack_id], dtype=np.int32),
                all_oklabs,
            )[:, 0]

        frontier_ids = pruned_candidate_sets[zone_id].candidate_ids.astype(np.int32, copy=False)
        if frontier_ids.size:
            frontier_scores, frontier_stack_ids = _score_pixels_best_against_candidates(
                zone_targets,
                frontier_ids,
                all_oklabs,
            )
            frontier_best_score[flat_indices] = frontier_scores
            frontier_best_stack_id[flat_indices] = frontier_stack_ids

        preprune_ids = preprune_candidate_sets[zone_id].candidate_ids.astype(np.int32, copy=False)
        if preprune_ids.size:
            preprune_scores, preprune_stack_ids = _score_pixels_best_against_candidates(
                zone_targets,
                preprune_ids,
                all_oklabs,
            )
            preprune_best_score[flat_indices] = preprune_scores
            preprune_best_stack_id[flat_indices] = preprune_stack_ids

    selected_grid = selected_score.reshape(fine_shape)
    frontier_grid = frontier_best_score.reshape(fine_shape)
    preprune_grid = preprune_best_score.reshape(fine_shape)
    local_grid = local_best_score.reshape(fine_shape)
    coarse_excess, neg0 = _clip_stage2_pressure_gap(selected_grid - frontier_grid)
    pruning_gap, neg1 = _clip_stage2_pressure_gap(frontier_grid - preprune_grid)
    local_gap, neg2 = _clip_stage2_pressure_gap(preprune_grid - local_grid)
    total_excess, neg3 = _clip_stage2_pressure_gap(selected_grid - local_grid)
    violation_pixels = int(neg0 + neg1 + neg2 + neg3)

    zone_count = len(zone_flat_indices)
    whole_fraction = np.zeros(zone_count, dtype=np.float32)
    interior_pixels = np.zeros(zone_count, dtype=np.int32)
    active_threshold = np.float32(_STAGE2_PRESSURE_ACTIVE_THRESHOLD)
    flat_coarse = coarse_excess.reshape(-1)
    flat_frontier_stack = frontier_best_stack_id.reshape(-1)
    for zone_id, indices in enumerate(zone_flat_indices):
        if indices.size == 0:
            continue
        flat_indices = indices.astype(np.int64, copy=False)
        selected_stack_id = int(selected_zone_stack_ids[zone_id])
        active = (
            (flat_coarse[flat_indices] > active_threshold)
            & (flat_frontier_stack[flat_indices] >= 0)
            & (flat_frontier_stack[flat_indices] != selected_stack_id)
        )
        active_count = int(np.count_nonzero(active))
        if active_count == 0:
            continue
        active_stacks = flat_frontier_stack[flat_indices][active]
        unique_stacks, counts = np.unique(active_stacks, return_counts=True)
        modal_count = int(counts[np.argmax(counts)]) if unique_stacks.size else 0
        whole_fraction[zone_id] = np.float32(modal_count / float(active_count))
        interior_pixels[zone_id] = int(active_count - modal_count)

    active_pressure = coarse_excess > active_threshold
    labels = np.asarray(zone_label_map, dtype=np.int32)
    boundary = np.zeros(labels.shape, dtype=bool)
    if labels.shape[0] > 1:
        dy = labels[:-1, :] != labels[1:, :]
        boundary[:-1, :] |= dy
        boundary[1:, :] |= dy
    if labels.shape[1] > 1:
        dx = labels[:, :-1] != labels[:, 1:]
        boundary[:, :-1] |= dx
        boundary[:, 1:] |= dx
    cross_boundary_pixels = int(np.count_nonzero(active_pressure & boundary))

    edge_strength = _compute_target_edge_strength(targets, fine_shape).reshape(-1)
    pressure_values = total_excess.reshape(-1)
    if (
        pressure_values.size
        and float(np.std(pressure_values)) > 1e-9
        and float(np.std(edge_strength)) > 1e-9
    ):
        corr = float(np.corrcoef(pressure_values.astype(np.float64), edge_strength.astype(np.float64))[0, 1])
        if not np.isfinite(corr):
            corr = 0.0
    else:
        corr = 0.0

    blockiness_ratio, blockiness_heatmap = _stage2_pressure_blockiness(
        pressure=total_excess,
        targets=targets,
        fine_shape=fine_shape,
        coarse_to_fine_scale=coarse_to_fine_scale,
        lattice_offset_y_px=int(lattice_offset_y_px),
        lattice_offset_x_px=int(lattice_offset_x_px),
    )

    return Stage2RecipePressure(
        selected_score=selected_grid.astype(np.float32, copy=True),
        frontier_best_score=frontier_grid.astype(np.float32, copy=True),
        preprune_best_score=preprune_grid.astype(np.float32, copy=True),
        local_best_score=local_grid.astype(np.float32, copy=True),
        coarse_excess=coarse_excess.astype(np.float32, copy=True),
        pruning_gap=pruning_gap.astype(np.float32, copy=True),
        local_gap=local_gap.astype(np.float32, copy=True),
        total_excess=total_excess.astype(np.float32, copy=True),
        frontier_best_stack_id=frontier_best_stack_id.reshape(fine_shape).astype(np.int32, copy=True),
        preprune_best_stack_id=preprune_best_stack_id.reshape(fine_shape).astype(np.int32, copy=True),
        local_best_stack_id=local_best_stack_id.reshape(fine_shape).astype(np.int32, copy=True),
        whole_zone_pressure_fraction_by_zone=whole_fraction.astype(np.float32, copy=True),
        interior_pressure_pixels_by_zone=interior_pixels.astype(np.int32, copy=True),
        cross_boundary_pressure_pixels=int(cross_boundary_pixels),
        pressure_x_image_edge_corr=float(corr),
        blockiness_energy_ratio=float(blockiness_ratio),
        blockiness_heatmap=blockiness_heatmap.astype(np.float32, copy=True),
        frontier_config_hash=str(frontier_config_hash),
        negative_gap_violation_pixels=int(violation_pixels),
    )

__all__ = (
    '_stage2_frontier_config_hash',
    '_clip_stage2_pressure_gap',
    '_stage2_edge_gradient_values',
    '_stage2_target_edge_values',
    '_stage2_pressure_blockiness',
    '_compute_stage2_recipe_pressure',
)
