"""Stage 2 candidate enumeration and frontier management."""
from __future__ import annotations


import numpy as np

from lut import query_luts_batch
from ...staged_solver_helpers import _score_candidates_batch


from ..recipe_pressure import _STAGE2_PRESSURE_ACTIVE_THRESHOLD

from .contracts import _ZoneCandidateSet
from .objective import (
    _build_zone_neighbors,
    _score_zone_pixels_against_candidates,
)

_STAGE2_FRONTIER_SIZE = 4

_STAGE2_FRONTIER_OPTICAL_RESCUE_MAX_EXTRA = 2

_STAGE2_FRONTIER_OPTICAL_RESCUE_RANK_BUDGET = 6

_STAGE2_FRONTIER_OPTICAL_RESCUE_MIN_SCORE_GAP = 0.002

_STAGE2_FRONTIER_PRESSURE_RESCUE_MIN_PIXELS = 4

def _query_stage2_pixel_stacks(state) -> tuple[dict[str, np.ndarray], np.ndarray, np.ndarray]:
    """Query per-pixel visible stack candidates on the solve lattice."""
    targets = state.solve_target_oklab
    if targets is None:
        raise RuntimeError("Staged Stage 2 requires solve_target_oklab from the runner.")
    thickness_result, de_flat = query_luts_batch(state.luts, targets)
    gamut_mask = (de_flat > float(state.config.de_threshold)).astype(np.float32)
    return thickness_result, de_flat.astype(np.float32), gamut_mask

def _enumerate_zone_candidates(
    *,
    zone_flat_indices: tuple[np.ndarray, ...],
    targets: np.ndarray,
    pixel_stack_ids: np.ndarray,
    unique_stack_dicts: dict[int, dict[str, float]],
    all_oklabs: np.ndarray,
) -> tuple[_ZoneCandidateSet, ...]:
    """Enumerate candidate visible recipes and local scores per zone."""
    zone_count = len(zone_flat_indices)
    candidates: list[_ZoneCandidateSet] = []

    for zone_id, indices in enumerate(zone_flat_indices):
        if indices.size == 0:
            candidates.append(
                _ZoneCandidateSet(
                    candidate_ids=np.zeros(0, dtype=np.int32),
                    local_scores=np.zeros(0, dtype=np.float32),
                    total_thickness_mm=np.zeros(0, dtype=np.float32),
                )
            )
            continue
        candidate_ids = np.unique(pixel_stack_ids[indices])
        if candidate_ids.size == 1:
            local_scores = np.array([0.0], dtype=np.float32)
        else:
            zone_targets = targets[indices]
            local_scores = _score_candidates_batch(zone_targets, candidate_ids, all_oklabs).astype(
                np.float32,
                copy=False,
            )
        totals = np.array(
            [
                sum(float(thickness) for thickness in unique_stack_dicts[int(stack_id)].values())
                for stack_id in candidate_ids
            ],
            dtype=np.float32,
        )
        candidates.append(
            _ZoneCandidateSet(
                candidate_ids=candidate_ids.astype(np.int32, copy=False),
                local_scores=local_scores,
                total_thickness_mm=totals,
            )
        )
    return tuple(candidates)

def _augment_zone_candidates_with_neighbor_local_bests(
    *,
    zone_count: int,
    zone_flat_indices: tuple[np.ndarray, ...],
    target_oklab_var_by_zone: np.ndarray,
    adjacency_edges: tuple[tuple[int, int], ...],
    adjacency_edge_lengths_px: np.ndarray,
    candidate_sets: tuple[_ZoneCandidateSet, ...],
    targets: np.ndarray,
    unique_stack_dicts: dict[int, dict[str, float]],
    all_oklabs: np.ndarray,
) -> tuple[tuple[_ZoneCandidateSet, ...], int, int]:
    """Augment zone candidates with strong neighboring local-best stacks."""
    zone_count = int(zone_count)
    if zone_count == 0 or len(adjacency_edges) == 0:
        return candidate_sets, 0, 0

    variance_norm = np.sqrt(np.sum(target_oklab_var_by_zone, axis=1))
    positive_variance = variance_norm[variance_norm > 1e-9]
    variance_threshold = float(np.median(positive_variance)) if positive_variance.size else float("inf")
    neighbor_lists = _build_zone_neighbors(
        zone_count,
        adjacency_edges,
        adjacency_edge_lengths_px.astype(np.float32, copy=False),
    )
    local_best_stack_ids = np.full(zone_count, -1, dtype=np.int32)
    for zone_id, candidate_set in enumerate(candidate_sets):
        if candidate_set.local_scores.size == 0:
            continue
        local_best_stack_ids[zone_id] = int(
            candidate_set.candidate_ids[int(np.argmin(candidate_set.local_scores))]
        )

    augmented: list[_ZoneCandidateSet] = []
    augmented_zone_hits = 0
    augmented_candidate_count = 0

    for zone_id, candidate_set in enumerate(candidate_sets):
        if candidate_set.local_scores.size == 0:
            augmented.append(candidate_set)
            continue

        ordered_neighbors = sorted(
            neighbor_lists[zone_id],
            key=lambda item: (-float(item[1]), int(item[0])),
        )
        if not ordered_neighbors:
            augmented.append(candidate_set)
            continue

        extra_budget = 1
        if (
            variance_norm.size > zone_id
            and float(variance_norm[zone_id]) >= variance_threshold
            and len(ordered_neighbors) > 1
        ):
            extra_budget = 2

        existing_ids = set(candidate_set.candidate_ids.tolist())
        borrowed_ids: list[int] = []
        for neighbor_zone_id, _ in ordered_neighbors:
            stack_id = int(local_best_stack_ids[int(neighbor_zone_id)])
            if stack_id < 0 or stack_id in existing_ids or stack_id in borrowed_ids:
                continue
            borrowed_ids.append(stack_id)
            if len(borrowed_ids) >= extra_budget:
                break

        if not borrowed_ids:
            augmented.append(candidate_set)
            continue

        zone_targets = targets[zone_flat_indices[zone_id]]
        borrowed_array = np.array(borrowed_ids, dtype=np.int32)
        borrowed_scores = _score_candidates_batch(zone_targets, borrowed_array, all_oklabs).astype(
            np.float32,
            copy=False,
        )
        borrowed_totals = np.array(
            [
                sum(float(thickness) for thickness in unique_stack_dicts[int(stack_id)].values())
                for stack_id in borrowed_ids
            ],
            dtype=np.float32,
        )
        augmented.append(
            _ZoneCandidateSet(
                candidate_ids=np.concatenate(
                    [candidate_set.candidate_ids, borrowed_array],
                    axis=0,
                ).astype(np.int32, copy=False),
                local_scores=np.concatenate(
                    [candidate_set.local_scores, borrowed_scores],
                    axis=0,
                ).astype(np.float32, copy=False),
                total_thickness_mm=np.concatenate(
                    [candidate_set.total_thickness_mm, borrowed_totals],
                    axis=0,
                ).astype(np.float32, copy=False),
            )
        )
        augmented_zone_hits += 1
        augmented_candidate_count += len(borrowed_ids)

    return tuple(augmented), augmented_zone_hits, augmented_candidate_count

def _prune_zone_candidate_frontiers(
    candidate_sets: tuple[_ZoneCandidateSet, ...],
    *,
    adjacency_edges: tuple[tuple[int, int], ...] = (),
    adjacency_edge_lengths_px: np.ndarray | None = None,
    frontier_size: int = _STAGE2_FRONTIER_SIZE,
) -> tuple[tuple[_ZoneCandidateSet, ...], int]:
    """Trim each zone candidate set while preserving seam-useful alternatives."""
    pruned: list[_ZoneCandidateSet] = []
    neighbor_match_zone_hits = 0
    limit = max(1, int(frontier_size))
    neighbor_lists: list[list[tuple[int, float]]] | None = None
    neighbor_seed_totals_mm: np.ndarray | None = None
    if len(adjacency_edges) > 0:
        edge_lengths = (
            np.asarray(adjacency_edge_lengths_px, dtype=np.float32)
            if adjacency_edge_lengths_px is not None
            else np.ones(len(adjacency_edges), dtype=np.float32)
        )
        neighbor_lists = _build_zone_neighbors(
            len(candidate_sets),
            adjacency_edges,
            edge_lengths,
        )
        neighbor_seed_totals_mm = np.full(len(candidate_sets), np.nan, dtype=np.float32)
        for zone_id, candidate_set in enumerate(candidate_sets):
            if candidate_set.local_scores.size == 0 or candidate_set.total_thickness_mm.size == 0:
                continue
            local_best_index = int(np.argmin(candidate_set.local_scores))
            neighbor_seed_totals_mm[zone_id] = float(
                candidate_set.total_thickness_mm[local_best_index]
            )

    for zone_id, candidate_set in enumerate(candidate_sets):
        if candidate_set.local_scores.size <= limit:
            pruned.append(candidate_set)
            continue
        selected: list[int] = []
        neighbor_match_added = False

        def add_index(candidate_index: int) -> None:
            idx = int(candidate_index)
            if idx not in selected:
                selected.append(idx)

        local_order = np.argsort(candidate_set.local_scores, kind="stable")
        add_index(local_order[0])

        if (
            neighbor_lists is not None
            and neighbor_seed_totals_mm is not None
            and zone_id < len(neighbor_lists)
            and len(selected) < limit
        ):
            ordered_neighbors = sorted(
                neighbor_lists[zone_id],
                key=lambda item: (-float(item[1]), int(item[0])),
            )
            for neighbor_zone_id, _ in ordered_neighbors:
                target_mm = float(neighbor_seed_totals_mm[int(neighbor_zone_id)])
                if not np.isfinite(target_mm):
                    continue
                match_index = min(
                    range(candidate_set.total_thickness_mm.size),
                    key=lambda idx: (
                        abs(float(candidate_set.total_thickness_mm[idx]) - target_mm),
                        float(candidate_set.local_scores[idx]),
                    ),
                )
                if int(match_index) not in selected and int(match_index) != int(local_order[0]):
                    neighbor_match_added = True
                add_index(match_index)
                if len(selected) >= limit:
                    break

        if len(selected) < limit:
            thickness_order = np.argsort(candidate_set.total_thickness_mm, kind="stable")
            anchor_count = min(limit, int(thickness_order.size))
            if anchor_count > 0:
                anchor_positions = np.linspace(
                    0,
                    int(thickness_order.size) - 1,
                    num=anchor_count,
                    dtype=np.int32,
                )
                for anchor_pos in anchor_positions:
                    add_index(thickness_order[int(anchor_pos)])

        if len(selected) < limit:
            for candidate_index in local_order:
                add_index(candidate_index)
                if len(selected) >= limit:
                    break

        order = np.array(selected[:limit], dtype=np.int32)
        pruned.append(
            _ZoneCandidateSet(
                candidate_ids=candidate_set.candidate_ids[order].astype(np.int32, copy=False),
                local_scores=candidate_set.local_scores[order].astype(np.float32, copy=False),
                total_thickness_mm=candidate_set.total_thickness_mm[order].astype(np.float32, copy=False),
            )
        )
        if neighbor_match_added:
            neighbor_match_zone_hits += 1
    return tuple(pruned), neighbor_match_zone_hits

def _rescue_stage2_optical_frontier_candidates(
    *,
    preprune_candidate_sets: tuple[_ZoneCandidateSet, ...],
    pruned_candidate_sets: tuple[_ZoneCandidateSet, ...],
    zone_flat_indices: tuple[np.ndarray, ...] | None = None,
    targets: np.ndarray | None = None,
    all_oklabs: np.ndarray | None = None,
    frontier_size: int = _STAGE2_FRONTIER_SIZE,
    max_extra_candidates: int = _STAGE2_FRONTIER_OPTICAL_RESCUE_MAX_EXTRA,
    rank_budget: int = _STAGE2_FRONTIER_OPTICAL_RESCUE_RANK_BUDGET,
    min_score_gap: float = _STAGE2_FRONTIER_OPTICAL_RESCUE_MIN_SCORE_GAP,
) -> tuple[tuple[_ZoneCandidateSet, ...], int, int, int]:
    """Append a few strong optical candidates that frontier pruning removed."""
    limit = max(1, int(frontier_size)) + max(0, int(max_extra_candidates))
    rank_limit = max(1, int(rank_budget))
    gap = max(0.0, float(min_score_gap))
    rescued: list[_ZoneCandidateSet] = []
    zone_hits = 0
    candidate_count = 0
    pressure_candidate_count = 0
    can_score_pressure = (
        zone_flat_indices is not None
        and targets is not None
        and all_oklabs is not None
    )

    for zone_id, (preprune_set, pruned_set) in enumerate(
        zip(preprune_candidate_sets, pruned_candidate_sets, strict=True)
    ):
        if (
            preprune_set.candidate_ids.size == 0
            or pruned_set.candidate_ids.size == 0
            or pruned_set.candidate_ids.size >= limit
        ):
            rescued.append(pruned_set)
            continue

        selected_indices: list[int] = []
        existing_ids = {int(stack_id) for stack_id in pruned_set.candidate_ids.tolist()}
        candidate_id_to_preprune_index = {
            int(stack_id): int(index)
            for index, stack_id in enumerate(preprune_set.candidate_ids.tolist())
        }
        for stack_id in pruned_set.candidate_ids.tolist():
            preprune_index = candidate_id_to_preprune_index.get(int(stack_id))
            if preprune_index is not None:
                selected_indices.append(preprune_index)

        if not selected_indices:
            rescued.append(pruned_set)
            continue

        additions: list[int] = []
        if can_score_pressure and zone_id < len(zone_flat_indices):
            indices = zone_flat_indices[zone_id]
            if indices.size:
                zone_targets = np.asarray(
                    targets[indices.astype(np.int64, copy=False)],
                    dtype=np.float32,
                )
                preprune_scores = _score_zone_pixels_against_candidates(
                    zone_targets,
                    preprune_set.candidate_ids.astype(np.int32, copy=False),
                    all_oklabs,
                )
                pruned_scores = _score_zone_pixels_against_candidates(
                    zone_targets,
                    pruned_set.candidate_ids.astype(np.int32, copy=False),
                    all_oklabs,
                )
                frontier_best = np.min(pruned_scores, axis=1) if pruned_scores.size else None
                if frontier_best is not None:
                    pressure_candidates: list[tuple[float, int, float, int]] = []
                    for candidate_index, stack_id in enumerate(preprune_set.candidate_ids.tolist()):
                        if int(stack_id) in existing_ids:
                            continue
                        gains = (
                            frontier_best - preprune_scores[:, int(candidate_index)]
                        ).astype(np.float32, copy=False)
                        active = gains > np.float32(_STAGE2_PRESSURE_ACTIVE_THRESHOLD)
                        active_pixels = int(np.count_nonzero(active))
                        if active_pixels < int(_STAGE2_FRONTIER_PRESSURE_RESCUE_MIN_PIXELS):
                            continue
                        active_gains = gains[active]
                        total_gain = float(np.sum(active_gains))
                        mean_gain = float(np.mean(active_gains)) if active_gains.size else 0.0
                        pressure_candidates.append(
                            (
                                -total_gain,
                                -active_pixels,
                                -mean_gain,
                                int(candidate_index),
                            )
                        )
                    pressure_candidates.sort()
                    for _, _, _, candidate_index in pressure_candidates:
                        if len(selected_indices) + len(additions) >= limit:
                            break
                        additions.append(int(candidate_index))
                        existing_ids.add(int(preprune_set.candidate_ids[int(candidate_index)]))
                        pressure_candidate_count += 1

        worst_selected_score = float(np.max(preprune_set.local_scores[selected_indices]))
        local_order = np.argsort(preprune_set.local_scores, kind="stable")
        for candidate_index in local_order[:rank_limit]:
            idx = int(candidate_index)
            stack_id = int(preprune_set.candidate_ids[idx])
            if stack_id in existing_ids:
                continue
            if len(selected_indices) + len(additions) >= limit:
                break
            candidate_score = float(preprune_set.local_scores[idx])
            if candidate_score + gap > worst_selected_score:
                continue
            additions.append(idx)
            existing_ids.add(stack_id)

        if not additions:
            rescued.append(pruned_set)
            continue

        order = np.array(selected_indices + additions, dtype=np.int32)
        rescued.append(
            _ZoneCandidateSet(
                candidate_ids=preprune_set.candidate_ids[order].astype(np.int32, copy=False),
                local_scores=preprune_set.local_scores[order].astype(np.float32, copy=False),
                total_thickness_mm=preprune_set.total_thickness_mm[order].astype(np.float32, copy=False),
            )
        )
        zone_hits += 1
        candidate_count += len(additions)

    return tuple(rescued), int(zone_hits), int(candidate_count), int(pressure_candidate_count)

__all__ = (
    '_STAGE2_FRONTIER_SIZE',
    '_STAGE2_FRONTIER_OPTICAL_RESCUE_MAX_EXTRA',
    '_STAGE2_FRONTIER_OPTICAL_RESCUE_RANK_BUDGET',
    '_STAGE2_FRONTIER_OPTICAL_RESCUE_MIN_SCORE_GAP',
    '_STAGE2_FRONTIER_PRESSURE_RESCUE_MIN_PIXELS',
    '_query_stage2_pixel_stacks',
    '_enumerate_zone_candidates',
    '_augment_zone_candidates_with_neighbor_local_bests',
    '_prune_zone_candidate_frontiers',
    '_rescue_stage2_optical_frontier_candidates',
)
