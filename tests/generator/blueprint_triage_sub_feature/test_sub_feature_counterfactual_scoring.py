from __future__ import annotations

import numpy as np

from pipeline.blueprint_triage import FixType, analyze_triage, plan_fixes
from tests.generator.blueprint_triage.conftest import make_context, make_mask_plan, make_oklab_target, make_state


def test_sub_feature_counterfactual_scoring() -> None:
    mask = np.zeros((7, 7), dtype=bool)
    mask[3, 3] = True
    plan = make_mask_plan(mask, thickness_mm=0.08, pitch_mm=0.08, cap_height_mm=0.0)
    state = make_state(
        plan,
        layer_height=0.08,
        solve_target_oklab=make_oklab_target(plan.shape, highlight_mask=mask, highlight_l=0.90),
    )
    report = analyze_triage(plan, make_context(state))

    candidate = next(
        candidate
        for candidate in plan_fixes(report)
        if candidate.fix_type is FixType.SHORTEN and candidate.preconditions_met
    )

    assert candidate.scores.image_cost > 0.0
