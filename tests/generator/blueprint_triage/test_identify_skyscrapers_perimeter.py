from __future__ import annotations

import numpy as np

from pipeline.blueprint_triage import ExposureContext
from pipeline.blueprint_triage.detect import detect_cliffs, identify_skyscrapers

from .conftest import make_context, make_mask_plan, make_state


def test_identify_skyscrapers_excludes_perimeter_edges_from_isolation() -> None:
    mask = np.zeros((7, 7), dtype=bool)
    mask[0:2, 2:4] = True
    plan = make_mask_plan(mask, thickness_mm=0.20, cap_height_mm=0.08)
    state = make_state(plan)
    state.config.printer_min_line_width_mm = 0.20
    context = make_context(state, exposure=ExposureContext(exempt_outer_perimeter=True))

    cliff_regions = detect_cliffs(context)
    skyscrapers = identify_skyscrapers(cliff_regions, context)

    assert len(skyscrapers) == 1
    assert skyscrapers[0].isolation_score == 1.0
