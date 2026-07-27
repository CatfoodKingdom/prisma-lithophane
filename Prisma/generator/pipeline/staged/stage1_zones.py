"""Stage 1 lateral-zone planning."""
from __future__ import annotations


import numpy as np

from ..staged_solver_helpers import generate_stage1_zone_labels

from ..staged_artifacts import (
    LateralZonePlan,
    PlanningDiagnosticEntry,
    PlanningDiagnosticsStream,
)
from ..staged_printability import resolve_blueprint_printability_settings

from .coarse_grid import (
    _effective_stage1_coarsening_factor,
    _stage1_lattice_offset_px,
    _downsample_rgb_image,
    _downsample_flat_oklab_targets,
)
from .zone_geometry import (
    _build_zone_adjacency,
    _zone_flat_indices,
    _summarize_zone_targets,
)

def _effective_color_region_target_mm(cfg) -> float:
    """Return the Stage 1 region target, optionally scaled by printability limits."""
    base = float(cfg.color_region_target_mm or 0.60)
    if not bool(cfg.color_region_target_from_printability):
        return base

    settings = resolve_blueprint_printability_settings(cfg)
    multiplier = float(
        cfg.color_region_target_width_multiplier or 2.0
    )
    if multiplier <= 0.0:
        multiplier = 2.0
    physical_floor = max(
        float(settings.minimum_line_length_mm),
        float(settings.minimum_extrusion_width_mm) * multiplier,
    )
    return max(base, physical_floor)

def build_zone_plan(state, diagnostics: PlanningDiagnosticsStream) -> LateralZonePlan:
    """Produce the Stage 1 authoritative zone artifact."""
    cfg = state.config
    fine_targets = state.solve_target_oklab
    if fine_targets is None:
        raise RuntimeError("Staged Stage 1 requires solve_target_oklab from the runner.")
    fine_shape = state.image.shape[:2]
    stage1_factor = _effective_stage1_coarsening_factor(cfg)
    offset_y_px, offset_x_px = _stage1_lattice_offset_px(cfg)
    if stage1_factor <= 1:
        offset_y_px = 0
        offset_x_px = 0
    planning_pitch_mm = float(cfg.solver_fine_pitch_mm) * float(stage1_factor)
    planning_image = _downsample_rgb_image(
        state.image,
        stage1_factor,
        offset_y_px=offset_y_px,
        offset_x_px=offset_x_px,
    )
    planning_targets = _downsample_flat_oklab_targets(
        fine_targets,
        fine_shape=fine_shape,
        factor=stage1_factor,
        offset_y_px=offset_y_px,
        offset_x_px=offset_x_px,
    )
    color_region_target_mm = _effective_color_region_target_mm(cfg)
    coarse_scale_override = None
    target_from_printability = bool(
        cfg.color_region_target_from_printability
    )
    if stage1_factor > 1 or target_from_printability:
        target_ratio = float(color_region_target_mm) / float(cfg.solver_fine_pitch_mm)
        scale = int(np.ceil(target_ratio - 1e-9)) if target_from_printability else int(round(target_ratio))
        coarse_scale_override = max(
            1,
            scale,
        )
    zone_labels = generate_stage1_zone_labels(
        planning_image,
        color_region_target_mm=float(color_region_target_mm),
        solver_fine_pitch_mm=planning_pitch_mm,
        cell_mode=str(cfg.cell_mode),
        scale=coarse_scale_override,
        smooth_boundaries=bool(cfg.smooth_boundaries),
        boundary_smooth_radius=int(cfg.boundary_smooth_radius),
    )
    zone_flat_indices = _zone_flat_indices(zone_labels)
    means, variances = _summarize_zone_targets(zone_flat_indices, planning_targets)
    adjacency, adjacency_lengths = _build_zone_adjacency(zone_labels)
    zone_pixel_counts = np.array(
        [indices.size for indices in zone_flat_indices],
        dtype=np.int32,
    )
    diagnostics.entries.append(
        PlanningDiagnosticEntry(
            code="stage1_zone_count",
            severity="info",
            message=f"Stage 1 generated {means.shape[0]} zones.",
        )
    )
    if offset_y_px != 0 or offset_x_px != 0:
        diagnostics.entries.append(
            PlanningDiagnosticEntry(
                code="stage1_lattice_offset",
                severity="info",
                message=(
                    "Staged Stage 1 lattice offset applied at "
                    f"y={int(offset_y_px)} px, x={int(offset_x_px)} px."
                ),
            )
        )
    return LateralZonePlan(
        planning_shape=tuple(int(dim) for dim in zone_labels.shape),
        planning_pitch_mm=planning_pitch_mm,
        coarse_to_fine_scale=stage1_factor,
        zone_label_map=zone_labels,
        zone_flat_indices=zone_flat_indices,
        adjacency_edges=adjacency,
        adjacency_edge_lengths_px=adjacency_lengths,
        zone_pixel_counts=zone_pixel_counts,
        target_oklab_mean_by_zone=means,
        target_oklab_var_by_zone=variances,
    )

__all__ = (
    '_effective_color_region_target_mm',
    'build_zone_plan',
)
