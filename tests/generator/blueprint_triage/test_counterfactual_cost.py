from __future__ import annotations

import numpy as np

from pipeline.blueprint_triage import FixType, counterfactual_cost

from .conftest import make_context, make_mask_plan, make_predicted_stack_target, make_state


def test_counterfactual_cost_distinguishes_noise_vs_feature_flatten() -> None:
    mask = np.zeros((7, 7), dtype=bool)
    mask[2:4, 2:4] = True
    plan = make_mask_plan(mask, thickness_mm=0.20, cap_height_mm=0.08)

    cheap_state = make_state(plan, solve_target_oklab=make_predicted_stack_target(plan.shape))
    cheap_state.diagnostics = {}
    cheap_context = make_context(cheap_state)
    cheap_cost = counterfactual_cost(
        plan,
        cheap_context,
        fix_type=FixType.SHORTEN,
        parameters={"target_mask": mask, "replacement_stack_id": 0},
        target_pixels=mask,
    )

    expensive_state = make_state(
        plan,
        solve_target_oklab=make_predicted_stack_target(plan.shape, highlight_mask=mask),
    )
    expensive_state.diagnostics = {}
    expensive_context = make_context(expensive_state)
    expensive_cost = counterfactual_cost(
        plan,
        expensive_context,
        fix_type=FixType.SHORTEN,
        parameters={"target_mask": mask, "replacement_stack_id": 0},
        target_pixels=mask,
    )

    assert cheap_cost < 0.01
    assert expensive_cost > cheap_cost
