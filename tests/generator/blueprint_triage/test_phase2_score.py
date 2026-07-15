from __future__ import annotations

import numpy as np

from pipeline.blueprint_triage import analyze_triage, plan_fixes

from .conftest import make_context, make_mask_plan, make_predicted_stack_target, make_state


def test_phase2_refinement_moves_noise_and_feature_scores_apart() -> None:
    mask = np.zeros((7, 7), dtype=bool)
    mask[2:4, 2:4] = True
    plan = make_mask_plan(mask, thickness_mm=0.20, cap_height_mm=0.08)

    noise_state = make_state(plan, solve_target_oklab=make_predicted_stack_target(plan.shape))
    noise_state.config.printer_min_line_width_mm = 0.20
    noise_state.diagnostics = {}
    noise_report = analyze_triage(plan, make_context(noise_state))
    noise_before = noise_report.skyscraper_regions[0].feature_salience_score
    plan_fixes(noise_report)
    noise_after = noise_report.skyscraper_regions[0].feature_salience_score

    feature_state = make_state(
        plan,
        solve_target_oklab=make_predicted_stack_target(plan.shape, highlight_mask=mask),
    )
    feature_state.config.printer_min_line_width_mm = 0.20
    feature_state.diagnostics = {}
    feature_report = analyze_triage(plan, make_context(feature_state))
    feature_before = feature_report.skyscraper_regions[0].feature_salience_score
    plan_fixes(feature_report)
    feature_after = feature_report.skyscraper_regions[0].feature_salience_score

    assert noise_after < noise_before
    assert feature_after >= feature_before
