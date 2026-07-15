from __future__ import annotations

import numpy as np

from pipeline.blueprint_triage import FixType, analyze_triage, plan_fixes
from tests.generator.blueprint_triage.conftest import make_context, make_mask_plan, make_predicted_stack_target, make_state


def test_line_width_counterfactual_scoring_populates_image_cost() -> None:
    mask = np.zeros((9, 9), dtype=bool)
    mask[2:7, 4] = True
    plan = make_mask_plan(mask, thickness_mm=0.08, pitch_mm=0.08, cap_height_mm=0.0)
    state = make_state(
        plan,
        layer_height=0.08,
        solve_target_oklab=make_predicted_stack_target(plan.shape, highlight_mask=mask, thickness_mm=0.08, cap_height_mm=0.0),
    )
    state.diagnostics = {}
    report = analyze_triage(plan, make_context(state))

    candidates = [
        candidate
        for candidate in plan_fixes(report)
        if candidate.fix_type is FixType.SHORTEN and candidate.target is report.narrow_strands[0]
    ]

    assert candidates
    assert candidates[0].scores.image_cost > 0.0
