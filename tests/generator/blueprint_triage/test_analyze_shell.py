from __future__ import annotations

from pipeline.blueprint_triage import (
    AnalysisContext,
    ExposureContext,
    ValidatedBlueprintToken,
    Verdict,
    analyze_triage,
)
from tests.generator.blueprint_triage.conftest import make_mask_plan, make_state


def test_analyze_triage_shell_returns_clean_stable_report():
    clean_plan = make_mask_plan(
        [[True] * 6 for _ in range(6)],
        thickness_mm=0.16,
        pitch_mm=0.20,
    )
    clean_state = make_state(clean_plan)
    context = AnalysisContext.from_state(
        clean_state,
        exposure=ExposureContext(exempt_outer_perimeter=True),
    )

    report_a = analyze_triage(clean_plan, context)
    report_b = analyze_triage(clean_plan, context)
    token = ValidatedBlueprintToken.from_report(report_a)

    assert report_a.verdict is Verdict.CLEAN
    assert report_a.blocking_hazards == []
    assert report_a.warnings == []
    assert report_a.voids == []
    assert report_a.floating == []
    assert report_a.overlaps == []
    assert report_a.sub_features == []
    assert report_a.narrow_strands == []
    assert report_a.cliff_regions == []
    assert report_a.skyscraper_regions == []
    assert report_a.plan_fingerprint
    assert report_a.context_fingerprint
    assert report_a.plan_fingerprint == report_b.plan_fingerprint
    assert report_a.context_fingerprint == report_b.context_fingerprint
    assert token.is_valid_for(clean_plan, report_a.context_fingerprint)
