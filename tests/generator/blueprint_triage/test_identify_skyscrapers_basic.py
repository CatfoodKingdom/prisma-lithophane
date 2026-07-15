from __future__ import annotations

import numpy as np
import pytest

from pipeline.blueprint_triage.detect import detect_cliffs, identify_skyscrapers

from .conftest import make_context, make_mask_plan, make_state


def test_identify_skyscrapers_detects_isolated_tall_column() -> None:
    mask = np.zeros((7, 7), dtype=bool)
    mask[2:4, 2:4] = True
    plan = make_mask_plan(mask, thickness_mm=0.20, cap_height_mm=0.08)
    state = make_state(plan)
    state.config.printer_min_line_width_mm = 0.20
    context = make_context(state)

    cliff_regions = detect_cliffs(context)
    skyscrapers = identify_skyscrapers(cliff_regions, context)

    assert len(skyscrapers) == 1
    skyscraper = skyscrapers[0]
    assert skyscraper.bbox == (2, 2, 4, 4)
    assert skyscraper.prominence_mm == pytest.approx(0.20, abs=1e-6)
    assert skyscraper.isolation_score == 1.0
    assert len(skyscraper.cliff_regions) == 1
