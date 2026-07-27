"""Stage 2 diagnostic projection.

The solver service produces the facts; this module preserves the established
diagnostic ordering without mixing that presentation work into optimization
and refinement control flow.
"""
from __future__ import annotations

from collections.abc import Sequence

import numpy as np

from ...staged_artifacts import (
    PlanningDiagnosticEntry,
    PlanningDiagnosticsStream,
    Stage2ObjectiveSummary,
)
from .contracts import _ZoneCandidateSet, _ZoneRecipeOptimizationResult


def record_stage2_diagnostics(
    diagnostics: PlanningDiagnosticsStream,
    *,
    source_edge_subzones: bool,
    subzone_refined_zone_count: int,
    subzone_refined_pixels: int,
    gamut_mask: np.ndarray,
    de_flat: np.ndarray,
    candidate_sets: Sequence[_ZoneCandidateSet],
    augmented_zone_hits: int,
    augmented_candidate_count: int,
    frontier_optical_rescue_zone_hits: int,
    frontier_optical_rescue_candidate_count: int,
    frontier_pressure_rescue_candidate_count: int,
    frontier_neighbor_match_zone_hits: int,
    optimization: _ZoneRecipeOptimizationResult,
    objective_summary: Stage2ObjectiveSummary,
    detail_override_pixels: int,
    detail_override_zones: int,
    interior_override_pixels: int,
    interior_override_zones: int,
    boundary_mutation_enabled: bool,
    boundary_mutation_accepted_pixels: int,
    boundary_mutation_candidate_pixels: int,
    boundary_mutation_mean_gain: float,
) -> None:
    """Append the Stage 2 diagnostic ledger in its established order."""
    if source_edge_subzones:
        diagnostics.entries.append(
            PlanningDiagnosticEntry(
                code="stage2_source_edge_subzones",
                severity="info",
                message=(
                    "Stage 2 source-edge subzone prototype refined "
                    f"{int(subzone_refined_zone_count)} projected zones over "
                    f"{int(subzone_refined_pixels)} fine-grid pixels."
                ),
            )
        )

    oog_pixels = int(np.count_nonzero(gamut_mask))
    diagnostics.entries.append(
        PlanningDiagnosticEntry(
            code="stage2_gamut_mask_pixels",
            severity="warning" if oog_pixels else "info",
            message=f"Stage 2 found {oog_pixels} out-of-gamut solve-grid pixels.",
        )
    )
    if de_flat.size:
        diagnostics.entries.append(
            PlanningDiagnosticEntry(
                code="stage2_de_mean",
                severity="info",
                message=(
                    "Stage 2 mean per-pixel pre-commit dE = "
                    f"{float(np.mean(de_flat)):.4f}."
                ),
            )
        )
    diagnostics.entries.append(
        PlanningDiagnosticEntry(
            code="stage2_frontier_mean_size",
            severity="info",
            message=(
                "Stage 2 mean frontier size after rescue = "
                f"{float(np.mean([c.local_scores.size for c in candidate_sets])) if candidate_sets else 0.0:.2f}."
            ),
        )
    )
    diagnostics.entries.append(
        PlanningDiagnosticEntry(
            code="stage2_neighbor_seed_candidate_zone_hits",
            severity="info",
            message=(
                "Stage 2 pre-pruning neighbor seed augmentation touched "
                f"{int(augmented_zone_hits)} zones and added "
                f"{int(augmented_candidate_count)} candidates."
            ),
        )
    )
    diagnostics.entries.append(
        PlanningDiagnosticEntry(
            code="stage2_frontier_optical_rescue_candidate_count",
            severity="info",
            message=(
                "Stage 2 optical frontier rescue touched "
                f"{int(frontier_optical_rescue_zone_hits)} zones and restored "
                f"{int(frontier_optical_rescue_candidate_count)} candidates "
                f"({int(frontier_pressure_rescue_candidate_count)} pressure-selected)."
            ),
        )
    )
    diagnostics.entries.append(
        PlanningDiagnosticEntry(
            code="stage2_frontier_thickness_span_mean_mm",
            severity="info",
            message=(
                "Stage 2 mean frontier thickness span after rescue = "
                f"{float(np.mean([float(np.ptp(c.total_thickness_mm)) if c.total_thickness_mm.size else 0.0 for c in candidate_sets])) if candidate_sets else 0.0:.4f}mm."
            ),
        )
    )
    diagnostics.entries.append(
        PlanningDiagnosticEntry(
            code="stage2_frontier_neighbor_match_zone_hits",
            severity="info",
            message=(
                "Stage 2 seam-aware frontier enrichment preserved "
                f"neighbor-matching candidates in "
                f"{int(frontier_neighbor_match_zone_hits)} zones."
            ),
        )
    )
    diagnostics.entries.append(
        PlanningDiagnosticEntry(
            code="stage2_beam_seed_zone_changes",
            severity="info",
            message=(
                "Stage 2 beam seed changed "
                f"{int(np.count_nonzero(optimization.local_seed_selected_stack_ids != optimization.initial_selected_stack_ids))} "
                "zone choices before coordinate descent."
            ),
        )
    )
    diagnostics.entries.append(
        PlanningDiagnosticEntry(
            code="stage2_beam_seed_boundary_step_mean_mm",
            severity="info",
            message=(
                "Stage 2 beam seed boundary step mean = "
                f"{optimization.boundary_step_mean_before_mm:.4f}mm "
                "(local independent seed was "
                f"{optimization.boundary_step_mean_local_seed_mm:.4f}mm)."
            ),
        )
    )
    diagnostics.entries.append(
        PlanningDiagnosticEntry(
            code="stage2_boundary_step_mean_before_mm",
            severity="info",
            message=(
                "Stage 2 mean boundary step improved from "
                f"{optimization.boundary_step_mean_before_mm:.4f}mm to "
                f"{optimization.boundary_step_mean_after_mm:.4f}mm."
            ),
        )
    )
    diagnostics.entries.append(
        PlanningDiagnosticEntry(
            code="stage2_pair_repair_zone_changes",
            severity="info",
            message=(
                "Stage 2 pair repair changed "
                f"{optimization.pair_repair_zone_changes} zone choices after "
                "coordinate descent; mean boundary step moved from "
                f"{optimization.boundary_step_mean_after_coord_mm:.4f}mm to "
                f"{optimization.boundary_step_mean_after_mm:.4f}mm."
            ),
        )
    )
    diagnostics.entries.append(
        PlanningDiagnosticEntry(
            code="stage2_intra_zone_target_variance_mean",
            severity="info",
            message=(
                "Stage 2 mean intra-zone target variance norm = "
                f"{objective_summary.intra_zone_target_variance_mean:.4f}."
            ),
        )
    )
    diagnostics.entries.append(
        PlanningDiagnosticEntry(
            code="stage2_boundary_step_p95_mm",
            severity="info",
            message=(
                "Stage 2 boundary step p95 improved from "
                f"{objective_summary.boundary_step_p95_before_mm:.4f}mm to "
                f"{objective_summary.boundary_step_p95_after_mm:.4f}mm."
            ),
        )
    )
    diagnostics.entries.append(
        PlanningDiagnosticEntry(
            code="stage2_boundary_aware_zone_changes",
            severity="info",
            message=(
                f"Stage 2 boundary-aware pass changed {optimization.changed_zone_count} "
                "zone recipe selections."
            ),
        )
    )
    diagnostics.entries.append(
        PlanningDiagnosticEntry(
            code="stage2_detail_override_pixels",
            severity="info",
            message=(
                "Stage 2 within-zone detail pass overrode "
                f"{int(detail_override_pixels)} fine-grid pixels across "
                f"{int(detail_override_zones)} coarse zones."
            ),
        )
    )
    diagnostics.entries.append(
        PlanningDiagnosticEntry(
            code="stage2_detail_interior_override_pixels",
            severity="info",
            message=(
                "Stage 2 mixed-resolution interior pass contributed "
                f"{int(interior_override_pixels)} fine-grid overrides across "
                f"{int(interior_override_zones)} coarse zones."
            ),
        )
    )
    diagnostics.entries.append(
        PlanningDiagnosticEntry(
            code="stage2_boundary_mutation_pixels",
            severity="info" if not boundary_mutation_enabled else "warning",
            message=(
                "Stage 2 boundary mutation "
                f"{'accepted' if boundary_mutation_enabled else 'skipped'} "
                f"{int(boundary_mutation_accepted_pixels)} pixels from "
                f"{int(boundary_mutation_candidate_pixels)} boundary candidates "
                f"(mean_gain={float(boundary_mutation_mean_gain):.4f})."
            ),
        )
    )
    for worst_edge in objective_summary.worst_edges[:3]:
        diagnostics.entries.append(
            PlanningDiagnosticEntry(
                code="stage2_worst_edge_seam",
                severity=(
                    "info"
                    if worst_edge.step_after_mm <= worst_edge.step_before_mm
                    else "warning"
                ),
                message=(
                    f"Edge ({worst_edge.zone_a}, {worst_edge.zone_b}) over "
                    f"{worst_edge.shared_length_px}px moved from "
                    f"{worst_edge.step_before_mm:.4f}mm to "
                    f"{worst_edge.step_after_mm:.4f}mm."
                ),
                zone_ids=(worst_edge.zone_a, worst_edge.zone_b),
            )
        )
    for changed_zone in objective_summary.changed_zones[:5]:
        diagnostics.entries.append(
            PlanningDiagnosticEntry(
                code="stage2_zone_change_reason",
                severity="info",
                message=(
                    f"Zone {changed_zone.zone_id} switched stack "
                    f"{changed_zone.initial_stack_id} -> "
                    f"{changed_zone.selected_stack_id}; total cost "
                    f"{changed_zone.total_cost_before:.4f} -> "
                    f"{changed_zone.total_cost_after:.4f}, boundary term "
                    f"{changed_zone.boundary_cost_before:.4f} -> "
                    f"{changed_zone.boundary_cost_after:.4f}."
                ),
                zone_ids=(changed_zone.zone_id,),
            )
        )


__all__ = ("record_stage2_diagnostics",)
