from __future__ import annotations

import numpy as np

from pipeline.blueprint_triage import FixType, analyze_triage, apply_fixes, plan_fixes

from .conftest import make_context, make_split_plan, make_state


def test_apply_fixes_hybrid_reassigns_high_side_and_smooths_low_side() -> None:
    plan = make_split_plan(shape=(5, 7), left_thickness_mm=0.24, right_thickness_mm=0.0)
    state = make_state(plan)
    report = analyze_triage(plan, make_context(state))
    candidate = next(candidate for candidate in plan_fixes(report) if candidate.fix_type is FixType.HYBRID)

    original_caps = np.array(plan.cap_height_map, copy=True)
    original_stacks = np.array(plan.segment_stack_id, copy=True)
    shorten_mask = np.asarray(candidate.parameters["shorten_target_mask"], dtype=bool)
    smooth_mask = np.asarray(candidate.parameters["smooth_target_mask"], dtype=bool)
    replacement_stack_id = int(candidate.parameters["replacement_stack_id"])

    apply_fixes(plan, [candidate])

    changed_segments = np.unique(plan.segment_id_map[shorten_mask])
    assert np.all(plan.segment_stack_id[changed_segments] == replacement_stack_id)
    assert np.any(plan.cap_height_map[smooth_mask] > original_caps[smooth_mask])
    assert np.any(plan.segment_stack_id != original_stacks)
