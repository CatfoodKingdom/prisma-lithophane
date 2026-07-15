from __future__ import annotations

import numpy as np

from pipeline.blueprint_triage import FixType, analyze_triage, plan_fixes

from .conftest import make_context, make_mask_plan, make_predicted_stack_target, make_state


def _cliff_candidates(report):
    return [candidate for candidate in plan_fixes(report) if candidate.target is report.cliff_regions[0]]


def test_plan_fixes_cliff_emits_ranked_candidates_and_scores_them() -> None:
    mask = np.zeros((7, 7), dtype=bool)
    mask[2:4, 2:4] = True
    plan = make_mask_plan(mask, thickness_mm=0.20, cap_height_mm=0.08)

    cheap_state = make_state(plan, solve_target_oklab=make_predicted_stack_target(plan.shape))
    cheap_state.diagnostics = {}
    cheap_report = analyze_triage(plan, make_context(cheap_state))
    cheap_candidates = _cliff_candidates(cheap_report)

    fix_types = {candidate.fix_type for candidate in cheap_candidates}
    assert fix_types.issuperset({FixType.ACCEPT, FixType.SHORTEN, FixType.WALL, FixType.SMOOTH, FixType.HYBRID})
    for candidate in cheap_candidates:
        assert candidate.scores.composite == candidate.scores.composite
        assert candidate.rank >= 1

    cheap_shorten = next(candidate for candidate in cheap_candidates if candidate.fix_type.value == "shorten")
    cheap_wall = next(candidate for candidate in cheap_candidates if candidate.fix_type.value == "wall")
    assert cheap_shorten.scores.composite < cheap_wall.scores.composite

    feature_state = make_state(
        plan,
        solve_target_oklab=make_predicted_stack_target(plan.shape, highlight_mask=mask),
    )
    feature_state.diagnostics = {}
    feature_report = analyze_triage(plan, make_context(feature_state))
    feature_candidates = _cliff_candidates(feature_report)
    feature_shorten = next(candidate for candidate in feature_candidates if candidate.fix_type.value == "shorten")
    feature_wall = next(candidate for candidate in feature_candidates if candidate.fix_type.value == "wall")
    assert feature_shorten.scores.composite > feature_wall.scores.composite
