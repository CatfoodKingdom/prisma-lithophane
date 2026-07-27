"""Stage 4 diagnostic projection.

This module translates completed Stage 4 facts into stable diagnostic entries.
It deliberately contains no cap-authoring or printability decisions.
"""
from __future__ import annotations

import numpy as np

from ...staged_artifacts import (
    PlanningDiagnosticEntry,
    PlanningDiagnosticsStream,
    Stage4BoundaryCapPrintabilitySummary,
    Stage4DetailAuthoringPrintabilitySummary,
    Stage4DetailPrintabilitySummary,
    Stage4DetailZoneSummary,
)


def record_stage4_diagnostics(
    diagnostics: PlanningDiagnosticsStream,
    *,
    cap_height: np.ndarray,
    cap_policy_reference: np.ndarray,
    final_cap_target: np.ndarray,
    detail_height: np.ndarray,
    detail_zone_summary: Stage4DetailZoneSummary,
    boundary_cap_printability_summary: Stage4BoundaryCapPrintabilitySummary,
    detail_authoring_printability_summary: Stage4DetailAuthoringPrintabilitySummary,
    detail_printability_summary: Stage4DetailPrintabilitySummary,
) -> None:
    """Append the Stage 4 diagnostic ledger in its established order."""
    clamped_pixels = int(
        np.count_nonzero(final_cap_target + 1e-9 < cap_policy_reference)
    )
    detail_active_pixels = int(np.count_nonzero(detail_height > 1e-9))
    diagnostics.entries.append(
        PlanningDiagnosticEntry(
            code="stage4_cap_clamped_pixels",
            severity="warning" if clamped_pixels else "info",
            message=f"Stage 4 clamped visible cap thickness at {clamped_pixels} pixels.",
        )
    )
    diagnostics.entries.append(
        PlanningDiagnosticEntry(
            code="stage4_cap_height_range_mm",
            severity="info",
            message=(
                "Stage 4 cap thickness range after policy and budget clamp = "
                f"{float(np.min(cap_height)):.4f}mm to "
                f"{float(np.max(cap_height)):.4f}mm."
            ),
        )
    )
    diagnostics.entries.append(
        PlanningDiagnosticEntry(
            code="stage4_detail_tier_active_pixels",
            severity="info",
            message=(
                "Stage 4 detail tier added visible cap at "
                f"{detail_active_pixels} pixels."
            ),
        )
    )
    diagnostics.entries.append(
        PlanningDiagnosticEntry(
            code="stage4_detail_zone_count",
            severity="info",
            message=(
                "Stage 4 detail cap tier materialized "
                f"{detail_zone_summary.zone_count} accepted zones from "
                f"{detail_zone_summary.candidate_zone_count} candidates."
            ),
        )
    )
    diagnostics.entries.append(
        PlanningDiagnosticEntry(
            code="stage4_detail_zone_rejected_pixels",
            severity=(
                "info" if detail_zone_summary.rejected_pixels == 0 else "warning"
            ),
            message=(
                "Stage 4 detail-cap zone filtering rejected "
                f"{detail_zone_summary.rejected_pixels} candidate pixels."
            ),
        )
    )
    diagnostics.entries.append(
        PlanningDiagnosticEntry(
            code="stage4_detail_zone_rejection_reasons",
            severity=(
                "info"
                if detail_zone_summary.rejected_zone_count == 0
                else "warning"
            ),
            message=(
                "Stage 4 detail-zone rejection counts: "
                f"too_small={detail_zone_summary.rejected_too_small_zone_count}, "
                "weak_optical_gain="
                f"{detail_zone_summary.rejected_weak_optical_gain_zone_count}, "
                f"weak_detail_signal={detail_zone_summary.rejected_weak_signal_zone_count}."
            ),
        )
    )
    if boundary_cap_printability_summary.enabled:
        diagnostics.entries.append(
            PlanningDiagnosticEntry(
                code="stage4_boundary_cap_printability_gate",
                severity=(
                    "info"
                    if boundary_cap_printability_summary.flagged_components == 0
                    else "warning"
                ),
                message=(
                    "Stage 4 boundary-cap printability repair flagged "
                    f"{boundary_cap_printability_summary.flagged_layer_pixels} "
                    "layer-pixels across "
                    f"{boundary_cap_printability_summary.flagged_components} "
                    "components; grew "
                    f"{boundary_cap_printability_summary.grown_layer_pixels}, "
                    "suppressed optional "
                    f"{boundary_cap_printability_summary.suppressed_optional_layer_pixels}, "
                    "and preserved mandatory "
                    f"{boundary_cap_printability_summary.preserved_mandatory_layer_pixels}."
                ),
            )
        )
    if detail_authoring_printability_summary.enabled:
        diagnostics.entries.append(
            PlanningDiagnosticEntry(
                code="stage4_detail_authoring_printability_gate",
                severity=(
                    "info"
                    if detail_authoring_printability_summary.prevented_layer_pixels == 0
                    else "warning"
                ),
                message=(
                    "Stage 4 luminance detail authoring printability gate prevented "
                    f"{detail_authoring_printability_summary.prevented_layer_pixels} "
                    "layer-pixels before final detail verification."
                ),
            )
        )
    if detail_printability_summary.enabled:
        diagnostics.entries.append(
            PlanningDiagnosticEntry(
                code="stage4_detail_printability_gate",
                severity=(
                    "info"
                    if detail_printability_summary.suppressed_components == 0
                    else "warning"
                ),
                message=(
                    "Stage 4 optional detail printability gate suppressed "
                    f"{detail_printability_summary.suppressed_layer_pixels} layer-pixels "
                    f"across {detail_printability_summary.suppressed_components} components."
                ),
            )
        )


__all__ = ("record_stage4_diagnostics",)
