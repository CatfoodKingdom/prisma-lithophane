"""Low-chroma appearance stabilization for Stage 2 zone assignments."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .contracts import _ZoneCandidateSet
from .objective import (
    _STAGE2_CONTINUITY_WEIGHT,
    _STAGE2_RETAINING_WALL_WEIGHT,
    _build_zone_neighbors,
    _candidate_retaining_penalties,
    _selected_total_thicknesses,
)


_NEUTRAL_SOURCE_EDGE_DE_MAX = 0.018
_NEUTRAL_OUTPUT_DE_FLOOR = 0.012
_NEUTRAL_OUTPUT_DE_MARGIN = 0.004
_NEUTRAL_LOCAL_DE_BUDGET = 0.004
_NEUTRAL_APPEARANCE_WEIGHT = 12.0
_NEUTRAL_CHROMA_OVERSHOOT_FLOOR = 0.012
_NEUTRAL_CHROMA_OVERSHOOT_MARGIN = 0.006
_NEUTRAL_CHROMA_OVERSHOOT_WEIGHT = 80.0
_NEUTRAL_MAX_PASSES = 4


@dataclass(frozen=True)
class _NeutralFieldProtectionResult:
    """Assignment and diagnostics emitted by neutral-field protection."""

    selected_stack_ids: np.ndarray
    eligible_edge_count: int
    changed_zone_count: int
    candidate_evaluation_count: int
    mean_output_edge_de_before: float
    mean_output_edge_de_after: float
    mean_excess_edge_de_before: float
    mean_excess_edge_de_after: float
    mean_chroma_overshoot_before: float
    mean_chroma_overshoot_after: float


def _empty_result(selected_stack_ids: np.ndarray) -> _NeutralFieldProtectionResult:
    return _NeutralFieldProtectionResult(
        selected_stack_ids=np.asarray(selected_stack_ids, dtype=np.int32).copy(),
        eligible_edge_count=0,
        changed_zone_count=0,
        candidate_evaluation_count=0,
        mean_output_edge_de_before=0.0,
        mean_output_edge_de_after=0.0,
        mean_excess_edge_de_before=0.0,
        mean_excess_edge_de_after=0.0,
        mean_chroma_overshoot_before=0.0,
        mean_chroma_overshoot_after=0.0,
    )


def _zone_target_means(
    zone_flat_indices: tuple[np.ndarray, ...],
    targets: np.ndarray,
) -> np.ndarray:
    """Return one source/target OKLab mean per Stage 2 zone."""
    means = np.zeros((len(zone_flat_indices), 3), dtype=np.float32)
    for zone_id, flat_indices in enumerate(zone_flat_indices):
        if flat_indices.size:
            means[zone_id] = np.mean(
                targets[np.asarray(flat_indices, dtype=np.int64)],
                axis=0,
                dtype=np.float64,
            )
    return means


def _candidate_representative_oklabs(
    zone_target_means: np.ndarray,
    candidate_sets: tuple[_ZoneCandidateSet, ...],
    all_oklabs: np.ndarray,
) -> tuple[np.ndarray, ...]:
    """Approximate each recipe's zone appearance at its best cap height."""
    representatives: list[np.ndarray] = []
    for zone_id, candidate_set in enumerate(candidate_sets):
        candidate_ids = np.asarray(candidate_set.candidate_ids, dtype=np.int32)
        if candidate_ids.size == 0:
            representatives.append(np.zeros((0, 3), dtype=np.float32))
            continue
        curves = np.asarray(all_oklabs[candidate_ids], dtype=np.float32)
        valid = np.isfinite(curves[..., 0])
        diff = curves - zone_target_means[zone_id][None, None, :]
        squared = np.sum(diff * diff, axis=2, dtype=np.float32)
        squared = np.where(valid, squared, np.float32(np.inf))
        best_steps = np.argmin(squared, axis=1)
        rows = np.arange(candidate_ids.size, dtype=np.int64)
        reps = curves[rows, best_steps].astype(np.float32, copy=True)
        invalid_rows = ~np.any(valid, axis=1)
        if np.any(invalid_rows):
            reps[invalid_rows] = zone_target_means[zone_id]
        representatives.append(reps)
    return tuple(representatives)


def _eligible_neutral_edges(
    zone_target_means: np.ndarray,
    adjacency_edges: tuple[tuple[int, int], ...],
    *,
    neutral_chroma_cutoff: float,
) -> tuple[np.ndarray, np.ndarray]:
    """Return the conservative low-chroma edge gate and allowed output dE."""
    edge_count = len(adjacency_edges)
    eligible = np.zeros(edge_count, dtype=bool)
    allowed_output_de = np.zeros(edge_count, dtype=np.float32)
    if edge_count == 0:
        return eligible, allowed_output_de
    chroma = np.linalg.norm(zone_target_means[:, 1:3], axis=1)
    for edge_index, (lhs, rhs) in enumerate(adjacency_edges):
        lhs = int(lhs)
        rhs = int(rhs)
        source_de = float(
            np.linalg.norm(zone_target_means[lhs] - zone_target_means[rhs])
        )
        eligible[edge_index] = bool(
            max(float(chroma[lhs]), float(chroma[rhs]))
            <= float(neutral_chroma_cutoff)
            and source_de <= _NEUTRAL_SOURCE_EDGE_DE_MAX
        )
        allowed_output_de[edge_index] = np.float32(
            max(_NEUTRAL_OUTPUT_DE_FLOOR, source_de + _NEUTRAL_OUTPUT_DE_MARGIN)
        )
    return eligible, allowed_output_de


def _edge_appearance_metrics(
    selected_stack_ids: np.ndarray,
    candidate_representatives: tuple[np.ndarray, ...],
    adjacency_edges: tuple[tuple[int, int], ...],
    adjacency_edge_lengths_px: np.ndarray,
    eligible_edges: np.ndarray,
    allowed_output_de: np.ndarray,
) -> tuple[float, float]:
    """Return weighted mean output dE and excess dE on gated edges."""
    eligible_indices = np.flatnonzero(eligible_edges)
    if eligible_indices.size == 0:
        return 0.0, 0.0
    weights = np.asarray(adjacency_edge_lengths_px, dtype=np.float64)[
        eligible_indices
    ]
    weight_sum = float(np.sum(weights))
    if weight_sum <= 0.0:
        return 0.0, 0.0
    output_de = np.zeros(eligible_indices.size, dtype=np.float64)
    excess_de = np.zeros(eligible_indices.size, dtype=np.float64)
    for position, edge_index in enumerate(eligible_indices.tolist()):
        lhs, rhs = adjacency_edges[int(edge_index)]
        lhs_color = candidate_representatives[int(lhs)][
            int(selected_stack_ids[int(lhs)])
        ]
        rhs_color = candidate_representatives[int(rhs)][
            int(selected_stack_ids[int(rhs)])
        ]
        edge_de = float(np.linalg.norm(lhs_color - rhs_color))
        output_de[position] = edge_de
        excess_de[position] = max(
            0.0,
            edge_de - float(allowed_output_de[int(edge_index)]),
        )
    return (
        float(np.sum(weights * output_de) / weight_sum),
        float(np.sum(weights * excess_de) / weight_sum),
    )


def _mean_chroma_overshoot(
    selected_stack_ids: np.ndarray,
    zone_target_means: np.ndarray,
    candidate_representatives: tuple[np.ndarray, ...],
    *,
    neutral_chroma_cutoff: float,
) -> float:
    """Return mean candidate chroma above the neutral target allowance."""
    overshoots: list[float] = []
    target_chroma = np.linalg.norm(zone_target_means[:, 1:3], axis=1)
    for zone_id, representatives in enumerate(candidate_representatives):
        if (
            representatives.size == 0
            or float(target_chroma[zone_id]) > float(neutral_chroma_cutoff)
        ):
            continue
        candidate_chroma = float(
            np.linalg.norm(
                representatives[int(selected_stack_ids[zone_id]), 1:3]
            )
        )
        allowed_chroma = max(
            _NEUTRAL_CHROMA_OVERSHOOT_FLOOR,
            float(target_chroma[zone_id]) + _NEUTRAL_CHROMA_OVERSHOOT_MARGIN,
        )
        overshoots.append(max(0.0, candidate_chroma - allowed_chroma))
    return float(np.mean(overshoots)) if overshoots else 0.0


def _apply_neutral_field_protection(
    *,
    selected_stack_ids: np.ndarray,
    candidate_sets: tuple[_ZoneCandidateSet, ...],
    zone_flat_indices: tuple[np.ndarray, ...],
    targets: np.ndarray,
    all_oklabs: np.ndarray,
    adjacency_edges: tuple[tuple[int, int], ...],
    adjacency_edge_lengths_px: np.ndarray,
    neutral_chroma_cutoff: float,
    local_cost_weights: np.ndarray | None = None,
    continuity_weight: float = _STAGE2_CONTINUITY_WEIGHT,
    retaining_wall_weight: float = _STAGE2_RETAINING_WALL_WEIGHT,
    appearance_weight: float = _NEUTRAL_APPEARANCE_WEIGHT,
    chroma_overshoot_weight: float = _NEUTRAL_CHROMA_OVERSHOOT_WEIGHT,
    local_de_budget: float = _NEUTRAL_LOCAL_DE_BUDGET,
    max_passes: int = _NEUTRAL_MAX_PASSES,
) -> _NeutralFieldProtectionResult:
    """Reduce false hue seams without materially worsening local color fit."""
    baseline = np.asarray(selected_stack_ids, dtype=np.int32)
    selected = baseline.copy()
    zone_count = len(candidate_sets)
    resolved_chroma_cutoff = max(0.0, float(neutral_chroma_cutoff))
    if zone_count == 0 or len(adjacency_edges) == 0:
        return _empty_result(selected)

    zone_means = _zone_target_means(zone_flat_indices, targets)
    representatives = _candidate_representative_oklabs(
        zone_means,
        candidate_sets,
        all_oklabs,
    )
    eligible_edges, allowed_output_de = _eligible_neutral_edges(
        zone_means,
        adjacency_edges,
        neutral_chroma_cutoff=resolved_chroma_cutoff,
    )
    eligible_edge_count = int(np.count_nonzero(eligible_edges))
    before_output_de, before_excess_de = _edge_appearance_metrics(
        selected,
        representatives,
        adjacency_edges,
        adjacency_edge_lengths_px,
        eligible_edges,
        allowed_output_de,
    )
    before_chroma_overshoot = _mean_chroma_overshoot(
        selected,
        zone_means,
        representatives,
        neutral_chroma_cutoff=resolved_chroma_cutoff,
    )
    if eligible_edge_count == 0:
        return _NeutralFieldProtectionResult(
            selected_stack_ids=selected,
            eligible_edge_count=0,
            changed_zone_count=0,
            candidate_evaluation_count=0,
            mean_output_edge_de_before=before_output_de,
            mean_output_edge_de_after=before_output_de,
            mean_excess_edge_de_before=before_excess_de,
            mean_excess_edge_de_after=before_excess_de,
            mean_chroma_overshoot_before=before_chroma_overshoot,
            mean_chroma_overshoot_after=before_chroma_overshoot,
        )

    neighbors = _build_zone_neighbors(
        zone_count,
        adjacency_edges,
        np.asarray(adjacency_edge_lengths_px, dtype=np.float32),
    )
    edge_lookup: dict[tuple[int, int], int] = {}
    for edge_index, (lhs, rhs) in enumerate(adjacency_edges):
        edge_lookup[(min(int(lhs), int(rhs)), max(int(lhs), int(rhs)))] = int(
            edge_index
        )
    local_weights = (
        np.asarray(local_cost_weights, dtype=np.float32)
        if local_cost_weights is not None
        else np.ones(zone_count, dtype=np.float32)
    )
    selected_totals = _selected_total_thicknesses(selected, candidate_sets)
    retaining_penalties = _candidate_retaining_penalties(candidate_sets)
    local_score_ceilings = np.zeros(zone_count, dtype=np.float32)
    for zone_id, candidate_set in enumerate(candidate_sets):
        if candidate_set.local_scores.size == 0:
            continue
        baseline_score = float(
            candidate_set.local_scores[int(baseline[zone_id])]
        )
        local_score_ceilings[zone_id] = np.float32(
            baseline_score + max(0.0, float(local_de_budget))
        )

    evaluation_count = 0
    for _ in range(max(1, int(max_passes))):
        changed = False
        for zone_id, candidate_set in enumerate(candidate_sets):
            candidate_count = int(candidate_set.local_scores.size)
            if candidate_count <= 1:
                continue
            gated_neighbors: list[tuple[int, float, int]] = []
            for neighbor_zone_id, edge_weight in neighbors[zone_id]:
                edge_index = edge_lookup[
                    (
                        min(zone_id, int(neighbor_zone_id)),
                        max(zone_id, int(neighbor_zone_id)),
                    )
                ]
                if eligible_edges[edge_index]:
                    gated_neighbors.append(
                        (int(neighbor_zone_id), float(edge_weight), edge_index)
                    )
            if not gated_neighbors:
                continue

            evaluation_count += candidate_count
            candidate_totals = np.asarray(
                candidate_set.total_thickness_mm,
                dtype=np.float64,
            )
            total_costs = (
                float(local_weights[zone_id])
                * np.asarray(candidate_set.local_scores, dtype=np.float64)
                + float(retaining_wall_weight)
                * np.asarray(retaining_penalties[zone_id], dtype=np.float64)
            )

            all_neighbors = neighbors[zone_id]
            if all_neighbors:
                neighbor_weight_sum = float(
                    sum(float(weight) for _, weight in all_neighbors)
                )
                thickness_cost = np.zeros(candidate_count, dtype=np.float64)
                for neighbor_zone_id, edge_weight in all_neighbors:
                    thickness_cost += float(edge_weight) * np.square(
                        candidate_totals
                        - float(selected_totals[int(neighbor_zone_id)])
                    )
                if neighbor_weight_sum > 0.0:
                    thickness_cost /= neighbor_weight_sum
                total_costs += float(continuity_weight) * thickness_cost

            appearance_cost = np.zeros(candidate_count, dtype=np.float64)
            appearance_weight_sum = float(
                sum(edge_weight for _, edge_weight, _ in gated_neighbors)
            )
            for neighbor_zone_id, edge_weight, edge_index in gated_neighbors:
                neighbor_color = representatives[neighbor_zone_id][
                    int(selected[neighbor_zone_id])
                ]
                output_de = np.linalg.norm(
                    representatives[zone_id] - neighbor_color[None, :],
                    axis=1,
                )
                excess_de = np.maximum(
                    0.0,
                    output_de - float(allowed_output_de[edge_index]),
                )
                appearance_cost += float(edge_weight) * np.square(excess_de)
            if appearance_weight_sum > 0.0:
                appearance_cost /= appearance_weight_sum
            total_costs += float(appearance_weight) * appearance_cost

            target_chroma = float(np.linalg.norm(zone_means[zone_id, 1:3]))
            if target_chroma <= resolved_chroma_cutoff:
                allowed_chroma = max(
                    _NEUTRAL_CHROMA_OVERSHOOT_FLOOR,
                    target_chroma + _NEUTRAL_CHROMA_OVERSHOOT_MARGIN,
                )
                candidate_chroma = np.linalg.norm(
                    representatives[zone_id][:, 1:3],
                    axis=1,
                )
                chroma_overshoot = np.maximum(
                    0.0,
                    candidate_chroma - allowed_chroma,
                )
                total_costs += float(chroma_overshoot_weight) * np.square(
                    chroma_overshoot
                )

            local_scores = np.asarray(candidate_set.local_scores)
            total_costs = np.where(
                local_scores <= local_score_ceilings[zone_id] + 1e-9,
                total_costs,
                np.inf,
            )
            current_index = int(selected[zone_id])
            best_index = int(np.argmin(total_costs))
            if (
                best_index != current_index
                and float(total_costs[best_index]) + 1e-12
                < float(total_costs[current_index])
            ):
                selected[zone_id] = best_index
                selected_totals[zone_id] = float(
                    candidate_set.total_thickness_mm[best_index]
                )
                changed = True
        if not changed:
            break

    after_output_de, after_excess_de = _edge_appearance_metrics(
        selected,
        representatives,
        adjacency_edges,
        adjacency_edge_lengths_px,
        eligible_edges,
        allowed_output_de,
    )
    after_chroma_overshoot = _mean_chroma_overshoot(
        selected,
        zone_means,
        representatives,
        neutral_chroma_cutoff=resolved_chroma_cutoff,
    )
    return _NeutralFieldProtectionResult(
        selected_stack_ids=selected,
        eligible_edge_count=eligible_edge_count,
        changed_zone_count=int(np.count_nonzero(selected != baseline)),
        candidate_evaluation_count=int(evaluation_count),
        mean_output_edge_de_before=before_output_de,
        mean_output_edge_de_after=after_output_de,
        mean_excess_edge_de_before=before_excess_de,
        mean_excess_edge_de_after=after_excess_de,
        mean_chroma_overshoot_before=before_chroma_overshoot,
        mean_chroma_overshoot_after=after_chroma_overshoot,
    )


__all__ = (
    "_NeutralFieldProtectionResult",
    "_apply_neutral_field_protection",
)
