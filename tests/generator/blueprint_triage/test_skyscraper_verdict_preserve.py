from __future__ import annotations

import numpy as np

from pipeline.blueprint_triage import ExposureContext, Verdict, analyze_triage

from .conftest import make_context, make_mask_plan, make_state


def test_skyscraper_does_not_add_blocking_verdict_on_its_own() -> None:
    mask = np.zeros((7, 7), dtype=bool)
    mask[2:4, 2:4] = True
    plan = make_mask_plan(mask, thickness_mm=0.20, cap_height_mm=0.08)
    target = np.full((*plan.shape, 3), (0.45, 0.0, 0.0), dtype=np.float32)
    target[mask, 0] = 0.80
    state = make_state(
        plan,
        solve_target_oklab=target.reshape((-1, 3)),
        de_map=np.full(plan.shape, 0.01, dtype=np.float32),
    )
    state.config.printer_min_line_width_mm = 0.20
    context = make_context(state, exposure=ExposureContext(exempt_outer_perimeter=True))

    report = analyze_triage(plan, context)

    assert report.summary.skyscraper_region_count == 1
    assert report.verdict is Verdict.WARNINGS
