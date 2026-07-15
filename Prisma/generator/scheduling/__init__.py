"""Staged post-solve layer scheduling package."""
from __future__ import annotations

from typing import Any, Mapping

from .metrics import (
    active_families_per_absolute_layer,
    active_family_sets_by_absolute_layer,
    active_filament_sets_by_absolute_layer,
    active_filaments_per_absolute_layer,
    adjacent_layer_transition_cost,
    adjacent_layer_filament_set_transition_cost,
    budget_preservation_deltas,
    budget_preserved,
    evaluate_case_schedules,
    evaluate_schedules,
    family_area_by_absolute_layer_mm2,
    filament_area_by_absolute_layer_mm2,
    setup_change_cost,
    tiny_presence_penalty,
    top_white_reserve_ok,
)
from .model import (
    QUANTIZATION_TOLERANCE_MM,
    WHITE_LAYER_TOKEN,
    FixedBudgetCaseEntry,
    FixedBudgetEvaluationCase,
    FixedBudgetScheduleCase,
    LayerBudgetSignature,
    LayerSchedule,
    ScheduleEvaluation,
    build_synthetic_equal_budget_case,
    build_swap_pressure_case,
    derive_fixed_budget_evaluation_case,
    derive_fixed_budget_schedule_case,
    derive_anisotropic_white_surface_reserve,
    derive_schedule_case,
    derive_unique_layer_budget_signatures,
    quantize_budget_layers,
    quantize_thickness_to_layers,
)
from .policies import (
    POLICY_REGISTRY,
    absorber_strength_global_order_policy,
    absorber_strength_order,
    baseline_top_white_only_policy,
    get_policy_registry,
    layer_sparse_greedy_policy,
    setup_beam_guarded_policy,
    setup_beam_search_policy,
    white_envelope_policy,
)

POLICY_NAME_ALIASES = {
    "baseline": "baseline_top_white_only",
    "baseline_top_white_only": "baseline_top_white_only",
    "white_envelope": "white_envelope",
    "absorber_strength_order": "absorber_strength_global_order",
    "absorber_strength_global_order": "absorber_strength_global_order",
    "layer_sparse_greedy": "layer_sparse_greedy",
    "setup_beam_guarded": "setup_beam_guarded",
    "setup_beam_search": "setup_beam_search",
}

ScheduleCase = FixedBudgetScheduleCase


def build_schedule_case(payload: Mapping[str, Any]) -> FixedBudgetScheduleCase:
    source_kind = str(payload.get("source_kind") or "")
    case_id = str(payload.get("case_id") or "schedule_case")
    if case_id in {"synthetic_swap_pressure", "swap_pressure_fixture"}:
        return build_swap_pressure_case()
    if source_kind == "synthetic" or case_id == "synthetic_fixture":
        return build_synthetic_equal_budget_case()

    solve_result = payload.get("solve_result") or payload.get("result")
    if solve_result is None or getattr(solve_result, "solved_plan", None) is None:
        raise ValueError("build_schedule_case() requires solve_result.solved_plan")

    ordering = tuple(
        str(fid)
        for fid in (
            payload.get("ordering")
            or payload.get("palette")
            or getattr(solve_result.config, "palette", ())
        )
    )
    if not ordering:
        raise ValueError("build_schedule_case() requires a non-empty ordering/palette")

    return derive_fixed_budget_schedule_case(
        solve_result.solved_plan,
        ordering,
        float(payload.get("layer_height_mm") or solve_result.config.layer_height),
        float(payload.get("d_wc_min_mm") or solve_result.config.d_wc_min),
        min_printable_line_width_mm=float(
            payload.get("min_printable_line_width_mm")
            or payload.get("line_width_mm")
            or payload.get("nozzle_diameter_mm")
            or payload.get("nozzle_diameter")
            or solve_result.config.nozzle_diameter
        ),
    )


build_case = build_schedule_case


def get_policy(policy_name: str):
    canonical = POLICY_NAME_ALIASES.get(str(policy_name), str(policy_name))
    try:
        return POLICY_REGISTRY[canonical]
    except KeyError as exc:
        raise KeyError(f"Unknown scheduling policy: {policy_name!r}") from exc


def evaluate_policy(
    case: FixedBudgetScheduleCase,
    policy_name: str,
) -> dict[str, Any]:
    policy = get_policy(policy_name)
    schedules = policy(case)
    report = evaluate_case_schedules(case, schedules, policy_name=str(policy_name))
    return {
        "policy_name": str(policy_name),
        "active_count": float(sum(report.active_filaments_per_absolute_layer)),
        "setup_change_cost": float(report.setup_change_cost),
        "transition_cost": float(report.adjacent_layer_transition_cost),
        "tiny_presence_penalty": float(report.tiny_presence_penalty),
        "top_white_penalty": 0.0 if report.top_white_reserved_ok else 1.0,
        "printability_proxy": 0.0,
        "schedule_length": max((schedule.total_layers for schedule in report.schedules), default=0),
        "budget_ok": bool(report.budget_preserved),
        "top_white_ok": bool(report.top_white_reserved_ok),
        "budget_deltas_by_signature": report.budget_deltas_by_signature,
        "top_white_reserved_ok_by_signature": report.top_white_reserved_ok_by_signature,
        "schedules": report.schedules,
        "metadata": {
            "hard_invalid": (not report.budget_preserved) or (not report.top_white_reserved_ok),
            "active_filaments_per_absolute_layer": list(report.active_filaments_per_absolute_layer),
            "active_families_per_absolute_layer": list(report.active_families_per_absolute_layer),
            "setup_change_cost": int(report.setup_change_cost),
            "adjacent_layer_transition_cost": int(report.adjacent_layer_transition_cost),
        },
    }


__all__ = [
    "POLICY_NAME_ALIASES",
    "POLICY_REGISTRY",
    "QUANTIZATION_TOLERANCE_MM",
    "ScheduleCase",
    "WHITE_LAYER_TOKEN",
    "FixedBudgetCaseEntry",
    "FixedBudgetEvaluationCase",
    "FixedBudgetScheduleCase",
    "LayerBudgetSignature",
    "LayerSchedule",
    "ScheduleEvaluation",
    "absorber_strength_global_order_policy",
    "absorber_strength_order",
    "active_families_per_absolute_layer",
    "active_family_sets_by_absolute_layer",
    "active_filament_sets_by_absolute_layer",
    "active_filaments_per_absolute_layer",
    "adjacent_layer_transition_cost",
    "adjacent_layer_filament_set_transition_cost",
    "baseline_top_white_only_policy",
    "build_case",
    "build_schedule_case",
    "build_synthetic_equal_budget_case",
    "build_swap_pressure_case",
    "budget_preservation_deltas",
    "budget_preserved",
    "derive_fixed_budget_evaluation_case",
    "derive_anisotropic_white_surface_reserve",
    "derive_fixed_budget_schedule_case",
    "derive_schedule_case",
    "derive_unique_layer_budget_signatures",
    "evaluate_case_schedules",
    "evaluate_policy",
    "evaluate_schedules",
    "family_area_by_absolute_layer_mm2",
    "filament_area_by_absolute_layer_mm2",
    "get_policy",
    "get_policy_registry",
    "layer_sparse_greedy_policy",
    "setup_beam_guarded_policy",
    "quantize_budget_layers",
    "quantize_thickness_to_layers",
    "setup_beam_search_policy",
    "setup_change_cost",
    "tiny_presence_penalty",
    "top_white_reserve_ok",
    "white_envelope_policy",
]
