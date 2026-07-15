from __future__ import annotations

import numpy as np

from pipeline.blueprint_triage import FixType, analyze_triage, apply_fixes, plan_fixes

from .conftest import make_context, make_split_plan, make_state


def test_apply_fixes_smooth_raises_low_side_with_linear_taper() -> None:
    plan = make_split_plan(shape=(5, 7), left_thickness_mm=0.24, right_thickness_mm=0.0)
    state = make_state(plan)
    report = analyze_triage(plan, make_context(state))
    candidate = next(candidate for candidate in plan_fixes(report) if candidate.fix_type is FixType.SMOOTH)

    original_caps = np.array(plan.cap_height_map, copy=True)
    target_mask = np.asarray(candidate.parameters["target_mask"], dtype=bool)
    seed_mask = np.asarray(candidate.parameters["seed_mask"], dtype=bool)
    meeting_top = float(candidate.parameters["meeting_top_mm"])
    cap_floor = np.asarray(candidate.parameters["cap_floor_map"], dtype=np.float32)

    apply_fixes(plan, [candidate])

    assert np.all(plan.cap_height_map[target_mask] >= original_caps[target_mask])
    assert np.allclose(plan.cap_height_map[~target_mask], original_caps[~target_mask])
    assert np.allclose(plan.cap_height_map[seed_mask] + cap_floor[seed_mask], meeting_top)
