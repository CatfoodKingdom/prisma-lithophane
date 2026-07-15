from __future__ import annotations

from pipeline.blueprint_triage.detect import detect_cliffs, identify_skyscrapers

from .conftest import make_context, make_split_plan, make_state


def test_identify_skyscrapers_ignores_broad_step() -> None:
    plan = make_split_plan(shape=(10, 20), left_thickness_mm=0.20, right_thickness_mm=0.0)
    state = make_state(plan)
    context = make_context(state)

    cliff_regions = detect_cliffs(context)
    skyscrapers = identify_skyscrapers(cliff_regions, context)

    assert cliff_regions
    assert skyscrapers == []
