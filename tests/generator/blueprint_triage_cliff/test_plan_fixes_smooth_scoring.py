from __future__ import annotations

import numpy as np

from pipeline.blueprint_triage import FixType, analyze_triage, plan_fixes

from .conftest import make_context, make_predicted_stack_target, make_split_plan, make_state


def _cliff_candidates(report):
    return [candidate for candidate in plan_fixes(report) if candidate.target is report.cliff_regions[0]]


def test_plan_fixes_smooth_and_hybrid_receive_real_counterfactual_scores() -> None:
    plan = make_split_plan(shape=(7, 7), left_thickness_mm=0.24, right_thickness_mm=0.0)
    highlight = np.zeros(plan.shape, dtype=bool)
    highlight[:, :3] = True
    state = make_state(
        plan,
        solve_target_oklab=make_predicted_stack_target(plan.shape, highlight_mask=highlight),
    )
    state.diagnostics = {}
    report = analyze_triage(plan, make_context(state))
    candidates = _cliff_candidates(report)

    smooth = next(candidate for candidate in candidates if candidate.fix_type is FixType.SMOOTH)
    hybrid = next(candidate for candidate in candidates if candidate.fix_type is FixType.HYBRID)
    shorten = next(candidate for candidate in candidates if candidate.fix_type is FixType.SHORTEN)

    assert smooth.preconditions_met
    assert hybrid.preconditions_met
    assert smooth.scores.image_cost >= 0.0
    assert hybrid.scores.image_cost >= 0.0
    assert smooth.scores.composite < shorten.scores.composite
