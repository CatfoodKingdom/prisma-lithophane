"""Staged-solver telemetry primitives."""
from __future__ import annotations

import time

import numpy as np

from progress import ProgressReporter

from ..staged_artifacts import StagedPerformanceProfile
from ..staged_printability import resolve_blueprint_printability_settings

from .coarse_grid import _stage1_lattice_offset_px
from .stage1_zones import _effective_color_region_target_mm

def _make_staged_progress_emitter(progress, total_start: float):
    """Adapt the supported progress interfaces to one staged-solve emitter."""

    def emit(label: str, pct: int) -> None:
        if progress is None:
            return
        bounded_pct = int(max(0, min(100, pct)))
        if isinstance(progress, ProgressReporter):
            progress.emit(
                stage="solve",
                stage_label=label,
                stage_index=1,
                stage_pct=bounded_pct,
                local_pct=bounded_pct,
            )
            return
        progress({
            "stage": "solve",
            "stage_label": label,
            "stage_index": 1,
            "stage_count": 1,
            "stage_pct": bounded_pct,
            "overall_pct": float(bounded_pct),
            "elapsed_s": time.perf_counter() - total_start,
            "eta_s": None,
            "palette_index": None,
            "palette_count": None,
        })

    return emit


def _debug_map_sink(state):
    debug_maps = getattr(state, "debug_maps", None)
    if debug_maps is None:
        try:
            state.debug_maps = {}
            debug_maps = state.debug_maps
        except AttributeError:
            return None
    if not isinstance(debug_maps, dict):
        return None
    return debug_maps

def _record_debug_map(debug_maps, key: str, value: np.ndarray) -> None:
    if debug_maps is None:
        return
    debug_maps[key] = np.array(value, dtype=np.float32, copy=True)

def _record_timing(profile: StagedPerformanceProfile, key: str, elapsed_s: float) -> None:
    """Record one timing span on the experimental performance profile."""
    profile.timings_s[str(key)] = float(elapsed_s)

def _set_counter(
    profile: StagedPerformanceProfile,
    key: str,
    value: int | float | bool | str | list[int],
) -> None:
    """Record one machine-friendly counter on the experimental performance profile."""
    profile.counters[str(key)] = value

def _record_solve_context_metrics(performance_profile, state) -> None:
    _set_counter(performance_profile, "observation_image_height_px", int(state.image.shape[0]))
    _set_counter(performance_profile, "observation_image_width_px", int(state.image.shape[1]))
    _set_counter(performance_profile, "palette_filament_count", int(len(state.config.palette)))


def _record_stage0_metrics(performance_profile, compiled_directives, stage_start: float) -> None:
    _record_timing(
        performance_profile,
        "stage0_compile_directives_s",
        time.perf_counter() - stage_start,
    )
    _set_counter(performance_profile, "solve_grid_height_px", int(compiled_directives.solver_shape[0]))
    _set_counter(performance_profile, "solve_grid_width_px", int(compiled_directives.solver_shape[1]))
    _set_counter(
        performance_profile,
        "solve_grid_pixel_count",
        int(compiled_directives.solver_shape[0] * compiled_directives.solver_shape[1]),
    )


def _record_stage1_metrics(performance_profile, state, zone_plan, stage_start: float) -> None:
    _record_timing(performance_profile, "stage1_zone_plan_s", time.perf_counter() - stage_start)
    _set_counter(performance_profile, "stage1_zone_count", int(zone_plan.zone_count))
    _set_counter(performance_profile, "stage1_adjacency_edge_count", int(len(zone_plan.adjacency_edges)))
    _set_counter(performance_profile, "stage1_planning_height_px", int(zone_plan.planning_shape[0]))
    _set_counter(performance_profile, "stage1_planning_width_px", int(zone_plan.planning_shape[1]))
    _set_counter(performance_profile, "stage1_planning_pitch_mm", float(zone_plan.planning_pitch_mm))
    _set_counter(performance_profile, "stage1_coarsening_factor", int(zone_plan.coarse_to_fine_scale))
    color_region_target_mm = _effective_color_region_target_mm(state.config)
    _set_counter(
        performance_profile,
        "stage1_requested_color_region_target_mm",
        float(state.config.color_region_target_mm or 0.60),
    )
    _set_counter(
        performance_profile,
        "stage1_effective_color_region_target_mm",
        float(color_region_target_mm),
    )
    _set_counter(
        performance_profile,
        "stage1_color_region_target_from_printability_enabled",
        bool(state.config.color_region_target_from_printability),
    )
    _set_counter(
        performance_profile,
        "stage1_color_region_target_width_multiplier",
        float(
            state.config.color_region_target_width_multiplier
            or 2.0
        ),
    )
    printability_settings = resolve_blueprint_printability_settings(state.config)
    _set_counter(
        performance_profile,
        "stage1_color_region_target_to_min_width_ratio",
        float(color_region_target_mm) / max(float(printability_settings.extrusion_width_mm), 1e-9),
    )
    _set_counter(
        performance_profile,
        "stage1_color_region_target_to_min_line_ratio",
        float(color_region_target_mm) / max(float(printability_settings.minimum_line_length_mm), 1e-9),
    )
    offset_y_px, offset_x_px = _stage1_lattice_offset_px(state.config)
    if zone_plan.coarse_to_fine_scale <= 1:
        offset_y_px = 0
        offset_x_px = 0
    _set_counter(performance_profile, "stage1_lattice_offset_y_px", int(offset_y_px))
    _set_counter(performance_profile, "stage1_lattice_offset_x_px", int(offset_x_px))


def _record_stage3_metrics(performance_profile, filler_plan, stage_start: float) -> None:
    _record_timing(performance_profile, "stage3_filler_s", time.perf_counter() - stage_start)
    _set_counter(
        performance_profile,
        "stage3_filler_pixels",
        int(np.count_nonzero(filler_plan.filler_height_mm > 1e-9)),
    )


def _record_stage4_metrics(performance_profile, state, cap_plan, stage_start: float) -> None:
    _record_timing(performance_profile, "stage4_cap_s", time.perf_counter() - stage_start)
    _set_counter(
        performance_profile,
        "stage4_cap_active_pixels",
        int(np.count_nonzero(cap_plan.cap_height_mm > 1e-9)),
    )
    _set_counter(
        performance_profile,
        "stage4_boundary_edge_guard_pixels",
        int(np.count_nonzero(cap_plan.boundary_edge_guard_weight > 0.25)),
    )
    _set_counter(
        performance_profile,
        "stage4_detail_active_pixels",
        int(np.count_nonzero(cap_plan.detail_height_mm > 1e-9)),
    )
    detail_layers = np.rint(
        np.asarray(cap_plan.detail_height_mm, dtype=np.float32) / float(state.config.layer_height)
    ).astype(np.int32)
    _set_counter(
        performance_profile,
        "stage4_detail_max_layers",
        int(np.max(detail_layers, initial=0)),
    )
    _set_counter(
        performance_profile,
        "stage4_detail_two_layer_pixels",
        int(np.count_nonzero(detail_layers >= 2)),
    )
    _set_counter(
        performance_profile,
        "stage4_detail_zone_count",
        int(cap_plan.detail_zone_summary.zone_count),
    )
    _set_counter(
        performance_profile,
        "stage4_detail_candidate_zone_count",
        int(cap_plan.detail_zone_summary.candidate_zone_count),
    )
    _set_counter(
        performance_profile,
        "stage4_detail_rejected_zone_count",
        int(cap_plan.detail_zone_summary.rejected_zone_count),
    )
    _set_counter(
        performance_profile,
        "stage4_detail_zone_rejected_pixels",
        int(cap_plan.detail_zone_summary.rejected_pixels),
    )
    _set_counter(
        performance_profile,
        "stage4_detail_rejected_too_small_zone_count",
        int(cap_plan.detail_zone_summary.rejected_too_small_zone_count),
    )
    _set_counter(
        performance_profile,
        "stage4_detail_rejected_weak_optical_gain_zone_count",
        int(cap_plan.detail_zone_summary.rejected_weak_optical_gain_zone_count),
    )
    _set_counter(
        performance_profile,
        "stage4_detail_rejected_weak_signal_zone_count",
        int(cap_plan.detail_zone_summary.rejected_weak_signal_zone_count),
    )
    _set_counter(
        performance_profile,
        "stage4_detail_zone_mean_structure_support",
        float(cap_plan.detail_zone_summary.mean_zone_structure_support),
    )
    _set_counter(
        performance_profile,
        "stage4_detail_zone_mean_recipe_boundary_support",
        float(cap_plan.detail_zone_summary.mean_zone_recipe_boundary_support),
    )
    boundary_cap_printability_summary = cap_plan.boundary_cap_printability_summary
    if boundary_cap_printability_summary is not None:
        _set_counter(
            performance_profile,
            "stage4_boundary_cap_printability_gate_enabled",
            bool(boundary_cap_printability_summary.enabled),
        )
        _set_counter(
            performance_profile,
            "stage4_boundary_cap_printability_flagged_layer_pixels",
            int(boundary_cap_printability_summary.flagged_layer_pixels),
        )
        _set_counter(
            performance_profile,
            "stage4_boundary_cap_printability_flagged_components",
            int(boundary_cap_printability_summary.flagged_components),
        )
        _set_counter(
            performance_profile,
            "stage4_boundary_cap_printability_grown_layer_pixels",
            int(boundary_cap_printability_summary.grown_layer_pixels),
        )
        _set_counter(
            performance_profile,
            "stage4_boundary_cap_printability_grown_components",
            int(boundary_cap_printability_summary.grown_components),
        )
        _set_counter(
            performance_profile,
            "stage4_boundary_cap_printability_suppressed_optional_layer_pixels",
            int(boundary_cap_printability_summary.suppressed_optional_layer_pixels),
        )
        _set_counter(
            performance_profile,
            "stage4_boundary_cap_printability_suppressed_optional_components",
            int(boundary_cap_printability_summary.suppressed_optional_components),
        )
        _set_counter(
            performance_profile,
            "stage4_boundary_cap_printability_preserved_mandatory_layer_pixels",
            int(boundary_cap_printability_summary.preserved_mandatory_layer_pixels),
        )
        _set_counter(
            performance_profile,
            "stage4_boundary_cap_printability_preserved_mandatory_components",
            int(boundary_cap_printability_summary.preserved_mandatory_components),
        )
        _set_counter(
            performance_profile,
            "stage4_boundary_cap_printability_accepted_components",
            int(boundary_cap_printability_summary.accepted_components),
        )
        _set_counter(
            performance_profile,
            "stage4_boundary_cap_printability_rejected_tiny_components",
            int(boundary_cap_printability_summary.rejected_tiny_components),
        )
        _set_counter(
            performance_profile,
            "stage4_boundary_cap_printability_rejected_narrow_components",
            int(boundary_cap_printability_summary.rejected_narrow_components),
        )
        _set_counter(
            performance_profile,
            "stage4_boundary_cap_printability_rejected_short_components",
            int(boundary_cap_printability_summary.rejected_short_components),
        )
    detail_authoring_printability_summary = cap_plan.detail_authoring_printability_summary
    if detail_authoring_printability_summary is not None:
        _set_counter(
            performance_profile,
            "stage4_detail_authoring_printability_enabled",
            bool(detail_authoring_printability_summary.enabled),
        )
        _set_counter(
            performance_profile,
            "stage4_detail_authoring_printability_mode",
            str(detail_authoring_printability_summary.mode),
        )
        _set_counter(
            performance_profile,
            "stage4_detail_authoring_requested_layer_pixels_before",
            int(detail_authoring_printability_summary.requested_layer_pixels_before),
        )
        _set_counter(
            performance_profile,
            "stage4_detail_authoring_requested_active_pixels_before",
            int(detail_authoring_printability_summary.requested_active_pixels_before),
        )
        _set_counter(
            performance_profile,
            "stage4_detail_authoring_requested_layer_pixels_after",
            int(detail_authoring_printability_summary.requested_layer_pixels_after),
        )
        _set_counter(
            performance_profile,
            "stage4_detail_authoring_requested_active_pixels_after",
            int(detail_authoring_printability_summary.requested_active_pixels_after),
        )
        _set_counter(
            performance_profile,
            "stage4_detail_authoring_prevented_layer_pixels",
            int(detail_authoring_printability_summary.prevented_layer_pixels),
        )
        _set_counter(
            performance_profile,
            "stage4_detail_authoring_prevented_active_pixels",
            int(detail_authoring_printability_summary.prevented_active_pixels),
        )
        _set_counter(
            performance_profile,
            "stage4_detail_authoring_runtime_s",
            float(detail_authoring_printability_summary.runtime_s),
        )
    detail_printability_summary = cap_plan.detail_printability_summary
    if detail_printability_summary is not None:
        _set_counter(
            performance_profile,
            "stage4_detail_printability_gate_enabled",
            bool(detail_printability_summary.enabled),
        )
        _set_counter(
            performance_profile,
            "stage4_detail_printability_suppressed_layer_pixels",
            int(detail_printability_summary.suppressed_layer_pixels),
        )
        _set_counter(
            performance_profile,
            "stage4_detail_printability_suppressed_components",
            int(detail_printability_summary.suppressed_components),
        )
        _set_counter(
            performance_profile,
            "stage4_detail_printability_accepted_components",
            int(detail_printability_summary.accepted_components),
        )
        _set_counter(
            performance_profile,
            "stage4_detail_printability_rejected_tiny_components",
            int(detail_printability_summary.rejected_tiny_components),
        )
        _set_counter(
            performance_profile,
            "stage4_detail_printability_rejected_narrow_components",
            int(detail_printability_summary.rejected_narrow_components),
        )
        _set_counter(
            performance_profile,
            "stage4_detail_printability_rejected_short_components",
            int(detail_printability_summary.rejected_short_components),
        )


__all__ = (
    '_make_staged_progress_emitter',
    '_debug_map_sink',
    '_record_debug_map',
    '_record_timing',
    '_set_counter',
    '_record_solve_context_metrics',
    '_record_stage0_metrics',
    '_record_stage1_metrics',
    '_record_stage3_metrics',
    '_record_stage4_metrics',
)
