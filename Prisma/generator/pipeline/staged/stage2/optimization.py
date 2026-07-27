"""Stage 2 zone-assignment optimization."""
from __future__ import annotations

import time

import numpy as np


from ...staged_artifacts import (
    Stage2EdgeSeamSummary,
    Stage2ObjectiveSummary,
    Stage2ZoneObjectiveBreakdown,
)


from .contracts import (
    _ZoneCandidateSet,
    _ZoneRecipeOptimizationResult,
    _BeamSeedResult,
    _BeamSearchState,
)
from .objective import (
    _STAGE2_CONTINUITY_WEIGHT,
    _STAGE2_RETAINING_WALL_WEIGHT,
    _build_zone_neighbors,
    _build_zone_edge_indices,
    _candidate_retaining_penalties,
    _selected_total_thicknesses,
    _zone_objective_breakdown,
    _mean_boundary_step_mm,
    _edge_step_arrays_mm,
)

_STAGE2_MAX_COORD_DESCENT_PASSES = 4

_STAGE2_BEAM_WIDTH = 12

_STAGE2_BEAM_CHECKPOINT_INTERVAL = 64

_STAGE2_PAIR_REPAIR_PASSES = 2

_STAGE2_PAIR_REPAIR_EDGE_PROBE_COUNT = 8

def _zone_assignment_order(
    *,
    zone_count: int,
    adjacency_edges: tuple[tuple[int, int], ...],
    zone_pixel_counts: np.ndarray,
    target_oklab_var_by_zone: np.ndarray,
) -> np.ndarray:
    """Order zones for beam expansion by connectivity, variance, then size."""
    degrees = np.zeros(zone_count, dtype=np.int32)
    for lhs, rhs in adjacency_edges:
        degrees[int(lhs)] += 1
        degrees[int(rhs)] += 1
    variance_norm = np.sqrt(np.sum(target_oklab_var_by_zone, axis=1))
    return np.lexsort(
        (
            -zone_pixel_counts.astype(np.float64),
            -variance_norm.astype(np.float64),
            -degrees.astype(np.float64),
        )
    )

def _global_assignment_cost(
    selected_stack_ids: np.ndarray,
    candidate_sets: tuple[_ZoneCandidateSet, ...],
    adjacency_edges: tuple[tuple[int, int], ...],
    adjacency_edge_lengths_px: np.ndarray,
    *,
    continuity_weight: float,
    retaining_wall_weight: float,
    local_cost_weights: np.ndarray | None = None,
) -> float:
    """Evaluate one full assignment under the current Stage 2 objective."""
    local_sum = 0.0
    retaining_sum = 0.0
    local_weights = (
        np.asarray(local_cost_weights, dtype=np.float32)
        if local_cost_weights is not None
        else np.ones(len(candidate_sets), dtype=np.float32)
    )
    for zone_id, candidate_set in enumerate(candidate_sets):
        if candidate_set.local_scores.size == 0:
            continue
        candidate_index = int(selected_stack_ids[zone_id])
        local_sum += float(local_weights[zone_id]) * float(candidate_set.local_scores[candidate_index])
        min_total = float(np.min(candidate_set.total_thickness_mm)) if candidate_set.total_thickness_mm.size else 0.0
        total_mm = float(candidate_set.total_thickness_mm[candidate_index]) if candidate_set.total_thickness_mm.size else 0.0
        retaining_sum += max(0.0, total_mm - min_total)
    total_edge_weight = float(np.sum(adjacency_edge_lengths_px))
    edge_term = 0.0
    if total_edge_weight > 0.0:
        for edge_index, (lhs, rhs) in enumerate(adjacency_edges):
            lhs_choice = int(selected_stack_ids[int(lhs)])
            rhs_choice = int(selected_stack_ids[int(rhs)])
            lhs_set = candidate_sets[int(lhs)]
            rhs_set = candidate_sets[int(rhs)]
            lhs_total = float(lhs_set.total_thickness_mm[lhs_choice]) if lhs_set.total_thickness_mm.size else 0.0
            rhs_total = float(rhs_set.total_thickness_mm[rhs_choice]) if rhs_set.total_thickness_mm.size else 0.0
            step_mm = abs(lhs_total - rhs_total)
            edge_term += float(adjacency_edge_lengths_px[edge_index]) * (step_mm ** 2)
        edge_term /= total_edge_weight
    return local_sum + float(retaining_wall_weight) * retaining_sum + float(continuity_weight) * edge_term

def _beam_state_candidate_index(
    state: _BeamSearchState,
    zone_id: int,
) -> int:
    """Read one assigned candidate from a bounded persistent beam path."""

    current: _BeamSearchState | None = state
    while current is not None:
        if current.delta_choices is not None:
            candidate_index = current.delta_choices.get(int(zone_id))
            if candidate_index is not None:
                return int(candidate_index)
        if current.checkpoint_selected is not None:
            return int(current.checkpoint_selected[int(zone_id)])
        if int(current.zone_id) == int(zone_id):
            return int(current.candidate_index)
        current = current.parent
    return -1

def _materialize_beam_state(
    state: _BeamSearchState,
    zone_count: int,
) -> np.ndarray:
    """Materialize a persistent beam path into the historical dense form."""

    if state.checkpoint_selected is not None and state.delta_choices is not None:
        selected = state.checkpoint_selected.astype(np.int32, copy=True)
        for zone_id, candidate_index in state.delta_choices.items():
            selected[int(zone_id)] = int(candidate_index)
        return selected

    pending: list[_BeamSearchState] = []
    current: _BeamSearchState | None = state
    while current is not None and current.checkpoint_selected is None:
        pending.append(current)
        current = current.parent
    if current is None:
        selected = np.full(int(zone_count), -1, dtype=np.int32)
    else:
        selected = current.checkpoint_selected.astype(np.int32, copy=True)
    for entry in reversed(pending):
        if int(entry.zone_id) >= 0:
            selected[int(entry.zone_id)] = int(entry.candidate_index)
    return selected

def _checkpoint_beam_state(
    state: _BeamSearchState,
    zone_count: int,
    *,
    force_dense_checkpoint: bool,
) -> _BeamSearchState:
    """Normalize a surviving child into checkpoint + small delta storage."""

    parent = state.parent
    if parent is None or parent.checkpoint_selected is None:
        selected = _materialize_beam_state(state, zone_count)
        return _BeamSearchState(
            score=float(state.score),
            local_sum=float(state.local_sum),
            retaining_sum=float(state.retaining_sum),
            edge_sum=float(state.edge_sum),
            checkpoint_selected=selected,
            delta_choices={},
        )
    delta_choices = dict(parent.delta_choices or {})
    delta_choices[int(state.zone_id)] = int(state.candidate_index)
    checkpoint_selected = parent.checkpoint_selected
    if force_dense_checkpoint:
        checkpoint_selected = checkpoint_selected.astype(np.int32, copy=True)
        for zone_id, candidate_index in delta_choices.items():
            checkpoint_selected[int(zone_id)] = int(candidate_index)
        delta_choices = {}
    return _BeamSearchState(
        score=float(state.score),
        local_sum=float(state.local_sum),
        retaining_sum=float(state.retaining_sum),
        edge_sum=float(state.edge_sum),
        checkpoint_selected=checkpoint_selected,
        delta_choices=delta_choices,
    )

def _select_completed_beam_assignment(
    beam: list[_BeamSearchState],
    *,
    zone_count: int,
    candidate_sets: tuple[_ZoneCandidateSet, ...],
    adjacency_edges: tuple[tuple[int, int], ...],
    adjacency_edge_lengths_px: np.ndarray,
    continuity_weight: float,
    retaining_wall_weight: float,
    local_cost_weights: np.ndarray,
) -> np.ndarray:
    """Select the historical global-cost winner with minimal rescanning.

    With every zone assignable, each persistent state's incremental score is
    the same non-negative objective summed in assignment order.  A conservative
    floating-point error interval identifies whether one state is unambiguous.
    Exact global rescans remain the fallback for ties, near-ties, non-finite
    scores, and the unusual empty-candidate case where incremental edge terms
    do not cover the same set as the historical global evaluator.
    """

    if not beam:
        raise ValueError("completed beam cannot be empty")
    scores = np.asarray([state.score for state in beam], dtype=np.float64)
    all_assignable = all(
        candidate_set.local_scores.size > 0 for candidate_set in candidate_sets
    )
    nonnegative_objective = (
        float(continuity_weight) >= 0.0
        and float(retaining_wall_weight) >= 0.0
        and np.all(np.asarray(local_cost_weights, dtype=np.float64) >= 0.0)
        and np.all(np.asarray(adjacency_edge_lengths_px) >= 0)
        and all(
            np.all(np.asarray(candidate_set.local_scores, dtype=np.float64) >= 0.0)
            for candidate_set in candidate_sets
        )
    )
    if all_assignable and nonnegative_objective and np.all(np.isfinite(scores)):
        best_score = float(np.min(scores))
        score_scale = max(1.0, float(np.max(np.abs(scores))))
        operation_count = max(1, int(zone_count) + len(adjacency_edges) + 8)
        roundoff_bound = (
            16.0
            * float(np.finfo(np.float64).eps)
            * float(operation_count)
            * score_scale
        )
        possible_winners = np.flatnonzero(
            scores <= best_score + 2.0 * roundoff_bound
        ).tolist()
        if len(possible_winners) == 1:
            return _materialize_beam_state(
                beam[int(possible_winners[0])],
                zone_count,
            )
    else:
        possible_winners = list(range(len(beam)))

    # Stable first-winner behavior matches sorting the historical completion
    # list by global cost alone.
    best_global_cost = float("inf")
    best_selected: np.ndarray | None = None
    for state_index in possible_winners:
        selected = _materialize_beam_state(beam[int(state_index)], zone_count)
        global_cost = _global_assignment_cost(
            selected,
            candidate_sets,
            adjacency_edges,
            adjacency_edge_lengths_px,
            continuity_weight=continuity_weight,
            retaining_wall_weight=retaining_wall_weight,
            local_cost_weights=local_cost_weights,
        )
        if global_cost < best_global_cost:
            best_global_cost = float(global_cost)
            best_selected = selected
    if best_selected is None:
        raise RuntimeError("completed beam produced no globally scored assignment")
    return best_selected

def _seed_zone_recipe_labels_with_beam(
    *,
    zone_count: int,
    adjacency_edges: tuple[tuple[int, int], ...],
    adjacency_edge_lengths_px: np.ndarray,
    zone_pixel_counts: np.ndarray,
    target_oklab_var_by_zone: np.ndarray,
    candidate_sets: tuple[_ZoneCandidateSet, ...],
    continuity_weight: float = _STAGE2_CONTINUITY_WEIGHT,
    retaining_wall_weight: float = _STAGE2_RETAINING_WALL_WEIGHT,
    beam_width: int = _STAGE2_BEAM_WIDTH,
    local_cost_weights: np.ndarray | None = None,
) -> _BeamSeedResult:
    """Build a better Stage 2 seed assignment via a small beam search."""
    zone_count = len(candidate_sets)
    if zone_count == 0:
        return _BeamSeedResult(
            selected_stack_ids=np.zeros(0, dtype=np.int32),
            expansion_count=0,
            max_beam_size=0,
        )
    neighbors = _build_zone_neighbors(
        zone_count,
        adjacency_edges,
        adjacency_edge_lengths_px,
    )
    total_edge_weight = float(np.sum(adjacency_edge_lengths_px))
    order = _zone_assignment_order(
        zone_count=zone_count,
        adjacency_edges=adjacency_edges,
        zone_pixel_counts=zone_pixel_counts,
        target_oklab_var_by_zone=target_oklab_var_by_zone,
    )
    assignable_mask = np.array(
        [candidate_set.local_scores.size > 0 for candidate_set in candidate_sets],
        dtype=bool,
    )
    retaining_penalties = tuple(
        (
            np.maximum(
                0.0,
                candidate_set.total_thickness_mm - float(np.min(candidate_set.total_thickness_mm)),
            ).astype(np.float32, copy=False)
            if candidate_set.total_thickness_mm.size
            else np.zeros(0, dtype=np.float32)
        )
        for candidate_set in candidate_sets
    )

    beam: list[_BeamSearchState] = [
        _BeamSearchState(
            score=0.0,
            local_sum=0.0,
            retaining_sum=0.0,
            edge_sum=0.0,
            checkpoint_selected=np.full(zone_count, -1, dtype=np.int32),
            delta_choices={},
        )
    ]
    assigned_mask = np.zeros(zone_count, dtype=bool)
    width = max(1, int(beam_width))
    expansion_count = 0
    max_beam_size = 1
    local_weights = (
        np.asarray(local_cost_weights, dtype=np.float32)
        if local_cost_weights is not None
        else np.ones(zone_count, dtype=np.float32)
    )

    assigned_depth = 0
    for zone_id in order:
        zone_id = int(zone_id)
        candidate_set = candidate_sets[zone_id]
        if candidate_set.local_scores.size == 0:
            continue
        next_beam: list[_BeamSearchState] = []
        candidate_totals = candidate_set.total_thickness_mm
        candidate_locals = candidate_set.local_scores
        candidate_retaining = retaining_penalties[zone_id]
        for state in beam:
            for candidate_index in range(candidate_set.local_scores.size):
                expansion_count += 1
                next_local_sum = (
                    state.local_sum
                    + float(local_weights[zone_id]) * float(candidate_locals[candidate_index])
                )
                next_retaining_sum = state.retaining_sum + float(
                    candidate_retaining[candidate_index]
                )
                next_edge_sum = state.edge_sum
                total_mm = float(candidate_totals[candidate_index]) if candidate_totals.size else 0.0
                for neighbor_zone_id, edge_weight in neighbors[zone_id]:
                    if not assigned_mask[int(neighbor_zone_id)]:
                        continue
                    neighbor_set = candidate_sets[int(neighbor_zone_id)]
                    neighbor_choice = _beam_state_candidate_index(
                        state,
                        int(neighbor_zone_id),
                    )
                    if neighbor_choice < 0:
                        raise RuntimeError(
                            "beam state is missing an assigned neighbor choice"
                        )
                    neighbor_total = (
                        float(neighbor_set.total_thickness_mm[neighbor_choice])
                        if neighbor_set.total_thickness_mm.size
                        else 0.0
                    )
                    step_mm = abs(total_mm - neighbor_total)
                    next_edge_sum += float(edge_weight) * (step_mm ** 2)
                score = next_local_sum + float(retaining_wall_weight) * next_retaining_sum
                if total_edge_weight > 0.0:
                    score += float(continuity_weight) * (next_edge_sum / total_edge_weight)
                next_beam.append(
                    _BeamSearchState(
                        score=score,
                        local_sum=next_local_sum,
                        retaining_sum=next_retaining_sum,
                        edge_sum=next_edge_sum,
                        parent=state,
                        zone_id=zone_id,
                        candidate_index=int(candidate_index),
                    )
                )
        next_beam.sort(key=lambda item: item.score)
        beam = [
            _checkpoint_beam_state(
                state,
                zone_count,
                force_dense_checkpoint=(
                    (assigned_depth + 1) % _STAGE2_BEAM_CHECKPOINT_INTERVAL == 0
                ),
            )
            for state in next_beam[:width]
        ]
        assigned_mask[zone_id] = True
        assigned_depth += 1
        max_beam_size = max(max_beam_size, len(beam))

    if not beam or not np.all(assigned_mask | ~assignable_mask):
        local_seed = np.zeros(zone_count, dtype=np.int32)
        for zone_id, candidate_set in enumerate(candidate_sets):
            if candidate_set.local_scores.size:
                local_seed[zone_id] = int(np.argmin(candidate_set.local_scores))
        return _BeamSeedResult(
            selected_stack_ids=local_seed,
            expansion_count=int(expansion_count),
            max_beam_size=int(max_beam_size),
        )
    completed_selected = _select_completed_beam_assignment(
        beam,
        zone_count=zone_count,
        candidate_sets=candidate_sets,
        adjacency_edges=adjacency_edges,
        adjacency_edge_lengths_px=adjacency_edge_lengths_px,
        continuity_weight=continuity_weight,
        retaining_wall_weight=retaining_wall_weight,
        local_cost_weights=local_weights,
    )
    return _BeamSeedResult(
        selected_stack_ids=completed_selected.astype(np.int32, copy=True),
        expansion_count=int(expansion_count),
        max_beam_size=int(max_beam_size),
    )

def _run_coord_descent(
    *,
    selected_stack_ids: np.ndarray,
    candidate_sets: tuple[_ZoneCandidateSet, ...],
    neighbors: list[list[tuple[int, float]]],
    continuity_weight: float,
    retaining_wall_weight: float,
    max_passes: int,
    local_cost_weights: np.ndarray | None = None,
) -> tuple[np.ndarray, np.ndarray, int, int]:
    """Run the existing Stage 2 local objective with vectorized per-zone scoring."""
    selected = selected_stack_ids.astype(np.int32, copy=True)
    selected_totals = _selected_total_thicknesses(selected, candidate_sets)
    retaining_penalties = _candidate_retaining_penalties(candidate_sets)
    local_weights = (
        np.asarray(local_cost_weights, dtype=np.float32)
        if local_cost_weights is not None
        else np.ones(len(candidate_sets), dtype=np.float32)
    )
    candidate_totals_by_zone = tuple(
        np.array(candidate_set.total_thickness_mm, dtype=np.float64, copy=True)
        for candidate_set in candidate_sets
    )
    local_costs_by_zone = tuple(
        candidate_set.local_scores.astype(np.float64, copy=False)
        * float(local_weights[zone_id])
        for zone_id, candidate_set in enumerate(candidate_sets)
    )
    retaining_costs_by_zone = tuple(
        np.array(penalties, dtype=np.float64, copy=True)
        for penalties in retaining_penalties
    )
    neighbor_arrays = tuple(
        (
            np.fromiter(
                (neighbor_zone_id for neighbor_zone_id, _ in zone_neighbors),
                dtype=np.int32,
                count=len(zone_neighbors),
            ),
            np.fromiter(
                (edge_weight for _, edge_weight in zone_neighbors),
                dtype=np.float64,
                count=len(zone_neighbors),
            ),
        )
        for zone_neighbors in neighbors
    )
    neighbor_weight_sums = tuple(
        float(np.sum(weights)) for _, weights in neighbor_arrays
    )
    for prepared_group in (
        candidate_totals_by_zone,
        local_costs_by_zone,
        retaining_costs_by_zone,
    ):
        for prepared in prepared_group:
            prepared.flags.writeable = False
    for neighbor_zone_ids, neighbor_weights in neighbor_arrays:
        neighbor_zone_ids.flags.writeable = False
        neighbor_weights.flags.writeable = False
    pass_count = 0
    eval_count = 0

    for _ in range(max_passes):
        pass_count += 1
        changed = False
        for zone_id, candidate_set in enumerate(candidate_sets):
            if candidate_set.local_scores.size <= 1:
                continue
            candidate_totals = candidate_totals_by_zone[zone_id]
            local_costs = local_costs_by_zone[zone_id]
            retaining_costs = retaining_costs_by_zone[zone_id]
            eval_count += int(candidate_totals.size)

            neighbor_zone_ids, neighbor_weights = neighbor_arrays[zone_id]
            if neighbor_zone_ids.size:
                neighbor_totals = selected_totals[neighbor_zone_ids].astype(np.float64, copy=False)
                steps_sq = np.square(candidate_totals[:, None] - neighbor_totals[None, :], dtype=np.float64)
                boundary_costs = np.sum(steps_sq * neighbor_weights[None, :], axis=1)
                weight_sum = neighbor_weight_sums[zone_id]
                if weight_sum > 0.0:
                    boundary_costs /= weight_sum
            else:
                boundary_costs = np.zeros(candidate_totals.shape[0], dtype=np.float64)

            total_costs = (
                local_costs
                + float(retaining_wall_weight) * retaining_costs
                + float(continuity_weight) * boundary_costs
            )
            best_index = int(np.argmin(total_costs))
            if best_index != int(selected[zone_id]):
                selected[zone_id] = best_index
                selected_totals[zone_id] = float(candidate_set.total_thickness_mm[best_index])
                changed = True
        if not changed:
            break

    return selected, selected_totals, pass_count, eval_count

def _repair_worst_boundary_pairs(
    *,
    selected_stack_ids: np.ndarray,
    candidate_sets: tuple[_ZoneCandidateSet, ...],
    adjacency_edges: tuple[tuple[int, int], ...],
    adjacency_edge_lengths_px: np.ndarray,
    continuity_weight: float,
    retaining_wall_weight: float,
    local_cost_weights: np.ndarray | None = None,
    max_passes: int = _STAGE2_PAIR_REPAIR_PASSES,
    edge_probe_count: int = _STAGE2_PAIR_REPAIR_EDGE_PROBE_COUNT,
) -> tuple[np.ndarray, int, int, int]:
    """Try joint two-zone repairs on the worst remaining seam edges."""
    repaired = selected_stack_ids.astype(np.int32, copy=True)
    if len(adjacency_edges) == 0:
        return repaired, 0, 0, 0

    total_zone_changes = 0
    probe_limit = max(1, int(edge_probe_count))
    pass_limit = max(1, int(max_passes))
    selected_totals = _selected_total_thicknesses(repaired, candidate_sets)
    retaining_penalties = _candidate_retaining_penalties(candidate_sets)
    local_weights = (
        np.asarray(local_cost_weights, dtype=np.float32)
        if local_cost_weights is not None
        else np.ones(len(candidate_sets), dtype=np.float32)
    )
    zone_edge_indices = _build_zone_edge_indices(len(candidate_sets), adjacency_edges)
    total_edge_weight = float(np.sum(adjacency_edge_lengths_px))
    edge_contribs = np.zeros(len(adjacency_edges), dtype=np.float64)
    local_sum = 0.0
    retaining_sum = 0.0
    for zone_id, candidate_set in enumerate(candidate_sets):
        if candidate_set.local_scores.size == 0:
            continue
        candidate_index = int(repaired[zone_id])
        local_sum += float(local_weights[zone_id]) * float(candidate_set.local_scores[candidate_index])
        retaining_sum += float(retaining_penalties[zone_id][candidate_index])
    for edge_index, (lhs, rhs) in enumerate(adjacency_edges):
        step_mm = abs(float(selected_totals[int(lhs)]) - float(selected_totals[int(rhs)]))
        edge_contribs[edge_index] = float(adjacency_edge_lengths_px[edge_index]) * (step_mm ** 2)
    current_cost = local_sum + float(retaining_wall_weight) * retaining_sum
    if total_edge_weight > 0.0:
        current_cost += float(continuity_weight) * float(np.sum(edge_contribs) / total_edge_weight)

    trial_count = 0
    executed_passes = 0
    for _ in range(pass_limit):
        executed_passes += 1
        edge_steps = _edge_step_arrays_mm(repaired, candidate_sets, adjacency_edges)
        if edge_steps.size == 0:
            break
        edge_order = np.argsort(-edge_steps, kind="stable")
        changed = False
        for edge_index in edge_order[:probe_limit]:
            lhs, rhs = adjacency_edges[int(edge_index)]
            lhs = int(lhs)
            rhs = int(rhs)
            lhs_set = candidate_sets[lhs]
            rhs_set = candidate_sets[rhs]
            if lhs_set.local_scores.size <= 1 and rhs_set.local_scores.size <= 1:
                continue
            current_lhs = int(repaired[lhs])
            current_rhs = int(repaired[rhs])
            best_lhs = current_lhs
            best_rhs = current_rhs
            best_cost = float(current_cost)
            affected_edges = sorted(set(zone_edge_indices[lhs]) | set(zone_edge_indices[rhs]))
            old_edge_sum = float(np.sum(edge_contribs[affected_edges])) if affected_edges else 0.0
            current_lhs_local = float(lhs_set.local_scores[current_lhs]) if lhs_set.local_scores.size else 0.0
            current_rhs_local = float(rhs_set.local_scores[current_rhs]) if rhs_set.local_scores.size else 0.0
            current_lhs_retaining = (
                float(retaining_penalties[lhs][current_lhs]) if retaining_penalties[lhs].size else 0.0
            )
            current_rhs_retaining = (
                float(retaining_penalties[rhs][current_rhs]) if retaining_penalties[rhs].size else 0.0
            )
            for lhs_candidate_index in range(lhs_set.local_scores.size):
                for rhs_candidate_index in range(rhs_set.local_scores.size):
                    trial_count += 1
                    if (
                        lhs_candidate_index == current_lhs
                        and rhs_candidate_index == current_rhs
                    ):
                        continue
                    lhs_total = float(lhs_set.total_thickness_mm[lhs_candidate_index]) if lhs_set.total_thickness_mm.size else 0.0
                    rhs_total = float(rhs_set.total_thickness_mm[rhs_candidate_index]) if rhs_set.total_thickness_mm.size else 0.0
                    delta_local = (
                        float(local_weights[lhs]) * float(lhs_set.local_scores[lhs_candidate_index])
                        + float(local_weights[rhs]) * float(rhs_set.local_scores[rhs_candidate_index])
                        - float(local_weights[lhs]) * current_lhs_local
                        - float(local_weights[rhs]) * current_rhs_local
                    )
                    delta_retaining = (
                        (float(retaining_penalties[lhs][lhs_candidate_index]) if retaining_penalties[lhs].size else 0.0)
                        + (float(retaining_penalties[rhs][rhs_candidate_index]) if retaining_penalties[rhs].size else 0.0)
                        - current_lhs_retaining
                        - current_rhs_retaining
                    )
                    new_edge_sum = 0.0
                    for affected_edge_index in affected_edges:
                        edge_lhs, edge_rhs = adjacency_edges[int(affected_edge_index)]
                        if int(edge_lhs) == lhs:
                            edge_lhs_total = lhs_total
                        elif int(edge_lhs) == rhs:
                            edge_lhs_total = rhs_total
                        else:
                            edge_lhs_total = float(selected_totals[int(edge_lhs)])
                        if int(edge_rhs) == lhs:
                            edge_rhs_total = lhs_total
                        elif int(edge_rhs) == rhs:
                            edge_rhs_total = rhs_total
                        else:
                            edge_rhs_total = float(selected_totals[int(edge_rhs)])
                        step_mm = abs(edge_lhs_total - edge_rhs_total)
                        new_edge_sum += float(adjacency_edge_lengths_px[int(affected_edge_index)]) * (step_mm ** 2)
                    delta_edge = new_edge_sum - old_edge_sum
                    trial_cost = (
                        current_cost
                        + delta_local
                        + float(retaining_wall_weight) * delta_retaining
                    )
                    if total_edge_weight > 0.0:
                        trial_cost += float(continuity_weight) * (delta_edge / total_edge_weight)
                    if trial_cost + 1e-12 < best_cost:
                        best_cost = trial_cost
                        best_lhs = int(lhs_candidate_index)
                        best_rhs = int(rhs_candidate_index)
            if best_lhs != current_lhs or best_rhs != current_rhs:
                total_zone_changes += int(best_lhs != current_lhs) + int(best_rhs != current_rhs)
                repaired[lhs] = best_lhs
                repaired[rhs] = best_rhs
                selected_totals[lhs] = float(lhs_set.total_thickness_mm[best_lhs]) if lhs_set.total_thickness_mm.size else 0.0
                selected_totals[rhs] = float(rhs_set.total_thickness_mm[best_rhs]) if rhs_set.total_thickness_mm.size else 0.0
                for affected_edge_index in affected_edges:
                    edge_lhs, edge_rhs = adjacency_edges[int(affected_edge_index)]
                    step_mm = abs(float(selected_totals[int(edge_lhs)]) - float(selected_totals[int(edge_rhs)]))
                    edge_contribs[int(affected_edge_index)] = (
                        float(adjacency_edge_lengths_px[int(affected_edge_index)]) * (step_mm ** 2)
                    )
                current_cost = float(best_cost)
                changed = True
        if not changed:
            break

    return repaired, total_zone_changes, executed_passes, trial_count

def _optimize_zone_recipe_labels(
    *,
    candidate_sets: tuple[_ZoneCandidateSet, ...],
    adjacency_edges: tuple[tuple[int, int], ...],
    adjacency_edge_lengths_px: np.ndarray,
    zone_pixel_counts: np.ndarray | None = None,
    local_cost_weights: np.ndarray | None = None,
    initial_selected_stack_ids: np.ndarray | None = None,
    continuity_weight: float = _STAGE2_CONTINUITY_WEIGHT,
    retaining_wall_weight: float = _STAGE2_RETAINING_WALL_WEIGHT,
    max_passes: int = _STAGE2_MAX_COORD_DESCENT_PASSES,
) -> _ZoneRecipeOptimizationResult:
    """Coordinate-descent Stage 2 recipe selection with boundary-aware costs."""
    zone_count = len(candidate_sets)
    if local_cost_weights is None:
        local_cost_weights = np.ones(zone_count, dtype=np.float32)
    else:
        local_cost_weights = np.asarray(local_cost_weights, dtype=np.float32)
    local_seed_selected_stack_ids = np.zeros(zone_count, dtype=np.int32)
    for zone_id, candidate_set in enumerate(candidate_sets):
        if candidate_set.local_scores.size == 0:
            continue
        local_seed_selected_stack_ids[zone_id] = int(np.argmin(candidate_set.local_scores))
    if initial_selected_stack_ids is None:
        initial_selected_stack_ids = local_seed_selected_stack_ids.copy()
    else:
        initial_selected_stack_ids = initial_selected_stack_ids.astype(np.int32, copy=True)
    neighbors = _build_zone_neighbors(zone_count, adjacency_edges, adjacency_edge_lengths_px)
    boundary_local_seed = _mean_boundary_step_mm(
        local_seed_selected_stack_ids,
        candidate_sets,
        adjacency_edges,
        adjacency_edge_lengths_px,
    )
    boundary_before = _mean_boundary_step_mm(
        initial_selected_stack_ids,
        candidate_sets,
        adjacency_edges,
        adjacency_edge_lengths_px,
    )
    coord_start = time.perf_counter()
    selected_stack_ids, _, coord_descent_pass_count, coord_descent_eval_count = _run_coord_descent(
        selected_stack_ids=initial_selected_stack_ids,
        candidate_sets=candidate_sets,
        neighbors=neighbors,
        continuity_weight=continuity_weight,
        retaining_wall_weight=retaining_wall_weight,
        max_passes=max_passes,
        local_cost_weights=local_cost_weights,
    )
    coord_descent_elapsed_s = float(time.perf_counter() - coord_start)

    boundary_after_coord = _mean_boundary_step_mm(
        selected_stack_ids,
        candidate_sets,
        adjacency_edges,
        adjacency_edge_lengths_px,
    )
    pair_start = time.perf_counter()
    selected_stack_ids, pair_repair_zone_changes, pair_repair_pass_count, pair_repair_trial_count = _repair_worst_boundary_pairs(
        selected_stack_ids=selected_stack_ids,
        candidate_sets=candidate_sets,
        adjacency_edges=adjacency_edges,
        adjacency_edge_lengths_px=adjacency_edge_lengths_px,
        continuity_weight=continuity_weight,
        retaining_wall_weight=retaining_wall_weight,
        local_cost_weights=local_cost_weights,
    )
    pair_repair_elapsed_s = float(time.perf_counter() - pair_start)
    boundary_after = _mean_boundary_step_mm(
        selected_stack_ids,
        candidate_sets,
        adjacency_edges,
        adjacency_edge_lengths_px,
    )
    changed_zone_count = int(np.count_nonzero(selected_stack_ids != initial_selected_stack_ids))
    return _ZoneRecipeOptimizationResult(
        local_seed_selected_stack_ids=local_seed_selected_stack_ids,
        selected_stack_ids=selected_stack_ids,
        initial_selected_stack_ids=initial_selected_stack_ids,
        boundary_step_mean_local_seed_mm=boundary_local_seed,
        boundary_step_mean_before_mm=boundary_before,
        boundary_step_mean_after_coord_mm=boundary_after_coord,
        boundary_step_mean_after_mm=boundary_after,
        changed_zone_count=changed_zone_count,
        pair_repair_zone_changes=pair_repair_zone_changes,
        coord_descent_pass_count=coord_descent_pass_count,
        coord_descent_eval_count=coord_descent_eval_count,
        pair_repair_pass_count=pair_repair_pass_count,
        pair_repair_trial_count=pair_repair_trial_count,
        coord_descent_elapsed_s=coord_descent_elapsed_s,
        pair_repair_elapsed_s=pair_repair_elapsed_s,
    )

def _build_stage2_objective_summary(
    *,
    zone_count: int,
    zone_pixel_counts: np.ndarray,
    target_oklab_var_by_zone: np.ndarray,
    adjacency_edges: tuple[tuple[int, int], ...],
    adjacency_edge_lengths_px: np.ndarray,
    candidate_sets: tuple[_ZoneCandidateSet, ...],
    optimization: _ZoneRecipeOptimizationResult,
    continuity_weight: float,
    retaining_wall_weight: float,
    local_cost_weights: np.ndarray | None = None,
) -> Stage2ObjectiveSummary:
    """Materialize a stable Stage 2 objective summary from optimization state."""
    neighbors = _build_zone_neighbors(
        zone_count,
        adjacency_edges,
        adjacency_edge_lengths_px,
    )
    if local_cost_weights is None:
        local_cost_weights = np.ones(zone_count, dtype=np.float32)
    else:
        local_cost_weights = np.asarray(local_cost_weights, dtype=np.float32)
    changed_zones: list[Stage2ZoneObjectiveBreakdown] = []
    local_costs_before: list[float] = []
    local_costs_after: list[float] = []
    target_variance_norms: list[float] = []
    steps_before = _edge_step_arrays_mm(
        optimization.initial_selected_stack_ids,
        candidate_sets,
        adjacency_edges,
    )
    steps_after = _edge_step_arrays_mm(
        optimization.selected_stack_ids,
        candidate_sets,
        adjacency_edges,
    )
    for zone_id, candidate_set in enumerate(candidate_sets):
        if candidate_set.candidate_ids.size == 0:
            continue
        initial_index = int(optimization.initial_selected_stack_ids[zone_id])
        final_index = int(optimization.selected_stack_ids[zone_id])
        variance_norm = float(np.sqrt(np.sum(target_oklab_var_by_zone[zone_id])))
        target_variance_norms.append(variance_norm)
        if initial_index == final_index:
            local_cost = float(local_cost_weights[zone_id]) * float(
                candidate_set.local_scores[initial_index]
            )
            local_costs_before.append(local_cost)
            local_costs_after.append(local_cost)
            continue
        before = _zone_objective_breakdown(
            zone_id=zone_id,
            candidate_index=initial_index,
            selected_stack_ids=optimization.initial_selected_stack_ids,
            candidate_sets=candidate_sets,
            neighbors=neighbors,
            continuity_weight=continuity_weight,
            retaining_wall_weight=retaining_wall_weight,
            local_cost_weight=float(local_cost_weights[zone_id]),
        )
        after = _zone_objective_breakdown(
            zone_id=zone_id,
            candidate_index=final_index,
            selected_stack_ids=optimization.selected_stack_ids,
            candidate_sets=candidate_sets,
            neighbors=neighbors,
            continuity_weight=continuity_weight,
            retaining_wall_weight=retaining_wall_weight,
            local_cost_weight=float(local_cost_weights[zone_id]),
        )
        local_costs_before.append(before.local_cost)
        local_costs_after.append(after.local_cost)
        changed_zones.append(
            Stage2ZoneObjectiveBreakdown(
                zone_id=zone_id,
                changed=True,
                initial_stack_id=int(candidate_set.candidate_ids[initial_index]),
                selected_stack_id=int(candidate_set.candidate_ids[final_index]),
                local_cost_before=before.local_cost,
                local_cost_after=after.local_cost,
                boundary_cost_before=before.boundary_cost,
                boundary_cost_after=after.boundary_cost,
                retaining_cost_before=before.retaining_cost,
                retaining_cost_after=after.retaining_cost,
                total_cost_before=before.total_cost,
                total_cost_after=after.total_cost,
                target_variance_norm=variance_norm,
            )
        )
    local_before_mean = float(np.mean(local_costs_before)) if local_costs_before else 0.0
    local_after_mean = float(np.mean(local_costs_after)) if local_costs_after else 0.0
    intra_zone_variance_mean = float(np.mean(target_variance_norms)) if target_variance_norms else 0.0
    changed_zones.sort(key=lambda item: item.total_cost_before - item.total_cost_after, reverse=True)
    boundary_step_p95_before = float(np.percentile(steps_before, 95)) if steps_before.size else 0.0
    boundary_step_p95_after = float(np.percentile(steps_after, 95)) if steps_after.size else 0.0
    worst_edges: list[Stage2EdgeSeamSummary] = []
    for edge_index, (lhs, rhs) in enumerate(adjacency_edges):
        worst_edges.append(
            Stage2EdgeSeamSummary(
                zone_a=int(lhs),
                zone_b=int(rhs),
                shared_length_px=int(adjacency_edge_lengths_px[edge_index]),
                step_before_mm=float(steps_before[edge_index]) if steps_before.size else 0.0,
                step_after_mm=float(steps_after[edge_index]) if steps_after.size else 0.0,
                step_delta_mm=(
                    float(steps_before[edge_index] - steps_after[edge_index])
                    if steps_after.size
                    else 0.0
                ),
            )
        )
    worst_edges.sort(key=lambda item: (item.step_after_mm, item.shared_length_px), reverse=True)
    return Stage2ObjectiveSummary(
        continuity_weight=float(continuity_weight),
        retaining_wall_weight=float(retaining_wall_weight),
        local_cost_mean_before=local_before_mean,
        local_cost_mean_after=local_after_mean,
        intra_zone_target_variance_mean=intra_zone_variance_mean,
        boundary_step_mean_before_mm=float(optimization.boundary_step_mean_before_mm),
        boundary_step_mean_after_mm=float(optimization.boundary_step_mean_after_mm),
        boundary_step_p95_before_mm=boundary_step_p95_before,
        boundary_step_p95_after_mm=boundary_step_p95_after,
        changed_zone_count=int(optimization.changed_zone_count),
        changed_zones=tuple(changed_zones),
        worst_edges=tuple(worst_edges[:5]),
    )

def _selected_zone_stack_ids(
    candidate_sets: tuple[_ZoneCandidateSet, ...],
    optimization: _ZoneRecipeOptimizationResult,
) -> np.ndarray:
    """Return the selected visible stack id for each Stage 2 zone."""
    selected = np.full(len(candidate_sets), -1, dtype=np.int32)
    for zone_id, candidate_set in enumerate(candidate_sets):
        if candidate_set.candidate_ids.size == 0:
            continue
        selected[zone_id] = int(candidate_set.candidate_ids[int(optimization.selected_stack_ids[zone_id])])
    return selected

__all__ = (
    '_STAGE2_MAX_COORD_DESCENT_PASSES',
    '_STAGE2_BEAM_WIDTH',
    '_STAGE2_BEAM_CHECKPOINT_INTERVAL',
    '_STAGE2_PAIR_REPAIR_PASSES',
    '_STAGE2_PAIR_REPAIR_EDGE_PROBE_COUNT',
    '_zone_assignment_order',
    '_global_assignment_cost',
    '_beam_state_candidate_index',
    '_materialize_beam_state',
    '_checkpoint_beam_state',
    '_select_completed_beam_assignment',
    '_seed_zone_recipe_labels_with_beam',
    '_run_coord_descent',
    '_repair_worst_boundary_pairs',
    '_optimize_zone_recipe_labels',
    '_build_stage2_objective_summary',
    '_selected_zone_stack_ids',
)
