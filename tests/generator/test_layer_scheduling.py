from __future__ import annotations

from types import SimpleNamespace

import numpy as np

from pipeline.solved_material_plan import SolvedMaterialPlan, StackDefinition
from scheduling import (
    FixedBudgetCaseEntry,
    FixedBudgetScheduleCase,
    LayerBudgetSignature,
    LayerSchedule,
    absorber_strength_order,
    build_schedule_case,
    build_swap_pressure_case,
    build_synthetic_equal_budget_case,
    budget_preservation_deltas,
    derive_anisotropic_white_surface_reserve,
    derive_fixed_budget_schedule_case,
    evaluate_case_schedules,
    evaluate_policy,
    quantize_thickness_to_layers,
    setup_beam_guarded_policy,
    setup_beam_search_policy,
    setup_change_cost,
    top_white_reserve_ok,
    white_envelope_policy,
)


def _manual_same_budget_case() -> FixedBudgetScheduleCase:
    ordering = (
        "bambu-basic-yellow",
        "bambu-basic-cyan",
        "bambu-basic-magenta",
    )
    entries = []
    for idx in range(3):
        signature = LayerBudgetSignature(
            stack_index=idx,
            ordering=ordering,
            color_layers=(
                ("bambu-basic-yellow", 1),
                ("bambu-basic-cyan", 1),
                ("bambu-basic-magenta", 1),
            ),
            white_layers=4,
            top_white_reserved_layers=3,
        )
        entries.append(
            FixedBudgetCaseEntry(
                signature=signature,
                pixel_count=100,
                area_mm2=4.0,
            )
        )
    return FixedBudgetScheduleCase(
        ordering=ordering,
        layer_height_mm=0.08,
        d_wc_min_mm=0.24,
        top_white_reserved_layers=3,
        entries=tuple(entries),
        image_shape=(20, 15),
        solver_fine_pitch_mm=0.2,
    )


def _manual_schedules(
    case: FixedBudgetScheduleCase,
    color_orders: tuple[tuple[str, ...], ...],
) -> tuple[LayerSchedule, ...]:
    schedules = []
    for entry, color_order in zip(case.entries, color_orders):
        absolute_layers = tuple(list(color_order) + [None, None, None, None])
        schedules.append(
            LayerSchedule(
                signature=entry.signature,
                absolute_layers=absolute_layers,
                policy_name="manual",
            )
        )
    return tuple(schedules)


def test_quantize_thickness_to_layers_rejects_nonquantized_nonzero_thickness() -> None:
    assert quantize_thickness_to_layers(0.16, 0.08) == 2
    try:
        quantize_thickness_to_layers(0.081, 0.08)
    except ValueError as exc:
        assert "does not quantize cleanly" in str(exc)
    else:
        raise AssertionError("Expected quantize_thickness_to_layers() to reject 0.081 mm")


def test_derive_schedule_case_does_not_mutate_plan() -> None:
    segment_id_map = np.array([[0, 0], [1, 1]], dtype=np.int32)
    segment_stack_id = np.array([0, 1], dtype=np.int32)
    stack_table = (
        StackDefinition.from_mapping({"bambu-basic-yellow": 0.08}),
        StackDefinition.from_mapping(
            {
                "bambu-basic-yellow": 0.08,
                "bambu-basic-cyan": 0.16,
            }
        ),
    )
    cap_map = np.array([[0.24, 0.24], [0.16, 0.16]], dtype=np.float32)
    plan = SolvedMaterialPlan(
        image_domain_width_mm=0.4,
        image_domain_height_mm=0.4,
        image_sample_pitch_mm=0.2,
        solver_fine_pitch_mm=0.2,
        color_region_target_mm=0.2,
        segment_id_map=segment_id_map.copy(),
        segment_stack_id=segment_stack_id.copy(),
        stack_table=stack_table,
        cap_height_map=cap_map.copy(),
    )
    before_segment_id_map = plan.segment_id_map.copy()
    before_segment_stack_id = plan.segment_stack_id.copy()
    before_cap_map = plan.cap_height_map.copy()

    case = derive_fixed_budget_schedule_case(
        plan,
        ("bambu-basic-yellow", "bambu-basic-cyan"),
        0.08,
        0.08,
    )

    assert len(case.entries) == 2
    assert np.array_equal(plan.segment_id_map, before_segment_id_map)
    assert np.array_equal(plan.segment_stack_id, before_segment_stack_id)
    assert np.array_equal(plan.cap_height_map, before_cap_map)


def test_derive_anisotropic_white_surface_reserve_marks_only_cliff_band_pixels() -> None:
    total_layers = np.array([[1, 3, 3, 3, 1]], dtype=np.int32)
    reserve, band = derive_anisotropic_white_surface_reserve(total_layers, band_width_px=1)

    assert reserve.tolist() == [[0, 2, 0, 2, 0]]
    assert band.tolist() == [[False, True, False, True, False]]


def test_schedule_case_splits_sidewall_band_without_scaling_band_width_with_top_reserve() -> None:
    segment_id_map = np.array([[0, 1, 2, 3, 4]], dtype=np.int32)
    segment_stack_id = np.array([0, 1, 1, 1, 0], dtype=np.int32)
    stack_table = (
        StackDefinition.from_mapping({}),
        StackDefinition.from_mapping({"bambu-basic-yellow": 0.16}),
    )
    cap_map = np.full((1, 5), 0.24, dtype=np.float32)
    plan = SolvedMaterialPlan(
        image_domain_width_mm=0.5,
        image_domain_height_mm=0.1,
        image_sample_pitch_mm=0.1,
        solver_fine_pitch_mm=0.1,
        color_region_target_mm=0.1,
        segment_id_map=segment_id_map,
        segment_stack_id=segment_stack_id,
        stack_table=stack_table,
        cap_height_map=cap_map,
    )

    case_low = derive_fixed_budget_schedule_case(
        plan,
        ("bambu-basic-yellow",),
        0.08,
        0.08,
        top_white_reserved_layers=1,
        min_printable_line_width_mm=0.1,
    )
    case_high = derive_fixed_budget_schedule_case(
        plan,
        ("bambu-basic-yellow",),
        0.08,
        0.08,
        top_white_reserved_layers=3,
        min_printable_line_width_mm=0.1,
    )

    low_sidewall = [entry for entry in case_low.entries if entry.signature.sidewall_band]
    high_sidewall = [entry for entry in case_high.entries if entry.signature.sidewall_band]

    assert len(low_sidewall) == len(high_sidewall) == 1
    assert low_sidewall[0].pixel_count == high_sidewall[0].pixel_count == 2
    assert low_sidewall[0].signature.sidewall_white_reserved_layers == 2
    assert high_sidewall[0].signature.sidewall_white_reserved_layers == 2
    assert low_sidewall[0].signature.top_white_reserved_layers == 2
    assert high_sidewall[0].signature.top_white_reserved_layers == 3


def test_white_envelope_preserves_budgets_and_top_white_reserve() -> None:
    case = build_synthetic_equal_budget_case()
    schedules = white_envelope_policy(case)
    report = evaluate_case_schedules(case, schedules, policy_name="white_envelope")

    assert report.budget_preserved
    assert report.top_white_reserved_ok
    first_schedule = schedules[0]
    assert first_schedule.absolute_layers[0] is None
    assert first_schedule.absolute_layers[-3:] == (None, None, None)
    assert budget_preservation_deltas(case.entries[0].signature, first_schedule) == ()
    assert top_white_reserve_ok(case.entries[0].signature, first_schedule)


def test_absorber_strength_order_is_deterministic() -> None:
    ordering = (
        "bambu-basic-yellow",
        "bambu-basic-cyan",
        "bambu-basic-magenta",
    )
    first = absorber_strength_order(ordering, 0.08)
    second = absorber_strength_order(ordering, 0.08)
    assert first == second
    assert set(first) == set(ordering)


def test_same_budget_case_prefers_aligned_ordering_over_permuted_ordering() -> None:
    case = _manual_same_budget_case()
    permuted = _manual_schedules(
        case,
        (
            ("bambu-basic-cyan", "bambu-basic-yellow", "bambu-basic-magenta"),
            ("bambu-basic-yellow", "bambu-basic-magenta", "bambu-basic-cyan"),
            ("bambu-basic-magenta", "bambu-basic-cyan", "bambu-basic-yellow"),
        ),
    )
    aligned = _manual_schedules(
        case,
        (
            ("bambu-basic-cyan", "bambu-basic-yellow", "bambu-basic-magenta"),
            ("bambu-basic-cyan", "bambu-basic-yellow", "bambu-basic-magenta"),
            ("bambu-basic-cyan", "bambu-basic-yellow", "bambu-basic-magenta"),
        ),
    )

    permuted_report = evaluate_case_schedules(case, permuted, policy_name="permuted")
    aligned_report = evaluate_case_schedules(case, aligned, policy_name="aligned")

    aligned_active = sum(aligned_report.active_filaments_per_absolute_layer)
    permuted_active = sum(permuted_report.active_filaments_per_absolute_layer)
    assert aligned_active < permuted_active

    aligned_score = 0.35 * aligned_active + 0.30 * aligned_report.adjacent_layer_transition_cost
    permuted_score = 0.35 * permuted_active + 0.30 * permuted_report.adjacent_layer_transition_cost
    assert aligned_score < permuted_score

    assert setup_change_cost(case, aligned) < setup_change_cost(case, permuted)


def test_runner_facing_api_supports_synthetic_case() -> None:
    payload = {
        "case_id": "synthetic_fixture",
        "source_kind": "synthetic",
    }
    case = build_schedule_case(payload)
    result = evaluate_policy(case, "baseline")

    assert case.entries
    assert result["budget_ok"] is True
    assert result["top_white_ok"] is True
    assert result["schedule_length"] > 0
    assert result["active_count"] is not None
    assert result["setup_change_cost"] is not None


def test_runner_facing_api_supports_swap_pressure_case() -> None:
    payload = {
        "case_id": "synthetic_swap_pressure",
        "source_kind": "synthetic",
    }
    case = build_schedule_case(payload)
    direct = build_swap_pressure_case()

    assert len(case.entries) == len(direct.entries)
    assert case.entries[0].signature.white_layers == direct.entries[0].signature.white_layers


def test_build_schedule_case_prefers_min_line_width_over_nominal_and_nozzle_diameter() -> None:
    segment_id_map = np.array([[0]], dtype=np.int32)
    segment_stack_id = np.array([0], dtype=np.int32)
    stack_table = (StackDefinition.from_mapping({"bambu-basic-yellow": 0.08}),)
    cap_map = np.array([[0.08]], dtype=np.float32)
    plan = SolvedMaterialPlan(
        image_domain_width_mm=0.2,
        image_domain_height_mm=0.2,
        image_sample_pitch_mm=0.2,
        solver_fine_pitch_mm=0.2,
        color_region_target_mm=0.2,
        segment_id_map=segment_id_map,
        segment_stack_id=segment_stack_id,
        stack_table=stack_table,
        cap_height_map=cap_map,
    )
    solve_result = SimpleNamespace(
        solved_plan=plan,
        config=SimpleNamespace(
            palette=["bambu-basic-yellow"],
            layer_height=0.08,
            d_wc_min=0.08,
            nozzle_diameter=0.20,
        ),
    )

    case = build_schedule_case(
        {
            "case_id": "line_width_precedence",
            "solve_result": solve_result,
            "ordering": ["bambu-basic-yellow"],
            "min_printable_line_width_mm": 0.16,
            "line_width_mm": 0.30,
            "nozzle_diameter_mm": 0.20,
        }
    )

    assert case.min_printable_line_width_mm == 0.16
    assert case.sidewall_band_width_px == 1


def test_setup_beam_search_preserves_budgets_and_improves_swap_fixture() -> None:
    case = build_swap_pressure_case()
    baseline = evaluate_case_schedules(
        case,
        evaluate_policy(case, "baseline")["schedules"],
        policy_name="baseline",
    )
    search = evaluate_case_schedules(
        case,
        setup_beam_search_policy(case),
        policy_name="setup_beam_search",
    )

    assert search.budget_preserved
    assert search.top_white_reserved_ok
    assert search.setup_change_cost <= baseline.setup_change_cost
    assert sum(search.active_filaments_per_absolute_layer) <= sum(
        baseline.active_filaments_per_absolute_layer
    )


def test_setup_beam_guarded_never_underperforms_baseline_on_swap_fixture() -> None:
    case = build_swap_pressure_case()
    baseline = evaluate_case_schedules(
        case,
        evaluate_policy(case, "baseline")["schedules"],
        policy_name="baseline",
    )
    guarded = evaluate_case_schedules(
        case,
        setup_beam_guarded_policy(case),
        policy_name="setup_beam_guarded",
    )

    baseline_key = (
        baseline.setup_change_cost,
        sum(baseline.active_filaments_per_absolute_layer),
        baseline.tiny_presence_penalty,
        baseline.adjacent_layer_transition_cost,
    )
    guarded_key = (
        guarded.setup_change_cost,
        sum(guarded.active_filaments_per_absolute_layer),
        guarded.tiny_presence_penalty,
        guarded.adjacent_layer_transition_cost,
    )
    assert guarded.budget_preserved
    assert guarded.top_white_reserved_ok
    assert guarded_key <= baseline_key
