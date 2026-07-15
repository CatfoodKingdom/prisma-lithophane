from __future__ import annotations

from pipeline.blueprint_triage import Verdict, analyze_triage
from tests.generator.blueprint_triage.conftest import make_context, make_split_plan, make_state


def test_analyze_triage_hard_cliff_is_disqualifying():
    plan = make_split_plan(shape=(4, 4), left_thickness_mm=0.24, right_thickness_mm=0.0, cap_height_mm=0.08)

    report = analyze_triage(plan, make_context(make_state(plan)))

    assert report.verdict is Verdict.DISQUALIFYING
    assert report.summary.cliff_region_count == 1
    assert any(getattr(hazard, "region", None) is not None for hazard in report.blocking_hazards)


def test_analyze_triage_warn_cliff_is_warning_only():
    plan = make_split_plan(shape=(4, 4), left_thickness_mm=0.16, right_thickness_mm=0.0, cap_height_mm=0.08)

    report = analyze_triage(plan, make_context(make_state(plan)))

    assert report.verdict is Verdict.WARNINGS
    assert report.summary.cliff_region_count == 1
    assert report.summary.blocking_count == 0
    assert report.summary.warning_count >= 1


def test_analyze_triage_below_warn_cliff_is_clean():
    plan = make_split_plan(shape=(4, 4), left_thickness_mm=0.12, right_thickness_mm=0.0, cap_height_mm=0.08)

    report = analyze_triage(plan, make_context(make_state(plan)))

    assert report.verdict is Verdict.CLEAN
    assert report.summary.cliff_region_count == 1
    assert report.summary.blocking_count == 0
    assert report.summary.warning_count == 0


def test_analyze_triage_one_hard_edge_among_warn_edges_is_disqualifying():
    plan = make_split_plan(shape=(3, 4), left_thickness_mm=0.24, right_thickness_mm=0.08, cap_height_mm=0.08)
    plan.cap_height_map[1, 2:] = 0.0

    report = analyze_triage(plan, make_context(make_state(plan)))

    assert report.verdict is Verdict.DISQUALIFYING
    assert report.summary.cliff_region_count == 1
    assert report.summary.blocking_count >= 1
