from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass

import numpy as np
import pytest

from evaluation.solve_equivalence import (
    assert_snapshots_equal,
    fingerprint_sections,
    snapshot_differences,
    solve_equivalence_snapshot,
)
from facade import SolveConfig, SolveResult, SolveStats
from pipeline.solved_material_plan import SolvedMaterialPlan, StackDefinition


def _result() -> SolveResult:
    segment_ids = np.array([[0, 0], [1, 1]], dtype=np.int32)
    solved_plan = SolvedMaterialPlan(
        image_domain_width_mm=0.4,
        image_domain_height_mm=0.4,
        image_sample_pitch_mm=0.2,
        solver_fine_pitch_mm=0.2,
        color_region_target_mm=0.4,
        segment_id_map=segment_ids,
        segment_stack_id=np.array([0, 1], dtype=np.int32),
        stack_table=(
            StackDefinition.from_mapping({"red": 0.08}),
            StackDefinition.from_mapping({"red": 0.16}),
        ),
        cap_height_map=np.full((2, 2), 0.08, dtype=np.float32),
    )
    return SolveResult(
        thickness_maps={
            "red": np.array([[0.08, 0.08], [0.16, 0.16]], dtype=np.float32),
            "__white_cap__": np.full((2, 2), 0.08, dtype=np.float32),
        },
        color_profiles={},
        wb_profile={},
        wc_profile={},
        stats=SolveStats(
            mean_de=0.01,
            max_de=0.02,
            n_out_of_gamut=0,
            total_pixels=4,
            image_w=2,
            image_h=2,
            coverage_pct=100.0,
            max_height=0.32,
        ),
        config=SolveConfig(palette=["red"], white_base="white"),
        solved_plan=solved_plan,
    )


def test_snapshot_is_stable_for_identical_result() -> None:
    first = solve_equivalence_snapshot(_result())
    second = solve_equivalence_snapshot(_result())
    assert_snapshots_equal(first, second)
    assert snapshot_differences(first, second) == []


def test_snapshot_detects_one_pixel_output_change() -> None:
    result = _result()
    before = solve_equivalence_snapshot(result)
    result.thickness_maps["red"][0, 0] += np.float32(0.08)
    after = solve_equivalence_snapshot(result)

    assert snapshot_differences(before, after) == [
        "changed section: thickness_maps",
        "changed section: thickness_maps.red",
    ]
    with pytest.raises(AssertionError, match="thickness_maps"):
        assert_snapshots_equal(before, after)


def test_snapshot_detects_one_stack_assignment_change() -> None:
    result = _result()
    before = solve_equivalence_snapshot(result)
    result.solved_plan.segment_stack_id[0] = 1
    after = solve_equivalence_snapshot(result)

    assert snapshot_differences(before, after) == ["changed section: solved_plan"]


def test_timing_fields_are_excluded_but_stable_counters_are_not() -> None:
    first = fingerprint_sections(
        {
            "profile": {
                "timings_s": {"stage": 1.0},
                "blueprint_printability_runtime_s": 2.0,
                "cache_hit": False,
                "zone_count": 12,
            }
        }
    )
    timing_changed = fingerprint_sections(
        {
            "profile": {
                "timings_s": {"stage": 99.0},
                "blueprint_printability_runtime_s": 88.0,
                "cache_hit": True,
                "zone_count": 12,
            }
        }
    )
    assert_snapshots_equal(first, timing_changed)

    counter_changed_payload = deepcopy(
        {
            "profile": {
                "timings_s": {"stage": 1.0},
                "blueprint_printability_runtime_s": 2.0,
                "cache_hit": False,
                "zone_count": 13,
            }
        }
    )
    counter_changed = fingerprint_sections(counter_changed_payload)
    assert snapshot_differences(first, counter_changed) == ["changed section: profile"]


def test_printability_ledger_is_classified_as_diagnostic_only() -> None:
    class _PerformanceProfile:
        timings_s = {}
        counters = {
            "stage2_zone_count": 2,
            "stage2_printability_ledger_final_total_hard_pixels": 0,
        }

    @dataclass
    class _VisiblePlan:
        recipe_label_map: np.ndarray

    class _Staged:
        performance_profile = _PerformanceProfile()
        visible_plan = _VisiblePlan(np.zeros((2, 2), dtype=np.int32))
        compiled_directives = None
        receipts = None
        planning_diagnostics = None
        lateral_zone_plan = None
        filler_plan = None
        cap_plan = None
        compatibility_bundle = None

    result = _result()
    result.staged_result = _Staged()
    first = solve_equivalence_snapshot(result)
    _Staged.performance_profile.counters[
        "stage2_printability_ledger_final_total_hard_pixels"
    ] = 99
    assert_snapshots_equal(first, solve_equivalence_snapshot(result))

    _Staged.performance_profile.counters["stage2_zone_count"] = 3
    assert snapshot_differences(first, solve_equivalence_snapshot(result)) == [
        "changed section: staged_stable_counters"
    ]
