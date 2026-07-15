from __future__ import annotations

import numpy as np

from facade import SolveConfig, SolveResult, SolveStats
from pipeline.blueprint_triage import (
    AnalysisContext,
    CliffEdge,
    CliffRegion,
    ExposureContext,
    FixCandidate,
    FixScoreBreakdown,
    FixType,
    FloatingHazard,
    Hazard,
    IntervalSchedule,
    MaterialInterval,
    MissingColumnHazard,
    NarrowStrandHazard,
    OverlapHazard,
    PrintabilityReport,
    ReportSummary,
    SalienceFields,
    SalienceSummary,
    SkyscraperRegion,
    SubFeatureHazard,
    TriageConfig,
    ValidatedBlueprintToken,
    Verdict,
    VoidHazard,
)
from pipeline.state import PipelineState


def test_blueprint_triage_dataclasses_instantiate(sample_plan, sample_state):
    triage = TriageConfig()
    interval = MaterialInterval(0.0, 0.08, "white")
    schedule = IntervalSchedule(shape=sample_plan.shape, intervals=[[[interval], []], [[], []]])
    salience = SalienceFields(
        luminance_gradient=np.zeros(sample_plan.shape, dtype=np.float32),
        chroma_gradient=np.zeros(sample_plan.shape, dtype=np.float32),
        combined_gradient=np.zeros(sample_plan.shape, dtype=np.float32),
    )
    exposure = ExposureContext(
        exempt_outer_perimeter=True,
        exempt_edge_mask=np.zeros((*sample_plan.shape, 4), dtype=bool),
    )
    context = AnalysisContext.from_state(sample_state, exposure=exposure)
    base_hazard = Hazard(severity=Verdict.WARNINGS, message="placeholder")
    void = VoidHazard(severity=Verdict.DISQUALIFYING)
    missing = MissingColumnHazard(severity=Verdict.DISQUALIFYING, row=0, col=1)
    floating = FloatingHazard(severity=Verdict.DISQUALIFYING)
    overlap = OverlapHazard(severity=Verdict.DISQUALIFYING)
    sub_feature = SubFeatureHazard(severity=Verdict.WARNINGS)
    narrow = NarrowStrandHazard(severity=Verdict.WARNINGS)
    edge = CliffEdge(row=0, col=0, direction="right", exposure_depth_mm=0.08)
    salience_summary = SalienceSummary()
    cliff = CliffRegion(edges=[edge], salience_summary=salience_summary)
    skyscraper = SkyscraperRegion(cliff_regions=[cliff])
    scores = FixScoreBreakdown()
    candidate = FixCandidate(fix_type=FixType.ACCEPT, target=cliff, scores=scores)
    summary = ReportSummary()
    report = PrintabilityReport(verdict=Verdict.CLEAN, summary=summary)
    token = ValidatedBlueprintToken.from_report(report)

    assert triage.hard_min_line_width_mm > 0.0
    assert triage.hard_min_feature_width_mm > 0.0
    assert triage.fidelity_prior_gradient_weight == 0.5
    assert schedule.shape == sample_plan.shape
    assert salience.combined_gradient.shape == sample_plan.shape
    assert exposure.exempt_edge_mask.shape == (*sample_plan.shape, 4)
    assert context.cap_height_map is sample_plan.cap_height_map
    assert base_hazard.severity is Verdict.WARNINGS
    assert void.pixel_count == 0
    assert missing.col == 1
    assert floating.severity is Verdict.DISQUALIFYING
    assert overlap.pixel_count == 0
    assert sub_feature.bbox == (0, 0, 0, 0)
    assert narrow.min_width_mm == 0.0
    assert cliff.edges[0].direction == "right"
    assert skyscraper.cliff_regions[0] is cliff
    assert candidate.fix_type is FixType.ACCEPT
    assert report.summary is summary
    assert token.verdict is Verdict.CLEAN


def test_triage_config_uses_min_line_width_not_nozzle_diameter():
    cfg = type(
        "Cfg",
        (),
        {
            "nozzle_diameter": 0.40,
            "printer_min_line_width_mm": 0.32,
            "printability_minimum_extrusion_width_mm": None,
            "layer_height": 0.08,
        },
    )()

    triage = TriageConfig.from_pipeline_config(cfg)

    assert triage.hard_min_line_width_mm == 0.32
    assert triage.hard_min_feature_width_mm == 0.32
    assert triage.hard_min_feature_area_mm2 == 0.32 * 0.32
    # Soft (warning) thresholds use the nozzle-derived feature scale
    # (2 x nozzle_diameter = 0.80 here), replacing the retired cleanup_min_width_mm
    # / cleanup_min_area_mm2 fields. Hard thresholds still come from
    # printer_min_line_width (0.32), NOT nozzle_diameter.
    assert triage.soft_min_line_width_mm == 0.80
    assert triage.soft_min_feature_width_mm == 0.80
    assert triage.soft_min_feature_area_mm2 == 0.80 * 0.80


def test_runtime_field_defaults(sample_state):
    cfg = SolveConfig(
        palette=["bambu-basic-cyan"],
        white_base="panchroma-matte-cotton-white",
    )
    result = SolveResult(
        thickness_maps={"__white_cap__": np.zeros((1, 1), dtype=np.float32)},
        color_profiles={},
        wb_profile={},
        wc_profile={},
        stats=SolveStats(
            mean_de=0.0,
            max_de=0.0,
            n_out_of_gamut=0,
            total_pixels=1,
            image_w=1,
            image_h=1,
            coverage_pct=100.0,
            max_height=0.28,
        ),
        config=cfg,
    )
    state = PipelineState(
        image=np.zeros((1, 1, 3), dtype=np.uint8),
        config=sample_state.config,
    )

    assert result.blueprint_triage is None
    assert state.blueprint_triage is None
