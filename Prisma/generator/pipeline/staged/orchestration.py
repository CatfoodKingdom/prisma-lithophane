"""Stage 0-5 backend orchestration."""
from __future__ import annotations

import time


from ..luminance_handler import luminance_handler_enabled

from ..staged_artifacts import (
    StagedPerformanceProfile,
    StagedBackendResult,
    PlanningDiagnosticsStream,
)
from ..staged_bridge import build_compatibility_bundle

from .postsolve_diagnostics import _run_postsolve_diagnostics
from .stage0_directives import compile_directives
from .stage1_zones import build_zone_plan
from .stage2 import build_visible_plan
from .stage3_filler import build_filler_plan
from .stage4 import build_cap_plan
from .telemetry import (
    _make_staged_progress_emitter,
    _record_solve_context_metrics,
    _record_stage0_metrics,
    _record_stage1_metrics,
    _record_stage3_metrics,
    _record_stage4_metrics,
    _record_timing,
    _set_counter,
)

def run_staged_backend_path(state, progress=None) -> StagedBackendResult:
    """Run the Stage 0-5 proof-slice backend on a prepared PipelineState."""
    total_start = time.perf_counter()
    diagnostics = PlanningDiagnosticsStream()
    performance_profile = StagedPerformanceProfile()
    _record_solve_context_metrics(performance_profile, state)
    _emit = _make_staged_progress_emitter(progress, total_start)

    _emit("Planning solve directives...", 2)
    stage_start = time.perf_counter()
    compiled_directives, receipts = compile_directives(state)
    _record_stage0_metrics(performance_profile, compiled_directives, stage_start)

    _emit("Planning color regions...", 12)
    stage_start = time.perf_counter()
    zone_plan = build_zone_plan(state, diagnostics)
    _record_stage1_metrics(performance_profile, state, zone_plan, stage_start)

    _emit("Solving visible color stacks...", 28)
    visible_plan = build_visible_plan(
        state,
        compiled_directives,
        zone_plan,
        diagnostics,
        performance_profile=performance_profile,
    )

    _emit("Planning filler support...", 52)
    stage_start = time.perf_counter()
    filler_plan = build_filler_plan(state, visible_plan, diagnostics)
    _record_stage3_metrics(performance_profile, filler_plan, stage_start)

    _emit("Solving white cap structure...", 62)
    stage_start = time.perf_counter()
    cap_plan = build_cap_plan(state, visible_plan, filler_plan, diagnostics)
    _record_stage4_metrics(performance_profile, state, cap_plan, stage_start)
    _run_postsolve_diagnostics(
        state=state,
        visible_plan=visible_plan,
        filler_plan=filler_plan,
        cap_plan=cap_plan,
        diagnostics=diagnostics,
        performance_profile=performance_profile,
        emit=_emit,
    )

    _emit("Building solved material maps...", 92)
    stage_start = time.perf_counter()
    compatibility_bundle = build_compatibility_bundle(
        palette=list(state.config.palette),
        solver_fine_pitch_mm=float(state.config.solver_fine_pitch_mm),
        layer_height_mm=float(state.config.layer_height),
        d_wb_mm=float(state.config.d_wb),
        d_wc_min_mm=float(state.config.d_wc_min),
        t_max_mm=float(state.config.t_max),
        effective_d_wc_max_mm=float(state.config.effective_d_wc_max()),
        effective_boundary_d_wc_max_mm=float(
            state.config.effective_boundary_d_wc_max()
        ),
        luminance_mode=(
            "luminance_detail"
            if luminance_handler_enabled(state.config)
            else "standard"
        ),
        cap_mode=str(state.config.cap_mode or "smooth_variable"),
        visible_plan=visible_plan,
        filler_plan=filler_plan,
        cap_plan=cap_plan,
    )
    _record_timing(performance_profile, "stage5_bridge_s", time.perf_counter() - stage_start)
    _set_counter(
        performance_profile,
        "stage5_bridge_thickness_map_count",
        int(len(compatibility_bundle.thickness_maps)),
    )
    _set_counter(
        performance_profile,
        "stage5_bridge_debug_map_count",
        int(len(compatibility_bundle.debug_maps)),
    )
    _record_timing(
        performance_profile,
        "staged_backend_total_s",
        time.perf_counter() - total_start,
    )
    _emit("Solved material maps ready", 100)
    return StagedBackendResult(
        compiled_directives=compiled_directives,
        receipts=receipts,
        planning_diagnostics=diagnostics,
        performance_profile=performance_profile,
        lateral_zone_plan=zone_plan,
        visible_plan=visible_plan,
        filler_plan=filler_plan,
        cap_plan=cap_plan,
        compatibility_bundle=compatibility_bundle,
    )

__all__ = (
    'run_staged_backend_path',
)
