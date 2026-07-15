"""Deterministic policy implementations for scheduling experiments."""
from __future__ import annotations

import sys
from collections import defaultdict
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import numpy as np

_THIS_DIR = Path(__file__).resolve().parent
_GEN_DIR = _THIS_DIR.parent
_PRISMA_DIR = _GEN_DIR.parent
for _path in (str(_GEN_DIR), str(_PRISMA_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from model import load_profile, predict_transmission  # noqa: E402

from .metrics import evaluate_case_schedules
from .model import FixedBudgetScheduleCase, LayerBudgetSignature, LayerSchedule


SchedulingPolicy = Callable[[FixedBudgetScheduleCase], tuple[LayerSchedule, ...]]

_DEFAULT_BEAM_WIDTH = 12


def _top_white_layers_used(signature: LayerBudgetSignature) -> int:
    return min(signature.white_layers, signature.top_white_reserved_layers)


def _color_sequence(signature: LayerBudgetSignature, order: Sequence[str]) -> list[str]:
    layers: list[str] = []
    budget = signature.color_layer_map()
    for fid in order:
        layers.extend([fid] * int(budget.get(fid, 0)))
    return layers


def _fallback_schedule(
    signature: LayerBudgetSignature,
    color_order: Sequence[str],
    *,
    policy_name: str,
) -> LayerSchedule:
    color_layers = _color_sequence(signature, color_order)
    absolute_layers = tuple(color_layers + ([None] * signature.white_layers))
    return LayerSchedule(
        signature=signature,
        absolute_layers=absolute_layers,
        policy_name=policy_name,
    )


@dataclass(frozen=True)
class _BeamState:
    remaining_colors: np.ndarray
    remaining_white: np.ndarray
    remaining_color_totals: np.ndarray
    prefix_choices: tuple[int, ...]
    prev_active_mask: int
    setup_cost: int
    active_count: int
    tiny_penalty: float

    @property
    def rank_key(self) -> tuple[int, int, float, int]:
        return (
            int(self.setup_cost),
            int(self.active_count),
            float(self.tiny_penalty),
            len(self.prefix_choices),
        )


def _white_envelope_schedule(signature: LayerBudgetSignature) -> LayerSchedule:
    top_white = _top_white_layers_used(signature)
    buried_white = signature.white_layers - top_white
    color_layers = _color_sequence(signature, signature.ordering)
    absolute_layers = tuple(
        ([None] * buried_white) + color_layers + ([None] * top_white)
    )
    return LayerSchedule(
        signature=signature,
        absolute_layers=absolute_layers,
        policy_name="white_envelope",
    )


@lru_cache(maxsize=None)
def _cached_profile_strength(
    filament_id: str,
    layer_height_mm: float,
    profiles_dir: str | None,
) -> float:
    base_dir = Path(profiles_dir) if profiles_dir else None
    try:
        profile = load_profile(filament_id, profiles_dir=base_dir)
    except Exception:
        return 0.0
    transmission = predict_transmission(profile, float(layer_height_mm))
    return float(1.0 - np.mean(transmission))


def absorber_strength_order(
    ordering: Sequence[str],
    layer_height_mm: float,
    *,
    profiles: Mapping[str, dict] | None = None,
    profiles_dir: Path | None = None,
) -> tuple[str, ...]:
    """Return a strongest-first deterministic global color order."""
    strengths: dict[str, float] = {}
    if profiles is not None:
        for fid in ordering:
            profile = profiles.get(fid)
            if profile is None:
                strengths[str(fid)] = 0.0
                continue
            transmission = predict_transmission(profile, float(layer_height_mm))
            strengths[str(fid)] = float(1.0 - np.mean(transmission))
    else:
        profiles_dir_str = str(profiles_dir) if profiles_dir is not None else None
        for fid in ordering:
            strengths[str(fid)] = _cached_profile_strength(
                str(fid),
                float(layer_height_mm),
                profiles_dir_str,
            )

    return tuple(
        sorted(
            (str(fid) for fid in ordering),
            key=lambda fid: (
                -float(strengths.get(fid, 0.0)),
                ordering.index(fid),
                fid,
            ),
        )
    )


def _color_priority_codes(
    case: FixedBudgetScheduleCase,
    *,
    profiles: Mapping[str, dict] | None = None,
    profiles_dir: Path | None = None,
) -> tuple[int, ...]:
    ordered = absorber_strength_order(
        case.ordering,
        case.layer_height_mm,
        profiles=profiles,
        profiles_dir=profiles_dir,
    )
    code_map = {fid: idx + 1 for idx, fid in enumerate(case.ordering)}
    return tuple(int(code_map[fid]) for fid in ordered if fid in code_map)


def _beam_initial_state(
    case: FixedBudgetScheduleCase,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    n_entries = len(case.entries)
    n_colors = len(case.ordering)
    remaining_colors = np.zeros((n_entries, n_colors), dtype=np.int16)
    remaining_white = np.zeros(n_entries, dtype=np.int16)
    lower_slots = np.zeros(n_entries, dtype=np.int16)

    for idx, entry in enumerate(case.entries):
        signature = entry.signature
        color_map = signature.color_layer_map()
        for color_idx, fid in enumerate(case.ordering):
            remaining_colors[idx, color_idx] = int(color_map.get(fid, 0))
        top_white = _top_white_layers_used(signature)
        remaining_white[idx] = int(signature.white_layers - top_white)
        lower_slots[idx] = int(signature.total_layers - top_white)

    remaining_color_totals = remaining_colors.sum(axis=1, dtype=np.int16)
    return remaining_colors, remaining_white, remaining_color_totals, lower_slots


def _beam_project_layer(
    *,
    remaining_colors: np.ndarray,
    remaining_white: np.ndarray,
    remaining_color_totals: np.ndarray,
    lower_slots: np.ndarray,
    layer_index: int,
    preferred_code: int,
    areas_mm2: np.ndarray,
    fallback_priority_codes: Sequence[int],
    tiny_presence_threshold_mm2: float,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, int, int, float]:
    next_colors = remaining_colors.copy()
    next_white = remaining_white.copy()
    next_color_totals = remaining_color_totals.copy()
    family_area = np.zeros(next_colors.shape[1] + 1, dtype=np.float64)

    for row_idx in range(next_colors.shape[0]):
        if layer_index >= int(lower_slots[row_idx]):
            continue

        slots_after = int(lower_slots[row_idx]) - int(layer_index) - 1
        can_use_white = (
            int(next_white[row_idx]) > 0
            and int(next_color_totals[row_idx]) <= slots_after
        )

        assigned_code = -1
        if preferred_code == 0:
            if can_use_white:
                assigned_code = 0
        else:
            color_idx = int(preferred_code) - 1
            if int(next_colors[row_idx, color_idx]) > 0:
                assigned_code = int(preferred_code)

        if assigned_code < 0 and can_use_white:
            assigned_code = 0

        if assigned_code < 0:
            best_code = 0
            best_count = -1
            for candidate_code in fallback_priority_codes:
                candidate_idx = int(candidate_code) - 1
                candidate_count = int(next_colors[row_idx, candidate_idx])
                if candidate_count > best_count:
                    best_count = candidate_count
                    best_code = int(candidate_code)
            assigned_code = best_code

        if assigned_code == 0:
            next_white[row_idx] -= 1
        else:
            assigned_idx = int(assigned_code) - 1
            next_colors[row_idx, assigned_idx] -= 1
            next_color_totals[row_idx] -= 1

        family_area[int(assigned_code)] += float(areas_mm2[row_idx])

    active_mask = 0
    active_color_count = 0
    tiny_penalty = 0.0
    for code, area_mm2 in enumerate(family_area.tolist()):
        if area_mm2 <= 0.0:
            continue
        active_mask |= 1 << int(code)
        if code != 0:
            active_color_count += 1
        if area_mm2 < tiny_presence_threshold_mm2:
            tiny_penalty += float(tiny_presence_threshold_mm2 - area_mm2)

    return (
        next_colors,
        next_white,
        next_color_totals,
        int(active_mask),
        int(active_color_count),
        float(tiny_penalty),
    )


def _template_sequence_to_schedules(
    case: FixedBudgetScheduleCase,
    preferred_codes: Sequence[int],
    *,
    policy_name: str,
) -> tuple[LayerSchedule, ...]:
    remaining_colors, remaining_white, remaining_color_totals, lower_slots = (
        _beam_initial_state(case)
    )
    areas_mm2 = np.array([float(entry.area_mm2) for entry in case.entries], dtype=np.float64)
    fallback_priority_codes = _color_priority_codes(case)
    n_entries = len(case.entries)
    lower_layers: list[list[int]] = [[] for _ in range(n_entries)]

    max_lower_slots = int(max(lower_slots.tolist(), default=0))
    choice_sequence = list(preferred_codes)
    if len(choice_sequence) < max_lower_slots:
        choice_sequence.extend([0] * (max_lower_slots - len(choice_sequence)))

    for layer_index in range(max_lower_slots):
        preferred_code = int(choice_sequence[layer_index])
        for row_idx in range(n_entries):
            if layer_index >= int(lower_slots[row_idx]):
                continue

            slots_after = int(lower_slots[row_idx]) - int(layer_index) - 1
            can_use_white = (
                int(remaining_white[row_idx]) > 0
                and int(remaining_color_totals[row_idx]) <= slots_after
            )
            assigned_code = -1
            if preferred_code == 0:
                if can_use_white:
                    assigned_code = 0
            else:
                color_idx = preferred_code - 1
                if int(remaining_colors[row_idx, color_idx]) > 0:
                    assigned_code = preferred_code

            if assigned_code < 0 and can_use_white:
                assigned_code = 0

            if assigned_code < 0:
                best_code = 0
                best_count = -1
                for candidate_code in fallback_priority_codes:
                    candidate_idx = int(candidate_code) - 1
                    candidate_count = int(remaining_colors[row_idx, candidate_idx])
                    if candidate_count > best_count:
                        best_count = candidate_count
                        best_code = int(candidate_code)
                assigned_code = best_code

            lower_layers[row_idx].append(int(assigned_code))
            if assigned_code == 0:
                remaining_white[row_idx] -= 1
            else:
                assigned_idx = int(assigned_code) - 1
                remaining_colors[row_idx, assigned_idx] -= 1
                remaining_color_totals[row_idx] -= 1

    schedules: list[LayerSchedule] = []
    for row_idx, entry in enumerate(case.entries):
        code_layers = lower_layers[row_idx]
        absolute_layers = [
            None if code == 0 else str(case.ordering[code - 1])
            for code in code_layers
        ]
        absolute_layers.extend([None] * _top_white_layers_used(entry.signature))
        schedules.append(
            LayerSchedule(
                signature=entry.signature,
                absolute_layers=tuple(absolute_layers),
                policy_name=policy_name,
            )
        )
    return tuple(schedules)


def setup_beam_search_policy(
    case: FixedBudgetScheduleCase,
    *,
    beam_width: int = _DEFAULT_BEAM_WIDTH,
) -> tuple[LayerSchedule, ...]:
    """Bounded beam search over per-layer family templates.

    The search chooses a preferred family for each lower-band absolute layer.
    Each signature then takes that family when possible, otherwise uses buried
    white when feasible, and otherwise falls back to the strongest remaining
    color demand. This keeps the search case-level and bounded even when the
    case contains many distinct signatures.
    """
    if not case.entries:
        return ()

    remaining_colors, remaining_white, remaining_color_totals, lower_slots = (
        _beam_initial_state(case)
    )
    areas_mm2 = np.array([float(entry.area_mm2) for entry in case.entries], dtype=np.float64)
    fallback_priority_codes = _color_priority_codes(case)
    max_lower_slots = int(max(lower_slots.tolist(), default=0))
    tiny_presence_threshold_mm2 = max(float(case.total_area_mm2) * 0.01, 1e-6)
    candidate_codes = (0,) + fallback_priority_codes

    beam: list[_BeamState] = [
        _BeamState(
            remaining_colors=remaining_colors,
            remaining_white=remaining_white,
            remaining_color_totals=remaining_color_totals,
            prefix_choices=(),
            prev_active_mask=1,  # white is loaded before the first lower layer
            setup_cost=0,
            active_count=0,
            tiny_penalty=0.0,
        )
    ]

    for layer_index in range(max_lower_slots):
        next_states: list[_BeamState] = []
        for state in beam:
            for preferred_code in candidate_codes:
                (
                    next_colors,
                    next_white,
                    next_color_totals,
                    active_mask,
                    active_color_count,
                    tiny_penalty,
                ) = _beam_project_layer(
                    remaining_colors=state.remaining_colors,
                    remaining_white=state.remaining_white,
                    remaining_color_totals=state.remaining_color_totals,
                    lower_slots=lower_slots,
                    layer_index=layer_index,
                    preferred_code=int(preferred_code),
                    areas_mm2=areas_mm2,
                    fallback_priority_codes=fallback_priority_codes,
                    tiny_presence_threshold_mm2=tiny_presence_threshold_mm2,
                )
                if active_mask == 0:
                    continue

                overlap = bool(int(state.prev_active_mask) & int(active_mask))
                setup_increment = int(active_mask.bit_count()) - 1 + (0 if overlap else 1)
                next_states.append(
                    _BeamState(
                        remaining_colors=next_colors,
                        remaining_white=next_white,
                        remaining_color_totals=next_color_totals,
                        prefix_choices=state.prefix_choices + (int(preferred_code),),
                        prev_active_mask=int(active_mask),
                        setup_cost=int(state.setup_cost + setup_increment),
                        active_count=int(state.active_count + active_color_count),
                        tiny_penalty=float(state.tiny_penalty + tiny_penalty),
                    )
                )

        if not next_states:
            return baseline_top_white_only_policy(case)

        next_states.sort(key=lambda item: item.rank_key)
        beam = next_states[: max(1, int(beam_width))]

    best_state = min(beam, key=lambda item: item.rank_key)
    return _template_sequence_to_schedules(
        case,
        best_state.prefix_choices,
        policy_name="setup_beam_search",
    )


def _report_rank_key(report: Any) -> tuple[int, int, float, int]:
    return (
        int(report.setup_change_cost),
        int(sum(report.active_filaments_per_absolute_layer)),
        float(report.tiny_presence_penalty),
        int(report.adjacent_layer_transition_cost),
    )


def setup_beam_guarded_policy(
    case: FixedBudgetScheduleCase,
    *,
    beam_width: int = _DEFAULT_BEAM_WIDTH,
) -> tuple[LayerSchedule, ...]:
    """Use beam search only when it beats baseline on the setup-aware score."""
    baseline = baseline_top_white_only_policy(case)
    baseline_report = evaluate_case_schedules(
        case,
        baseline,
        policy_name="baseline_top_white_only",
    )

    candidate = setup_beam_search_policy(case, beam_width=beam_width)
    candidate_report = evaluate_case_schedules(
        case,
        candidate,
        policy_name="setup_beam_search",
    )

    if _report_rank_key(candidate_report) < _report_rank_key(baseline_report):
        return candidate
    return baseline


def baseline_top_white_only_policy(
    case: FixedBudgetScheduleCase,
) -> tuple[LayerSchedule, ...]:
    """Control policy: fixed global order with all white on top."""
    return tuple(
        _fallback_schedule(
            entry.signature,
            case.ordering,
            policy_name="baseline_top_white_only",
        )
        for entry in case.entries
    )


def white_envelope_policy(
    case: FixedBudgetScheduleCase,
) -> tuple[LayerSchedule, ...]:
    """Keep color order fixed, but bury extra white below the color block."""
    return tuple(_white_envelope_schedule(entry.signature) for entry in case.entries)


def absorber_strength_global_order_policy(
    case: FixedBudgetScheduleCase,
    *,
    profiles: Mapping[str, dict] | None = None,
    profiles_dir: Path | None = None,
) -> tuple[LayerSchedule, ...]:
    """Use a strongest-first global color order with fixed top-white policy."""
    global_order = absorber_strength_order(
        case.ordering,
        case.layer_height_mm,
        profiles=profiles,
        profiles_dir=profiles_dir,
    )
    return tuple(
        _fallback_schedule(
            entry.signature,
            global_order,
            policy_name="absorber_strength_global_order",
        )
        for entry in case.entries
    )


def layer_sparse_greedy_policy(
    case: FixedBudgetScheduleCase,
) -> tuple[LayerSchedule, ...]:
    """Greedy case-level scheduler that aligns chroma by absolute layer.

    The heuristic is deliberately bounded and deterministic:

    - reserve the required top-white run per signature when possible
    - pick one globally preferred filament for each absolute layer based on
      remaining area-weighted demand
    - let signatures take that filament when they can
    - otherwise leave white, unless the signature is out of slack and must
      place some chroma in the current layer
    """
    global_priority_scores: dict[str, float] = defaultdict(float)
    for entry in case.entries:
        for fid, count in entry.signature.color_layers:
            global_priority_scores[fid] += float(entry.area_mm2) * int(count)

    global_priority = tuple(
        sorted(
            case.ordering,
            key=lambda fid: (
                -float(global_priority_scores.get(fid, 0.0)),
                case.ordering.index(fid),
                fid,
            ),
        )
    )

    remaining = [entry.signature.color_layer_map() for entry in case.entries]
    lower_slots = [
        entry.signature.total_layers - _top_white_layers_used(entry.signature)
        for entry in case.entries
    ]
    built_layers: list[list[str | None]] = [[] for _ in case.entries]
    max_lower_slots = max(lower_slots, default=0)

    for absolute_layer in range(max_lower_slots):
        layer_demand: dict[str, float] = defaultdict(float)
        for idx, entry in enumerate(case.entries):
            if absolute_layer >= lower_slots[idx]:
                continue
            for fid, count in remaining[idx].items():
                if count > 0:
                    layer_demand[fid] += float(entry.area_mm2) * int(count)

        chosen_fid = None
        if layer_demand:
            chosen_fid = min(
                layer_demand.keys(),
                key=lambda fid: (
                    -float(layer_demand[fid]),
                    global_priority.index(fid) if fid in global_priority else len(global_priority),
                    fid,
                ),
            )

        for idx, entry in enumerate(case.entries):
            if absolute_layer >= lower_slots[idx]:
                continue

            rem_counts = remaining[idx]
            remaining_total = int(sum(rem_counts.values()))
            slots_after = lower_slots[idx] - absolute_layer - 1
            must_place = remaining_total > slots_after

            assigned: str | None = None
            if chosen_fid is not None and rem_counts.get(chosen_fid, 0) > 0:
                assigned = chosen_fid
            elif must_place and remaining_total > 0:
                assigned = min(
                    (fid for fid, count in rem_counts.items() if count > 0),
                    key=lambda fid: (
                        -(int(rem_counts[fid])),
                        global_priority.index(fid) if fid in global_priority else len(global_priority),
                        fid,
                    ),
                )

            built_layers[idx].append(assigned)
            if assigned is not None:
                rem_counts[assigned] -= 1

    schedules: list[LayerSchedule] = []
    for idx, entry in enumerate(case.entries):
        signature = entry.signature
        lower = built_layers[idx]

        # Repair pass: if any chromatic budget remains, replace earliest lower
        # white slots so the final schedule still preserves totals exactly.
        unresolved = {fid: count for fid, count in remaining[idx].items() if count > 0}
        if unresolved:
            white_slots = [i for i, layer in enumerate(lower) if layer is None]
            cursor = 0
            for fid in global_priority:
                count = int(unresolved.get(fid, 0))
                for _ in range(count):
                    if cursor >= len(white_slots):
                        break
                    lower[white_slots[cursor]] = fid
                    cursor += 1
                    unresolved[fid] -= 1
            if any(count > 0 for count in unresolved.values()):
                schedules.append(
                    _fallback_schedule(
                        signature,
                        global_priority,
                        policy_name="layer_sparse_greedy",
                    )
                )
                continue

        top_white = [None] * _top_white_layers_used(signature)
        absolute_layers = tuple(lower + top_white)
        schedules.append(
            LayerSchedule(
                signature=signature,
                absolute_layers=absolute_layers,
                policy_name="layer_sparse_greedy",
            )
        )

    return tuple(schedules)


POLICY_REGISTRY: dict[str, SchedulingPolicy] = {
    "baseline_top_white_only": baseline_top_white_only_policy,
    "white_envelope": white_envelope_policy,
    "absorber_strength_global_order": absorber_strength_global_order_policy,
    "layer_sparse_greedy": layer_sparse_greedy_policy,
    "setup_beam_search": setup_beam_search_policy,
    "setup_beam_guarded": setup_beam_guarded_policy,
}


def get_policy_registry() -> dict[str, SchedulingPolicy]:
    """Return a deterministic copy of the policy registry."""
    return dict(POLICY_REGISTRY)


__all__ = [
    "POLICY_REGISTRY",
    "SchedulingPolicy",
    "absorber_strength_global_order_policy",
    "absorber_strength_order",
    "baseline_top_white_only_policy",
    "get_policy_registry",
    "layer_sparse_greedy_policy",
    "setup_beam_guarded_policy",
    "setup_beam_search_policy",
    "white_envelope_policy",
]
