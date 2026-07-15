"""Metrics for fixed-budget post-solve scheduling experiments."""
from __future__ import annotations

from collections import Counter, defaultdict
from typing import Iterable, Sequence

from .model import (
    WHITE_LAYER_TOKEN,
    FixedBudgetScheduleCase,
    LayerBudgetSignature,
    LayerSchedule,
    ScheduleEvaluation,
)


def _coerce_layers(schedule_or_layers: LayerSchedule | Sequence[str | None]) -> tuple[str | None, ...]:
    if isinstance(schedule_or_layers, LayerSchedule):
        return schedule_or_layers.absolute_layers
    return tuple(schedule_or_layers)


def _align_schedules(
    case: FixedBudgetScheduleCase,
    schedules: Sequence[LayerSchedule],
) -> tuple[LayerSchedule, ...]:
    if len(schedules) == len(case.entries) and all(
        schedule.signature == entry.signature
        for schedule, entry in zip(schedules, case.entries)
    ):
        return tuple(schedules)

    by_signature = {schedule.signature: schedule for schedule in schedules}
    if len(by_signature) != len(schedules):
        raise ValueError("schedules contains duplicate signatures")

    aligned: list[LayerSchedule] = []
    for entry in case.entries:
        schedule = by_signature.get(entry.signature)
        if schedule is None:
            raise ValueError(
                "missing schedule for signature "
                f"(stack_index={entry.signature.stack_index}, "
                f"white_layers={entry.signature.white_layers})"
            )
        aligned.append(schedule)
    return tuple(aligned)


def budget_preservation_deltas(
    signature: LayerBudgetSignature,
    schedule_or_layers: LayerSchedule | Sequence[str | None],
) -> tuple[tuple[str, int], ...]:
    """Return non-zero layer-budget deltas for one schedule."""
    layers = _coerce_layers(schedule_or_layers)
    observed = Counter(layers)
    expected = signature.full_layer_budget_map()

    nonzero: list[tuple[str, int]] = []
    for fid in signature.ordering:
        delta = int(observed.get(fid, 0) - expected.get(fid, 0))
        if delta != 0:
            nonzero.append((fid, delta))

    white_delta = int(observed.get(None, 0) - expected.get(WHITE_LAYER_TOKEN, 0))
    if white_delta != 0:
        nonzero.append((WHITE_LAYER_TOKEN, white_delta))

    extras = sorted(
        str(fid)
        for fid in observed
        if fid is not None and fid not in expected
    )
    for fid in extras:
        delta = int(observed.get(fid, 0))
        if delta != 0:
            nonzero.append((fid, delta))

    return tuple(nonzero)


def budget_preserved(
    signature: LayerBudgetSignature,
    schedule_or_layers: LayerSchedule | Sequence[str | None],
) -> bool:
    """Return ``True`` when the schedule preserves all quantized budgets."""
    return len(budget_preservation_deltas(signature, schedule_or_layers)) == 0


def top_white_reserve_ok(
    signature: LayerBudgetSignature,
    schedule_or_layers: LayerSchedule | Sequence[str | None],
) -> bool:
    """Check the contiguous top-white reserve against the schedule's own top."""
    layers = _coerce_layers(schedule_or_layers)
    reserve = min(int(signature.top_white_reserved_layers), int(signature.white_layers))
    if reserve <= 0:
        return True
    if len(layers) < reserve:
        return False
    return all(layer is None for layer in layers[-reserve:])


def family_area_by_absolute_layer_mm2(
    case: FixedBudgetScheduleCase,
    schedules: Sequence[LayerSchedule],
) -> tuple[tuple[tuple[str, float], ...], ...]:
    """Aggregate total active area by family and absolute layer.

    Unlike ``filament_area_by_absolute_layer_mm2()``, this includes white as an
    explicit family because setup/change accounting cares about white too.
    """
    aligned = _align_schedules(case, schedules)
    per_layer: list[dict[str, float]] = [
        defaultdict(float) for _ in range(case.max_total_layers)
    ]

    for entry, schedule in zip(case.entries, aligned):
        for layer_index, filament_id in enumerate(schedule.absolute_layers):
            family_id = WHITE_LAYER_TOKEN if filament_id is None else filament_id
            per_layer[layer_index][family_id] += float(entry.area_mm2)

    out: list[tuple[tuple[str, float], ...]] = []
    for layer_map in per_layer:
        ordered = tuple(
            sorted(
                ((fid, float(area)) for fid, area in layer_map.items()),
                key=lambda item: item[0],
            )
        )
        out.append(ordered)
    return tuple(out)


def filament_area_by_absolute_layer_mm2(
    case: FixedBudgetScheduleCase,
    schedules: Sequence[LayerSchedule],
) -> tuple[tuple[tuple[str, float], ...], ...]:
    """Aggregate total active area by filament and absolute layer.

    White layers are intentionally excluded because the metric is meant to
    capture operational chromatic activity.
    """
    aligned = _align_schedules(case, schedules)
    per_layer: list[dict[str, float]] = [
        defaultdict(float) for _ in range(case.max_total_layers)
    ]

    for entry, schedule in zip(case.entries, aligned):
        for layer_index, filament_id in enumerate(schedule.absolute_layers):
            if filament_id is None:
                continue
            per_layer[layer_index][filament_id] += float(entry.area_mm2)

    out: list[tuple[tuple[str, float], ...]] = []
    for layer_map in per_layer:
        ordered = tuple(
            sorted(
                ((fid, float(area)) for fid, area in layer_map.items()),
                key=lambda item: item[0],
            )
        )
        out.append(ordered)
    return tuple(out)


def active_filament_sets_by_absolute_layer(
    case: FixedBudgetScheduleCase,
    schedules: Sequence[LayerSchedule],
) -> tuple[tuple[str, ...], ...]:
    """Return the non-white active filament set for each absolute layer."""
    per_layer = filament_area_by_absolute_layer_mm2(case, schedules)
    return tuple(tuple(fid for fid, _ in layer) for layer in per_layer)


def active_family_sets_by_absolute_layer(
    case: FixedBudgetScheduleCase,
    schedules: Sequence[LayerSchedule],
) -> tuple[tuple[str, ...], ...]:
    """Return the active family set for each absolute layer, including white."""
    per_layer = family_area_by_absolute_layer_mm2(case, schedules)
    return tuple(tuple(fid for fid, _ in layer) for layer in per_layer)


def active_filaments_per_absolute_layer(
    case: FixedBudgetScheduleCase,
    schedules: Sequence[LayerSchedule],
) -> tuple[int, ...]:
    """Return the non-white active filament count for each absolute layer."""
    return tuple(
        len(layer)
        for layer in filament_area_by_absolute_layer_mm2(case, schedules)
    )


def active_families_per_absolute_layer(
    case: FixedBudgetScheduleCase,
    schedules: Sequence[LayerSchedule],
) -> tuple[int, ...]:
    """Return the active family count per absolute layer, including white."""
    return tuple(
        len(layer)
        for layer in family_area_by_absolute_layer_mm2(case, schedules)
    )


def adjacent_layer_filament_set_transition_cost(
    case: FixedBudgetScheduleCase,
    schedules: Sequence[LayerSchedule],
) -> int:
    """Penalize filament appearance/disappearance between adjacent layers."""
    filament_sets = active_filament_sets_by_absolute_layer(case, schedules)
    cost = 0
    for lower, upper in zip(filament_sets, filament_sets[1:]):
        cost += len(set(lower).symmetric_difference(upper))
    return int(cost)


def setup_change_cost(
    case: FixedBudgetScheduleCase,
    schedules: Sequence[LayerSchedule],
    *,
    start_family: str = WHITE_LAYER_TOKEN,
) -> int:
    """Return the minimum family-change cost with carry-over setup state.

    For one absolute layer, the printer must visit every active family in that
    layer at least once. The loaded family after one layer can carry over to the
    next layer. All family-to-family changes have unit cost.
    """
    family_sets = active_family_sets_by_absolute_layer(case, schedules)
    state_costs: dict[str, int] = {str(start_family): 0}

    for family_set in family_sets:
        active = tuple(family_set)
        if not active:
            continue
        next_costs: dict[str, int] = {}
        for end_family in active:
            best_cost: int | None = None
            for loaded_family, loaded_cost in state_costs.items():
                layer_cost = len(active) - 1 if loaded_family in active else len(active)
                total_cost = int(loaded_cost) + int(layer_cost)
                if best_cost is None or total_cost < best_cost:
                    best_cost = total_cost
            next_costs[end_family] = int(best_cost or 0)
        state_costs = next_costs

    return min(state_costs.values(), default=0)


def tiny_presence_penalty(
    case: FixedBudgetScheduleCase,
    schedules: Sequence[LayerSchedule],
    *,
    min_area_mm2: float = 1.0,
) -> float:
    """Penalize filament/layer presences whose total area is tiny."""
    threshold = float(min_area_mm2)
    if threshold <= 0.0:
        return 0.0

    penalty = 0.0
    for layer in family_area_by_absolute_layer_mm2(case, schedules):
        for _, area_mm2 in layer:
            if area_mm2 < threshold:
                penalty += threshold - area_mm2
    return float(penalty)


def evaluate_case_schedules(
    case: FixedBudgetScheduleCase,
    schedules: Sequence[LayerSchedule],
    *,
    policy_name: str | None = None,
    tiny_presence_threshold_mm2: float = 1.0,
) -> ScheduleEvaluation:
    """Build a structured evaluation for one case/policy result."""
    aligned = _align_schedules(case, schedules)
    budget_deltas = tuple(
        budget_preservation_deltas(entry.signature, schedule)
        for entry, schedule in zip(case.entries, aligned)
    )
    reserve_ok = tuple(
        top_white_reserve_ok(entry.signature, schedule)
        for entry, schedule in zip(case.entries, aligned)
    )
    chosen_policy = str(
        policy_name
        if policy_name is not None
        else (aligned[0].policy_name if aligned else "unknown")
    )
    layer_areas = filament_area_by_absolute_layer_mm2(case, aligned)

    return ScheduleEvaluation(
        policy_name=chosen_policy,
        schedules=aligned,
        budget_preserved=all(len(delta) == 0 for delta in budget_deltas),
        top_white_reserved_ok=all(reserve_ok),
        active_filaments_per_absolute_layer=tuple(len(layer) for layer in layer_areas),
        active_families_per_absolute_layer=active_families_per_absolute_layer(case, aligned),
        setup_change_cost=setup_change_cost(case, aligned),
        adjacent_layer_transition_cost=adjacent_layer_filament_set_transition_cost(case, aligned),
        tiny_presence_penalty=tiny_presence_penalty(
            case,
            aligned,
            min_area_mm2=tiny_presence_threshold_mm2,
        ),
        budget_deltas_by_signature=budget_deltas,
        top_white_reserved_ok_by_signature=reserve_ok,
        filament_area_by_absolute_layer_mm2=layer_areas,
    )


adjacent_layer_transition_cost = adjacent_layer_filament_set_transition_cost
evaluate_schedules = evaluate_case_schedules


__all__ = [
    "active_filament_sets_by_absolute_layer",
    "active_filaments_per_absolute_layer",
    "active_families_per_absolute_layer",
    "active_family_sets_by_absolute_layer",
    "adjacent_layer_transition_cost",
    "adjacent_layer_filament_set_transition_cost",
    "budget_preservation_deltas",
    "budget_preserved",
    "evaluate_case_schedules",
    "evaluate_schedules",
    "family_area_by_absolute_layer_mm2",
    "filament_area_by_absolute_layer_mm2",
    "setup_change_cost",
    "tiny_presence_penalty",
    "top_white_reserve_ok",
]
