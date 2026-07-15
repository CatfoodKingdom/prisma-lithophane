from __future__ import annotations

import numpy as np

from pipeline.blueprint_triage import FixType, analyze_triage, plan_fixes

from .conftest import make_context, make_mask_plan, make_predicted_stack_target, make_state


def _skyscraper_candidates(report):
    return [candidate for candidate in plan_fixes(report) if candidate.target is report.skyscraper_regions[0]]


def test_plan_fixes_skyscraper_adds_bulldoze_and_ranks_it_by_salience() -> None:
    mask = np.zeros((7, 7), dtype=bool)
    mask[2:4, 2:4] = True
    plan = make_mask_plan(mask, thickness_mm=0.20, cap_height_mm=0.08)

    noise_state = make_state(plan, solve_target_oklab=make_predicted_stack_target(plan.shape))
    noise_state.config.printer_min_line_width_mm = 0.20
    noise_state.diagnostics = {}
    noise_report = analyze_triage(plan, make_context(noise_state))
    noise_candidates = _skyscraper_candidates(noise_report)
    assert {candidate.fix_type for candidate in noise_candidates} == {FixType.ACCEPT, FixType.BULLDOZE}
    assert noise_candidates[0].fix_type is FixType.BULLDOZE

    feature_state = make_state(
        plan,
        solve_target_oklab=make_predicted_stack_target(plan.shape, highlight_mask=mask),
    )
    feature_state.config.printer_min_line_width_mm = 0.20
    feature_state.diagnostics = {}
    feature_report = analyze_triage(plan, make_context(feature_state))
    feature_candidates = _skyscraper_candidates(feature_report)
    assert feature_candidates[0].fix_type is FixType.ACCEPT
