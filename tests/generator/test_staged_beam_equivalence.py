from __future__ import annotations

import numpy as np

from pipeline.staged_runner import (
    _BeamSeedResult,
    _ZoneCandidateSet,
    _build_zone_neighbors,
    _global_assignment_cost,
    _seed_zone_recipe_labels_with_beam,
    _zone_assignment_order,
)


def _reference_copying_beam(
    *,
    zone_count: int,
    adjacency_edges: tuple[tuple[int, int], ...],
    adjacency_edge_lengths_px: np.ndarray,
    zone_pixel_counts: np.ndarray,
    target_oklab_var_by_zone: np.ndarray,
    candidate_sets: tuple[_ZoneCandidateSet, ...],
    continuity_weight: float,
    retaining_wall_weight: float,
    beam_width: int,
    local_cost_weights: np.ndarray | None,
) -> _BeamSeedResult:
    """Literal pre-PERF-3 beam implementation used as an exact oracle."""

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
                candidate_set.total_thickness_mm
                - float(np.min(candidate_set.total_thickness_mm)),
            ).astype(np.float32, copy=False)
            if candidate_set.total_thickness_mm.size
            else np.zeros(0, dtype=np.float32)
        )
        for candidate_set in candidate_sets
    )
    beam: list[tuple[float, float, float, float, np.ndarray, np.ndarray]] = [
        (
            0.0,
            0.0,
            0.0,
            0.0,
            np.full(zone_count, -1, dtype=np.int32),
            np.zeros(zone_count, dtype=bool),
        )
    ]
    width = max(1, int(beam_width))
    expansion_count = 0
    max_beam_size = 1
    local_weights = (
        np.asarray(local_cost_weights, dtype=np.float32)
        if local_cost_weights is not None
        else np.ones(zone_count, dtype=np.float32)
    )

    for raw_zone_id in order:
        zone_id = int(raw_zone_id)
        candidate_set = candidate_sets[zone_id]
        if candidate_set.local_scores.size == 0:
            continue
        next_beam: list[
            tuple[float, float, float, float, np.ndarray, np.ndarray]
        ] = []
        candidate_totals = candidate_set.total_thickness_mm
        candidate_locals = candidate_set.local_scores
        candidate_retaining = retaining_penalties[zone_id]
        for _, local_sum, retaining_sum, edge_sum, selected, assigned in beam:
            for candidate_index in range(candidate_set.local_scores.size):
                expansion_count += 1
                next_selected = selected.copy()
                next_assigned = assigned.copy()
                next_selected[zone_id] = candidate_index
                next_assigned[zone_id] = True
                next_local_sum = (
                    local_sum
                    + float(local_weights[zone_id])
                    * float(candidate_locals[candidate_index])
                )
                next_retaining_sum = retaining_sum + float(
                    candidate_retaining[candidate_index]
                )
                next_edge_sum = edge_sum
                total_mm = (
                    float(candidate_totals[candidate_index])
                    if candidate_totals.size
                    else 0.0
                )
                for neighbor_zone_id, edge_weight in neighbors[zone_id]:
                    if not assigned[int(neighbor_zone_id)]:
                        continue
                    neighbor_set = candidate_sets[int(neighbor_zone_id)]
                    neighbor_choice = int(selected[int(neighbor_zone_id)])
                    neighbor_total = (
                        float(neighbor_set.total_thickness_mm[neighbor_choice])
                        if neighbor_set.total_thickness_mm.size
                        else 0.0
                    )
                    step_mm = abs(total_mm - neighbor_total)
                    next_edge_sum += float(edge_weight) * (step_mm**2)
                score = (
                    next_local_sum
                    + float(retaining_wall_weight) * next_retaining_sum
                )
                if total_edge_weight > 0.0:
                    score += float(continuity_weight) * (
                        next_edge_sum / total_edge_weight
                    )
                next_beam.append(
                    (
                        score,
                        next_local_sum,
                        next_retaining_sum,
                        next_edge_sum,
                        next_selected,
                        next_assigned,
                    )
                )
        next_beam.sort(key=lambda item: item[0])
        beam = next_beam[:width]
        max_beam_size = max(max_beam_size, len(beam))

    completed = [
        (
            _global_assignment_cost(
                selected,
                candidate_sets,
                adjacency_edges,
                adjacency_edge_lengths_px,
                continuity_weight=continuity_weight,
                retaining_wall_weight=retaining_wall_weight,
                local_cost_weights=local_weights,
            ),
            selected,
        )
        for _, _, _, _, selected, assigned in beam
        if np.all(assigned | ~assignable_mask)
    ]
    if not completed:
        local_seed = np.zeros(zone_count, dtype=np.int32)
        for zone_id, candidate_set in enumerate(candidate_sets):
            if candidate_set.local_scores.size:
                local_seed[zone_id] = int(np.argmin(candidate_set.local_scores))
        return _BeamSeedResult(
            selected_stack_ids=local_seed,
            expansion_count=int(expansion_count),
            max_beam_size=int(max_beam_size),
        )
    completed.sort(key=lambda item: item[0])
    return _BeamSeedResult(
        selected_stack_ids=completed[0][1].astype(np.int32, copy=True),
        expansion_count=int(expansion_count),
        max_beam_size=int(max_beam_size),
    )


def _case(seed: int, zone_count: int) -> dict[str, object]:
    rng = np.random.default_rng(seed)
    edge_set = {(zone_id, zone_id + 1) for zone_id in range(zone_count - 1)}
    for _ in range(zone_count):
        lhs, rhs = sorted(rng.choice(zone_count, size=2, replace=False).tolist())
        edge_set.add((int(lhs), int(rhs)))
    edges = tuple(sorted(edge_set))
    candidate_sets = []
    for zone_id in range(zone_count):
        candidate_count = 0 if zone_id == zone_count // 3 and seed % 3 == 0 else int(
            rng.integers(1, 5)
        )
        candidate_sets.append(
            _ZoneCandidateSet(
                candidate_ids=np.arange(candidate_count, dtype=np.int32),
                local_scores=rng.random(candidate_count, dtype=np.float32),
                total_thickness_mm=(
                    rng.integers(0, 10, size=candidate_count).astype(np.float32)
                    * np.float32(0.08)
                ),
            )
        )
    return {
        "zone_count": zone_count,
        "adjacency_edges": edges,
        "adjacency_edge_lengths_px": rng.integers(
            1,
            20,
            size=len(edges),
            dtype=np.int32,
        ),
        "zone_pixel_counts": rng.integers(
            1,
            100,
            size=zone_count,
            dtype=np.int32,
        ),
        "target_oklab_var_by_zone": rng.random(
            (zone_count, 3),
            dtype=np.float32,
        ),
        "candidate_sets": tuple(candidate_sets),
        "continuity_weight": 0.12,
        "retaining_wall_weight": 0.03,
        "beam_width": 12,
        "local_cost_weights": rng.random(zone_count, dtype=np.float32),
    }


def test_checkpointed_beam_matches_copying_oracle_on_randomized_graphs() -> None:
    for seed in range(12):
        kwargs = _case(seed, zone_count=80 if seed == 11 else 6 + seed)
        expected = _reference_copying_beam(**kwargs)  # type: ignore[arg-type]
        actual = _seed_zone_recipe_labels_with_beam(**kwargs)  # type: ignore[arg-type]

        np.testing.assert_array_equal(
            actual.selected_stack_ids,
            expected.selected_stack_ids,
        )
        assert actual.expansion_count == expected.expansion_count
        assert actual.max_beam_size == expected.max_beam_size


def test_checkpointed_beam_preserves_stable_tie_order_across_checkpoint() -> None:
    zone_count = 80
    candidate_sets = tuple(
        _ZoneCandidateSet(
            candidate_ids=np.arange(3, dtype=np.int32),
            local_scores=np.zeros(3, dtype=np.float32),
            total_thickness_mm=np.zeros(3, dtype=np.float32),
        )
        for _ in range(zone_count)
    )
    kwargs = {
        "zone_count": zone_count,
        "adjacency_edges": tuple((index, index + 1) for index in range(zone_count - 1)),
        "adjacency_edge_lengths_px": np.ones(zone_count - 1, dtype=np.int32),
        "zone_pixel_counts": np.ones(zone_count, dtype=np.int32),
        "target_oklab_var_by_zone": np.zeros((zone_count, 3), dtype=np.float32),
        "candidate_sets": candidate_sets,
        "continuity_weight": 0.12,
        "retaining_wall_weight": 0.03,
        "beam_width": 12,
        "local_cost_weights": None,
    }

    expected = _reference_copying_beam(**kwargs)
    actual = _seed_zone_recipe_labels_with_beam(**kwargs)
    np.testing.assert_array_equal(actual.selected_stack_ids, expected.selected_stack_ids)
    np.testing.assert_array_equal(actual.selected_stack_ids, np.zeros(zone_count, dtype=np.int32))


def test_checkpointed_beam_falls_back_to_global_cost_for_nonstandard_weights() -> None:
    kwargs = _case(41, zone_count=10)
    local_weights = np.asarray(kwargs["local_cost_weights"], dtype=np.float32).copy()
    local_weights[0] = np.float32(-1.0)
    kwargs["local_cost_weights"] = local_weights

    expected = _reference_copying_beam(**kwargs)  # type: ignore[arg-type]
    actual = _seed_zone_recipe_labels_with_beam(**kwargs)  # type: ignore[arg-type]
    np.testing.assert_array_equal(actual.selected_stack_ids, expected.selected_stack_ids)
