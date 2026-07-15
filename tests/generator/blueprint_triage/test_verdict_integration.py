from __future__ import annotations

from pipeline.blueprint_triage import Verdict, analyze_triage
from tests.generator.blueprint_triage.conftest import make_context, make_mask_plan, make_plan, make_state


def test_analyze_triage_escalates_blocking_void_to_disqualifying():
    plan = make_plan()
    context = make_context(make_state(plan))
    context.interval_schedule.intervals[0][0] = []

    report = analyze_triage(plan, context)

    assert report.verdict is Verdict.DISQUALIFYING
    assert report.summary.blocking_count >= 1
    assert len(report.blocking_hazards) >= 1
    assert len(report.voids) == 1


def test_analyze_triage_keeps_clean_plan_clean():
    plan = make_mask_plan(
        [[True] * 6 for _ in range(6)],
        thickness_mm=0.16,
        pitch_mm=0.20,
    )
    report = analyze_triage(plan, make_context(make_state(plan)))

    assert report.verdict is Verdict.CLEAN
    assert report.summary.blocking_count == 0
    assert report.blocking_hazards == []
