from __future__ import annotations

import numpy as np

from pipeline.blueprint_triage import ExposureContext
from pipeline.blueprint_triage.detect.cliffs import detect_cliffs
from tests.generator.blueprint_triage.conftest import make_context, make_mask_plan, make_split_plan, make_state


def test_detect_cliffs_respects_outer_perimeter_exemption():
    plan = make_mask_plan(np.ones((1, 1), dtype=bool), thickness_mm=0.16, pitch_mm=0.20, cap_height_mm=0.08)

    exempted = detect_cliffs(make_context(make_state(plan), exposure=ExposureContext(exempt_outer_perimeter=True)))
    unexempted = detect_cliffs(make_context(make_state(plan), exposure=ExposureContext(exempt_outer_perimeter=False)))

    assert exempted == []
    assert len(unexempted) == 1


def test_detect_cliffs_respects_internal_edge_exemption_mask():
    plan = make_split_plan(shape=(1, 2), left_thickness_mm=0.16, right_thickness_mm=0.0, cap_height_mm=0.08)
    exempt_edge_mask = np.zeros((1, 2, 4), dtype=bool)
    exempt_edge_mask[0, 0, 3] = True

    regions = detect_cliffs(
        make_context(
            make_state(plan),
            exposure=ExposureContext(
                exempt_outer_perimeter=True,
                exempt_edge_mask=exempt_edge_mask,
            ),
        )
    )

    assert regions == []
