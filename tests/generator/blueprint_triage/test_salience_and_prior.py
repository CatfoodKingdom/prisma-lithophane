from __future__ import annotations

import numpy as np

from tests.generator.blueprint_triage.conftest import make_context, make_mask_plan, make_state


def test_salience_and_fidelity_prior_follow_target_edges():
    plan = make_mask_plan(np.ones((4, 4), dtype=bool), thickness_mm=0.16, pitch_mm=0.20, cap_height_mm=0.0)
    source_image_oklab = np.zeros((4, 4, 3), dtype=np.float32)
    source_image_oklab[:, 2:, 0] = 1.0
    de_map = np.full(plan.shape, 0.1, dtype=np.float32)
    state = make_state(
        plan,
        solve_target_oklab=source_image_oklab,
        de_map=de_map,
    )

    context = make_context(state)

    assert float(context.salience.luminance_gradient.max()) > 0.0
    assert float(context.salience.combined_gradient.max()) >= float(context.salience.luminance_gradient.max())
    assert context.fidelity_prior[1, 0] < context.fidelity_prior[1, 1]


def test_salience_and_fidelity_prior_zero_fill_without_source_image():
    plan = make_mask_plan(np.ones((4, 4), dtype=bool), thickness_mm=0.16, pitch_mm=0.20, cap_height_mm=0.0)
    state = make_state(plan, de_map=np.full(plan.shape, 0.2, dtype=np.float32))
    state.solve_target_oklab = None

    context = make_context(state)

    assert not context.salience.luminance_gradient.any()
    assert not context.salience.chroma_gradient.any()
    assert not context.salience.combined_gradient.any()
    assert not context.fidelity_prior.any()
