from __future__ import annotations

from pipeline.blueprint_triage.detect import (
    detect_cliffs,
    detect_floating,
    detect_narrow_strands,
    detect_overlaps,
    detect_sub_features,
    detect_voids,
    identify_skyscrapers,
)
from pipeline.blueprint_triage.fields import AnalysisContext
from pipeline.blueprint_triage.fingerprint import exposure_fingerprint, plan_fingerprint
from pipeline.blueprint_triage.report import CliffHazard, PrintabilityReport, ReportSummary, Verdict
from pipeline.solved_material_plan import SolvedMaterialPlan


def analyze_triage(plan: SolvedMaterialPlan, context: AnalysisContext) -> PrintabilityReport:
    voids = detect_voids(context)
    floating = detect_floating(context)
    overlaps = detect_overlaps(context)
    sub_features = detect_sub_features(context)
    narrow_strands = detect_narrow_strands(context)
    cliff_regions = detect_cliffs(context)
    skyscraper_regions = identify_skyscrapers(cliff_regions, context)
    blocking_sub_features = [
        hazard for hazard in sub_features if hazard.severity is Verdict.DISQUALIFYING
    ]
    warning_sub_features = [
        hazard for hazard in sub_features if hazard.severity is Verdict.WARNINGS
    ]
    blocking_narrow_strands = [
        hazard for hazard in narrow_strands if hazard.severity is Verdict.DISQUALIFYING
    ]
    warning_narrow_strands = [
        hazard for hazard in narrow_strands if hazard.severity is Verdict.WARNINGS
    ]
    cliff_eps = max(1e-6, float(context.config.layer_height_mm) * 0.01)
    blocking_cliffs = []
    warning_cliffs = []
    for region in cliff_regions:
        if region.max_exposure_depth_mm >= float(context.config.cliff_block_depth_mm) - cliff_eps:
            blocking_cliffs.append(
                CliffHazard(
                    severity=Verdict.DISQUALIFYING,
                    message="Color cliff exceeds the blocking exposure-depth threshold.",
                    region=region,
                )
            )
        elif region.max_exposure_depth_mm >= float(context.config.cliff_warn_depth_mm) - cliff_eps:
            warning_cliffs.append(
                CliffHazard(
                    severity=Verdict.WARNINGS,
                    message="Color cliff exceeds the warning exposure-depth threshold.",
                    region=region,
                )
            )
    blocking_hazards = [
        *voids,
        *floating,
        *overlaps,
        *blocking_sub_features,
        *blocking_narrow_strands,
        *blocking_cliffs,
    ]
    warnings = [*warning_sub_features, *warning_narrow_strands, *warning_cliffs]
    verdict = Verdict.CLEAN
    if blocking_hazards:
        verdict = Verdict.DISQUALIFYING
    elif warnings:
        verdict = Verdict.WARNINGS
    return PrintabilityReport(
        verdict=verdict,
        blocking_hazards=blocking_hazards,
        warnings=warnings,
        voids=voids,
        floating=floating,
        overlaps=overlaps,
        sub_features=sub_features,
        narrow_strands=narrow_strands,
        cliff_regions=cliff_regions,
        skyscraper_regions=skyscraper_regions,
        summary=ReportSummary(
            blocking_count=len(blocking_hazards),
            warning_count=len(warnings),
            void_count=len(voids),
            floating_count=len(floating),
            overlap_count=len(overlaps),
            sub_feature_count=len(sub_features),
            narrow_strand_count=len(narrow_strands),
            cliff_region_count=len(cliff_regions),
            skyscraper_region_count=len(skyscraper_regions),
        ),
        plan_fingerprint=plan_fingerprint(plan),
        context_fingerprint=exposure_fingerprint(context.exposure),
        plan=plan,
        analysis_context=context,
    )
