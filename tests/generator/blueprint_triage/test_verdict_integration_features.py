from __future__ import annotations

import numpy as np

from pipeline.blueprint_triage import Verdict, analyze_triage
from tests.generator.blueprint_triage.conftest import make_context, make_mask_plan, make_state


def test_analyze_triage_hard_sub_feature_is_disqualifying():
    mask = np.zeros((6, 6), dtype=bool)
    mask[2, 2] = True
    plan = make_mask_plan(mask, thickness_mm=0.16, pitch_mm=0.10, cap_height_mm=0.0)

    report = analyze_triage(plan, make_context(make_state(plan)))

    assert report.verdict is Verdict.DISQUALIFYING
    assert report.summary.blocking_count >= 1
    assert report.summary.sub_feature_count == 1
    assert any(hazard.severity is Verdict.DISQUALIFYING for hazard in report.sub_features)


def test_analyze_triage_soft_sub_feature_is_warning_only():
    mask = np.zeros((8, 8), dtype=bool)
    mask[3:5, 3:5] = True
    plan = make_mask_plan(mask, thickness_mm=0.08, pitch_mm=0.11, cap_height_mm=0.0)
    state = make_state(plan)
    state.config.printer_min_line_width_mm = 0.20

    report = analyze_triage(plan, make_context(state))

    assert report.verdict is Verdict.WARNINGS
    assert report.summary.blocking_count == 0
    assert report.summary.warning_count >= 1
    assert report.summary.sub_feature_count == 1
    assert any(hazard.severity is Verdict.WARNINGS for hazard in report.sub_features)


def test_analyze_triage_clean_feature_plan_stays_clean():
    plan = make_mask_plan(np.ones((6, 6), dtype=bool), thickness_mm=0.16, pitch_mm=0.20, cap_height_mm=0.0)

    report = analyze_triage(plan, make_context(make_state(plan)))

    assert report.verdict is Verdict.CLEAN
    assert report.summary.blocking_count == 0
    assert report.summary.warning_count == 0
