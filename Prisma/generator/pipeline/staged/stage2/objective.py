"""Stage 2 scoring, zone topology, and objective kernels."""
from __future__ import annotations


import numpy as np
from scipy.spatial import KDTree




from .contracts import (
    _ZoneCandidateSet,
    _ZoneCostBreakdown,
)

_STAGE2_CONTINUITY_WEIGHT = 0.12

_STAGE2_RETAINING_WALL_WEIGHT = 0.02

_STAGE2_ZONE_LOCAL_WEIGHT_EXPONENT = 0.5

_STAGE2_ZONE_LOCAL_WEIGHT_MIN = 0.5

_STAGE2_ZONE_LOCAL_WEIGHT_MAX = 4.0

_STAGE2_ZONE_SCORE_MAX_BROADCAST_FLOATS = 8_000_000

def _stage2_continuity_weight(cfg) -> float:
    """Return the Stage 2 continuity weight, allowing experimental sweeps."""
    raw = cfg.stage2_continuity_weight
    if raw is None:
        return float(_STAGE2_CONTINUITY_WEIGHT)
    return max(0.0, float(raw))

def _build_zone_neighbors(
    zone_count: int,
    adjacency_edges: tuple[tuple[int, int], ...],
    adjacency_edge_lengths_px: np.ndarray,
) -> list[list[tuple[int, float]]]:
    """Build weighted zone-neighbor lists from the Stage 1 adjacency artifact."""
    neighbors: list[list[tuple[int, float]]] = [[] for _ in range(zone_count)]
    for edge_index, (lhs, rhs) in enumerate(adjacency_edges):
        weight = float(adjacency_edge_lengths_px[edge_index])
        neighbors[int(lhs)].append((int(rhs), weight))
        neighbors[int(rhs)].append((int(lhs), weight))
    return neighbors

def _build_zone_edge_indices(
    zone_count: int,
    adjacency_edges: tuple[tuple[int, int], ...],
) -> list[list[int]]:
    """Build per-zone incident edge indices for local exact-delta updates."""
    zone_edge_indices: list[list[int]] = [[] for _ in range(zone_count)]
    for edge_index, (lhs, rhs) in enumerate(adjacency_edges):
        zone_edge_indices[int(lhs)].append(int(edge_index))
        zone_edge_indices[int(rhs)].append(int(edge_index))
    return zone_edge_indices

def _candidate_retaining_penalties(
    candidate_sets: tuple[_ZoneCandidateSet, ...],
) -> tuple[np.ndarray, ...]:
    """Per-candidate retaining-wall penalties for each zone."""
    penalties: list[np.ndarray] = []
    for candidate_set in candidate_sets:
        if candidate_set.total_thickness_mm.size == 0:
            penalties.append(np.zeros(0, dtype=np.float32))
            continue
        min_total = float(np.min(candidate_set.total_thickness_mm))
        penalties.append(
            np.maximum(0.0, candidate_set.total_thickness_mm - min_total).astype(
                np.float32,
                copy=False,
            )
        )
    return tuple(penalties)

def _zone_local_cost_weights(zone_pixel_counts: np.ndarray | None, zone_count: int) -> np.ndarray:
    """Return area weights for per-zone optical terms, normalized around 1."""
    if zone_pixel_counts is None:
        return np.ones(zone_count, dtype=np.float32)
    counts = np.asarray(zone_pixel_counts, dtype=np.float32).reshape(-1)
    if counts.size != int(zone_count):
        return np.ones(zone_count, dtype=np.float32)
    positive = counts[counts > 0]
    if positive.size == 0:
        return np.ones(zone_count, dtype=np.float32)
    mean_count = float(np.mean(positive))
    if mean_count <= 1e-9:
        return np.ones(zone_count, dtype=np.float32)
    weights = np.where(counts > 0, counts / np.float32(mean_count), np.float32(1.0))
    weights = np.power(
        weights,
        np.float32(_STAGE2_ZONE_LOCAL_WEIGHT_EXPONENT),
        dtype=np.float32,
    )
    weights = np.clip(
        weights,
        np.float32(_STAGE2_ZONE_LOCAL_WEIGHT_MIN),
        np.float32(_STAGE2_ZONE_LOCAL_WEIGHT_MAX),
    )
    return weights.astype(np.float32, copy=False)

def _selected_total_thicknesses(
    selected_stack_ids: np.ndarray,
    candidate_sets: tuple[_ZoneCandidateSet, ...],
) -> np.ndarray:
    """Return one total visible thickness per currently selected zone recipe."""
    totals = np.zeros(len(candidate_sets), dtype=np.float32)
    for zone_id, candidate_set in enumerate(candidate_sets):
        if candidate_set.total_thickness_mm.size == 0:
            continue
        totals[zone_id] = float(candidate_set.total_thickness_mm[int(selected_stack_ids[zone_id])])
    return totals

def _zone_objective_breakdown(
    *,
    zone_id: int,
    candidate_index: int,
    selected_stack_ids: np.ndarray,
    candidate_sets: tuple[_ZoneCandidateSet, ...],
    neighbors: list[list[tuple[int, float]]],
    continuity_weight: float,
    retaining_wall_weight: float,
    local_cost_weight: float = 1.0,
) -> _ZoneCostBreakdown:
    """Compute the Stage 2 objective terms for one zone/candidate choice."""
    candidate_set = candidate_sets[zone_id]
    total_mm = float(candidate_set.total_thickness_mm[candidate_index])
    local_cost = float(local_cost_weight) * float(candidate_set.local_scores[candidate_index])
    boundary_cost = 0.0
    boundary_weight_sum = 0.0
    for neighbor_zone_id, edge_weight in neighbors[zone_id]:
        neighbor_candidate_index = int(selected_stack_ids[neighbor_zone_id])
        neighbor_set = candidate_sets[neighbor_zone_id]
        if neighbor_set.total_thickness_mm.size == 0:
            continue
        neighbor_total_mm = float(neighbor_set.total_thickness_mm[neighbor_candidate_index])
        step_mm = abs(total_mm - neighbor_total_mm)
        boundary_cost += edge_weight * (step_mm ** 2)
        boundary_weight_sum += edge_weight
    if boundary_weight_sum > 0.0:
        boundary_cost /= boundary_weight_sum
    min_total = float(np.min(candidate_set.total_thickness_mm)) if candidate_set.total_thickness_mm.size else 0.0
    retaining_cost = max(0.0, total_mm - min_total)
    total_cost = (
        local_cost
        + float(continuity_weight) * boundary_cost
        + float(retaining_wall_weight) * retaining_cost
    )
    return _ZoneCostBreakdown(
        local_cost=local_cost,
        boundary_cost=boundary_cost,
        retaining_cost=retaining_cost,
        total_cost=total_cost,
    )

def _mean_boundary_step_mm(
    selected_stack_ids: np.ndarray,
    candidate_sets: tuple[_ZoneCandidateSet, ...],
    adjacency_edges: tuple[tuple[int, int], ...],
    adjacency_edge_lengths_px: np.ndarray,
) -> float:
    """Average shared-boundary color-ceiling step in millimeters."""
    if len(adjacency_edges) == 0:
        return 0.0
    total_weight = float(np.sum(adjacency_edge_lengths_px))
    if total_weight <= 0.0:
        return 0.0
    weighted_sum = 0.0
    for edge_index, (lhs, rhs) in enumerate(adjacency_edges):
        lhs_choice = int(selected_stack_ids[int(lhs)])
        rhs_choice = int(selected_stack_ids[int(rhs)])
        lhs_set = candidate_sets[int(lhs)]
        rhs_set = candidate_sets[int(rhs)]
        lhs_total = float(lhs_set.total_thickness_mm[lhs_choice]) if lhs_set.total_thickness_mm.size else 0.0
        rhs_total = float(rhs_set.total_thickness_mm[rhs_choice]) if rhs_set.total_thickness_mm.size else 0.0
        weighted_sum += float(adjacency_edge_lengths_px[edge_index]) * abs(lhs_total - rhs_total)
    return weighted_sum / total_weight

def _edge_step_arrays_mm(
    selected_stack_ids: np.ndarray,
    candidate_sets: tuple[_ZoneCandidateSet, ...],
    adjacency_edges: tuple[tuple[int, int], ...],
) -> np.ndarray:
    """Per-edge visible-thickness steps for one Stage 2 assignment."""
    if len(adjacency_edges) == 0:
        return np.zeros(0, dtype=np.float32)
    steps = np.zeros(len(adjacency_edges), dtype=np.float32)
    for edge_index, (lhs, rhs) in enumerate(adjacency_edges):
        lhs_choice = int(selected_stack_ids[int(lhs)])
        rhs_choice = int(selected_stack_ids[int(rhs)])
        lhs_set = candidate_sets[int(lhs)]
        rhs_set = candidate_sets[int(rhs)]
        lhs_total = float(lhs_set.total_thickness_mm[lhs_choice]) if lhs_set.total_thickness_mm.size else 0.0
        rhs_total = float(rhs_set.total_thickness_mm[rhs_choice]) if rhs_set.total_thickness_mm.size else 0.0
        steps[edge_index] = abs(lhs_total - rhs_total)
    return steps

def _score_zone_pixels_against_candidates(
    zone_targets: np.ndarray,
    candidate_ids: np.ndarray,
    all_oklabs: np.ndarray,
    *,
    max_broadcast_floats: int = _STAGE2_ZONE_SCORE_MAX_BROADCAST_FLOATS,
) -> np.ndarray:
    """Return per-pixel optimal-cap dE for each candidate visible stack."""
    target_arr = np.asarray(zone_targets, dtype=np.float32)
    ids = np.asarray(candidate_ids, dtype=np.int32)
    if target_arr.size == 0 or ids.size == 0:
        return np.zeros((len(target_arr), len(ids)), dtype=np.float32)
    curves = np.asarray(all_oklabs[ids], dtype=np.float32)
    valid_steps = np.isfinite(curves[..., 0])
    pixel_count = int(target_arr.shape[0])
    candidate_count = int(curves.shape[0])
    cap_step_count = int(curves.shape[1])
    broadcast_floats = pixel_count * candidate_count * cap_step_count * 3
    if broadcast_floats <= int(max_broadcast_floats):
        diff = target_arr[:, None, None, :] - curves[None, :, :, :]
        squared = np.sum(diff * diff, axis=3, dtype=np.float32)
        squared = np.where(valid_steps[None, :, :], squared, np.float32(np.inf))
        best_squared = np.min(squared, axis=2)
        return np.sqrt(best_squared, dtype=np.float32)

    scores = np.empty((pixel_count, candidate_count), dtype=np.float32)
    max_pairs = max(1, int(max_broadcast_floats) // max(1, cap_step_count * 3))
    candidate_chunk = min(candidate_count, 64)
    pixel_chunk = max(1, max_pairs // max(1, candidate_chunk))
    if pixel_chunk < 32 and candidate_chunk > 1:
        candidate_chunk = max(1, min(candidate_count, max_pairs // 32))
        pixel_chunk = max(1, max_pairs // max(1, candidate_chunk))

    for pixel_start in range(0, pixel_count, pixel_chunk):
        pixel_stop = min(pixel_count, pixel_start + pixel_chunk)
        target_chunk = target_arr[pixel_start:pixel_stop]
        for candidate_start in range(0, candidate_count, candidate_chunk):
            candidate_stop = min(candidate_count, candidate_start + candidate_chunk)
            curve_chunk = curves[candidate_start:candidate_stop]
            valid_chunk = valid_steps[candidate_start:candidate_stop]
            diff = target_chunk[:, None, None, :] - curve_chunk[None, :, :, :]
            squared = np.sum(diff * diff, axis=3, dtype=np.float32)
            squared = np.where(valid_chunk[None, :, :], squared, np.float32(np.inf))
            best_squared = np.min(squared, axis=2)
            scores[pixel_start:pixel_stop, candidate_start:candidate_stop] = np.sqrt(
                best_squared,
                dtype=np.float32,
            )
    return scores

def _score_pixels_best_against_candidates(
    targets: np.ndarray,
    candidate_ids: np.ndarray,
    all_oklabs: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    """Return per-pixel best scores and stack ids over a candidate pool."""
    target_arr = np.asarray(targets, dtype=np.float32)
    ids = np.asarray(candidate_ids, dtype=np.int32)
    best_scores = np.full(target_arr.shape[0], np.float32(np.inf), dtype=np.float32)
    best_stack_ids = np.full(target_arr.shape[0], -1, dtype=np.int32)
    if target_arr.size == 0 or ids.size == 0:
        return best_scores, best_stack_ids

    curves = np.asarray(all_oklabs[ids], dtype=np.float32)
    valid_steps = np.isfinite(curves[..., 0])
    candidate_rows, _ = np.nonzero(valid_steps)
    if candidate_rows.size == 0:
        return best_scores, best_stack_ids

    points = curves[valid_steps].astype(np.float32, copy=False)
    distances, nearest = KDTree(points).query(target_arr, workers=-1)
    best_scores[:] = np.asarray(distances, dtype=np.float32)
    best_stack_ids[:] = ids[candidate_rows[np.asarray(nearest, dtype=np.int64)]]
    return best_scores.astype(np.float32, copy=False), best_stack_ids.astype(np.int32, copy=False)

def _score_pixels_against_stack_ids(
    zone_targets: np.ndarray,
    stack_ids: np.ndarray,
    all_oklabs: np.ndarray,
    *,
    max_broadcast_floats: int = _STAGE2_ZONE_SCORE_MAX_BROADCAST_FLOATS,
) -> np.ndarray:
    """Return per-pixel optimal-cap dE against one stack id per pixel."""
    target_arr = np.asarray(zone_targets, dtype=np.float32)
    ids = np.asarray(stack_ids, dtype=np.int32)
    if target_arr.size == 0 or ids.size == 0:
        return np.zeros(len(target_arr), dtype=np.float32)
    curves = np.asarray(all_oklabs[ids.astype(np.int32, copy=False)], dtype=np.float32)
    valid_steps = np.isfinite(curves[..., 0])
    pixel_count = int(target_arr.shape[0])
    cap_step_count = int(curves.shape[1])
    broadcast_floats = pixel_count * cap_step_count * 3
    if broadcast_floats <= int(max_broadcast_floats):
        diff = target_arr[:, None, :] - curves
        squared = np.sum(diff * diff, axis=2, dtype=np.float32)
        squared = np.where(valid_steps, squared, np.float32(np.inf))
        best_squared = np.min(squared, axis=1)
        return np.sqrt(best_squared, dtype=np.float32)

    scores = np.empty(pixel_count, dtype=np.float32)
    pixel_chunk = max(1, int(max_broadcast_floats) // max(1, cap_step_count * 3))
    for pixel_start in range(0, pixel_count, pixel_chunk):
        pixel_stop = min(pixel_count, pixel_start + pixel_chunk)
        diff = target_arr[pixel_start:pixel_stop, None, :] - curves[pixel_start:pixel_stop]
        squared = np.sum(diff * diff, axis=2, dtype=np.float32)
        squared = np.where(
            valid_steps[pixel_start:pixel_stop],
            squared,
            np.float32(np.inf),
        )
        best_squared = np.min(squared, axis=1)
        scores[pixel_start:pixel_stop] = np.sqrt(best_squared, dtype=np.float32)
    return scores

__all__ = (
    '_STAGE2_CONTINUITY_WEIGHT',
    '_STAGE2_RETAINING_WALL_WEIGHT',
    '_STAGE2_ZONE_LOCAL_WEIGHT_EXPONENT',
    '_STAGE2_ZONE_LOCAL_WEIGHT_MIN',
    '_STAGE2_ZONE_LOCAL_WEIGHT_MAX',
    '_STAGE2_ZONE_SCORE_MAX_BROADCAST_FLOATS',
    '_stage2_continuity_weight',
    '_build_zone_neighbors',
    '_build_zone_edge_indices',
    '_candidate_retaining_penalties',
    '_zone_local_cost_weights',
    '_selected_total_thicknesses',
    '_zone_objective_breakdown',
    '_mean_boundary_step_mm',
    '_edge_step_arrays_mm',
    '_score_zone_pixels_against_candidates',
    '_score_pixels_best_against_candidates',
    '_score_pixels_against_stack_ids',
)
