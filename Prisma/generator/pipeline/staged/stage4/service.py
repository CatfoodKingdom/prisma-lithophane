"""Stage 4 white-cap synthesis service."""
from __future__ import annotations


import numpy as np

from ...luminance_handler import luminance_handler_enabled

from ...staged_artifacts import (
    CapSynthesisPlan,
    FillerGeometryPlan,
    PlanningDiagnosticEntry,
    PlanningDiagnosticsStream,
    Stage4BoundaryCapPrintabilitySummary,
    Stage4DetailPrintabilitySummary,
    Stage4DetailZoneSummary,
    VisibleRecipeRawGeometryPlan,
)
from ...staged_printability import resolve_blueprint_printability_settings
from ...material_exposure import positive_layer_counts

from ..cap_surface import (
    _quantized_cap_floor,
    _compute_stage4_detail_signal,
)
from ..printability_enforcement import _printability_enforcement_enabled
from ..telemetry import (
    _debug_map_sink,
    _record_debug_map,
)

from .detail import (
    _STAGE4_DEFAULT_DETAIL_MAX_LAYERS,
    _shape_stage4_detail_stack_layers,
    _limit_stage4_independent_detail_layers,
    _compute_stage4_detail_optical_gain_map,
    _build_stage4_optical_detail_surface,
    _stage4_detail_zone_min_pixels,
    _author_stage4_detail_zones,
    _author_stage4_direct_residual_detail_zones,
    _apply_stage4_detail_cap_smoothing,
)
from .metrics import record_stage4_diagnostics
from .printability import (
    _apply_stage4_boundary_cap_printability_gate,
    _stage4_detail_authoring_printability_mode,
    _stage4_detail_authoring_printability_enabled,
    _disabled_stage4_detail_authoring_printability_summary,
    _apply_stage4_luminance_detail_authoring_printability,
    _apply_stage4_detail_printability_gate,
)
from .requests import (
    _banded_luminance_cap_limit_mm,
    _requested_stage4_cap_maps,
)

def build_cap_plan(
    state,
    visible_plan: VisibleRecipeRawGeometryPlan,
    filler_plan: FillerGeometryPlan,
    diagnostics: PlanningDiagnosticsStream,
) -> CapSynthesisPlan:
    """Produce the Stage 4 cap synthesis artifact."""
    cfg = state.config
    debug_maps = _debug_map_sink(state)
    # Phase 1: derive boundary/detail requests and available cap budgets.
    layer_height = float(cfg.layer_height)
    d_wc_min = float(cfg.d_wc_min)
    floor_mm = _quantized_cap_floor(d_wc_min, layer_height)
    cap_ceiling_mm = float(cfg.effective_d_wc_max())
    banded_luminance_cap_limit_mm = _banded_luminance_cap_limit_mm(state)
    if banded_luminance_cap_limit_mm is not None:
        cap_ceiling_mm = min(cap_ceiling_mm, banded_luminance_cap_limit_mm)
    remaining_cap_budget = np.clip(
        float(cfg.t_max) - filler_plan.color_ceiling_mm,
        0.0,
        cap_ceiling_mm,
    ).astype(np.float32)
    boundary_cap_budget = np.minimum(
        remaining_cap_budget,
        np.float32(min(float(cfg.effective_boundary_d_wc_max()), cap_ceiling_mm)),
    ).astype(np.float32, copy=False)
    appearance_structural_split_enabled = (
        str(cfg.cap_mode or "smooth_variable") == "appearance_bounded_smooth"
        and not luminance_handler_enabled(cfg)
    )
    requested_cap_policy, detail_reference_cap, boundary_edge_guard_weight = _requested_stage4_cap_maps(
        state,
        visible_plan,
        filler_plan,
        diagnostics,
    )
    detail_enabled = bool(cfg.detail_cap_enabled)
    enforce_printability = _printability_enforcement_enabled(cfg)
    boundary_shield_floor_mm: np.ndarray | None = None
    shape = filler_plan.color_ceiling_mm.shape
    if enforce_printability or appearance_structural_split_enabled:
        stage2_floor = visible_plan.mandatory_lateral_boundary_shield_floor_mm
        if stage2_floor is not None:
            floor_map = np.asarray(stage2_floor, dtype=np.float32)
            if floor_map.shape != shape:
                raise ValueError(
                    "Stage 2 lateral boundary shield floor must match boundary cap shape"
                )
            boundary_shield_floor_mm = np.minimum(
                floor_map,
                boundary_cap_budget,
            ).astype(np.float32, copy=False)
            state.debug_maps["stage2_lateral_boundary_shield_floor"] = (
                boundary_shield_floor_mm.astype(np.float32, copy=True)
            )
            over_budget_pixels = int(np.count_nonzero(floor_map > boundary_cap_budget))
            if over_budget_pixels > 0:
                diagnostics.entries.append(
                    PlanningDiagnosticEntry(
                        code="stage2_lateral_boundary_shield_floor_over_budget",
                        severity="warning",
                        message=(
                            "Stage 2 lateral boundary shield floor exceeded the "
                            f"boundary-cap budget at {over_budget_pixels} pixels; "
                            "final exposure audit must remain the product gate."
                        ),
                    )
                )
        if visible_plan.mandatory_lateral_boundary_shield_floor_active_pixels > 0:
            floor_layers = positive_layer_counts(
                boundary_shield_floor_mm
                if boundary_shield_floor_mm is not None
                else np.zeros(shape, dtype=np.float32),
                layer_height,
            )
            diagnostics.entries.append(
                PlanningDiagnosticEntry(
                    code="stage2_lateral_boundary_shield_floor_preserved",
                    severity="info",
                    message=(
                        "Stage 4 is preserving the Stage 2 lateral boundary "
                        f"shield floor: {int(np.sum(floor_layers))} layer-pixels "
                        f"across {int(np.count_nonzero(floor_layers > 0))} pixels; "
                        f"max {int(np.max(floor_layers, initial=0))} layers."
                    ),
                )
            )
    # Phases 2-3: shape the structural boundary and enforce its hard bounds.
    requested_boundary_cap = requested_cap_policy.astype(np.float32, copy=False)
    desired_final_cap_target: np.ndarray | None = None
    if appearance_structural_split_enabled:
        top_cover_floor = np.full(shape, np.float32(floor_mm), dtype=np.float32)
        structural_floor = top_cover_floor
        if boundary_shield_floor_mm is not None:
            structural_floor = np.maximum(
                structural_floor,
                boundary_shield_floor_mm,
            ).astype(np.float32, copy=False)
        boundary_cap_target = np.minimum(
            structural_floor,
            boundary_cap_budget,
        ).astype(np.float32, copy=False)
        desired_final_cap_target = np.minimum(
            np.maximum(requested_boundary_cap, boundary_cap_target),
            remaining_cap_budget,
        ).astype(np.float32, copy=False)
        _record_debug_map(
            debug_maps,
            "stage4_boundary_minimal_floor_mm",
            structural_floor.astype(np.float32, copy=False),
        )
        _record_debug_map(
            debug_maps,
            "stage4_appearance_desired_final_cap_mm",
            desired_final_cap_target,
        )
    else:
        boundary_cap_target = np.minimum(
            requested_boundary_cap,
            boundary_cap_budget,
        ).astype(np.float32, copy=False)
    configured_detail_max_layers = cfg.detail_cap_max_layers
    user_detail_max_layers = (
        int(_STAGE4_DEFAULT_DETAIL_MAX_LAYERS)
        if configured_detail_max_layers is None
        else max(0, int(configured_detail_max_layers))
    )
    boundary_cap_printability_repair_map: np.ndarray | None = None
    boundary_cap_printability_summary = Stage4BoundaryCapPrintabilitySummary(
        enabled=False,
        flagged_layer_pixels=0,
        flagged_components=0,
        grown_layer_pixels=0,
        grown_components=0,
        suppressed_optional_layer_pixels=0,
        suppressed_optional_components=0,
        preserved_mandatory_layer_pixels=0,
        preserved_mandatory_components=0,
        accepted_components=0,
        rejected_tiny_components=0,
        rejected_narrow_components=0,
        rejected_short_components=0,
    )
    boundary_cap_height = boundary_cap_target.astype(np.float32, copy=False)
    boundary_printability_repair_applied = False
    if appearance_structural_split_enabled and enforce_printability:
        printability_settings = resolve_blueprint_printability_settings(
            cfg,
            pitch_mm=float(cfg.solver_fine_pitch_mm),
        )
        boundary_cap_printability_gate = _apply_stage4_boundary_cap_printability_gate(
            boundary_cap_height_mm=boundary_cap_height,
            settings=printability_settings,
            color_ceiling_mm=filler_plan.color_ceiling_mm,
            max_boundary_cap_height_mm=boundary_cap_budget,
            minimum_boundary_cap_height_mm=floor_mm,
            minimum_boundary_cap_height_map_mm=boundary_shield_floor_mm,
            apply_changes=True,
        )
        boundary_cap_height = (
            boundary_cap_printability_gate.boundary_cap_height_mm.astype(
                np.float32,
                copy=False,
            )
        )
        boundary_cap_printability_repair_map = boundary_cap_printability_gate.rejection_map
        boundary_cap_printability_summary = boundary_cap_printability_gate.summary
        boundary_printability_repair_applied = True
        assert desired_final_cap_target is not None
        desired_final_cap_target = np.minimum(
            np.maximum(desired_final_cap_target, boundary_cap_height),
            remaining_cap_budget,
        ).astype(np.float32, copy=False)
        _record_debug_map(
            debug_maps,
            "stage4_appearance_desired_final_cap_mm",
            desired_final_cap_target,
        )
    if appearance_structural_split_enabled:
        _record_debug_map(
            debug_maps,
            "stage4_boundary_structural_cap_mm",
            boundary_cap_height.astype(np.float32, copy=False),
        )
    # Phase 4: author optional optical/detail zones.
    final_cap_target = boundary_cap_height.astype(np.float32, copy=False)
    if detail_enabled:
        optical_gain_map: np.ndarray | None = None
        direct_residual_detail = bool(appearance_structural_split_enabled)
        if appearance_structural_split_enabled:
            assert desired_final_cap_target is not None
            raw_residual = np.maximum(
                desired_final_cap_target - boundary_cap_height,
                np.float32(0.0),
            ).astype(np.float32, copy=False)
            requested_detail_layers = _limit_stage4_independent_detail_layers(
                raw_residual,
                available_detail_mm=np.maximum(
                    remaining_cap_budget - boundary_cap_height,
                    np.float32(0.0),
                ),
                layer_height=layer_height,
                max_layers=user_detail_max_layers,
            )
            candidate_final_cap_target = np.minimum(
                boundary_cap_height + requested_detail_layers,
                desired_final_cap_target,
            ).astype(np.float32, copy=False)
            _record_debug_map(
                debug_maps,
                "stage4_detail_residual_from_appearance_target_mm",
                raw_residual,
            )
        else:
            requested_detail_layers, optical_gain_map = _build_stage4_optical_detail_surface(
                state=state,
                visible_plan=visible_plan,
                boundary_cap_height=boundary_cap_height,
                remaining_cap_budget=remaining_cap_budget,
                max_layers=user_detail_max_layers,
            )
            candidate_final_cap_target = np.minimum(
                boundary_cap_height + requested_detail_layers,
                remaining_cap_budget,
            ).astype(np.float32, copy=False)
        candidate_detail_mask = np.asarray(requested_detail_layers > 1e-9, dtype=bool)
        detail_signal = _compute_stage4_detail_signal(visible_plan)
        detail_mask = candidate_detail_mask
        signal_threshold_for_authoring: float | None = None
        boundary_cap_height = boundary_cap_height.astype(np.float32, copy=False)
        if optical_gain_map is None:
            optical_gain_map = _compute_stage4_detail_optical_gain_map(
                state=state,
                visible_plan=visible_plan,
                boundary_cap_height=boundary_cap_height,
                final_cap_target=candidate_final_cap_target,
                detail_mask=detail_mask,
            )
        _record_debug_map(
            debug_maps,
            "stage4_detail_optical_gain_map",
            optical_gain_map,
        )
        _record_debug_map(
            debug_maps,
            "stage4_detail_best_layers_pre_authoring_mm",
            requested_detail_layers,
        )
        _record_debug_map(
            debug_maps,
            "stage4_detail_signal_map",
            detail_signal,
        )
        _record_debug_map(
            debug_maps,
            "stage4_detail_candidate_mask_pre_zone",
            detail_mask.astype(np.float32, copy=False),
        )
        if direct_residual_detail:
            (
                detail_mask,
                detail_zone_label_map,
                detail_candidate_zone_label_map,
                detail_zone_rejection_reason_map,
                detail_zone_summary,
                detail_zone_facts,
            ) = _author_stage4_direct_residual_detail_zones(
                state=state,
                detail_mask=detail_mask,
                requested_detail_layers=requested_detail_layers,
                optical_gain_map=optical_gain_map,
                detail_signal=detail_signal,
            )
        else:
            (
                detail_mask,
                detail_zone_label_map,
                detail_candidate_zone_label_map,
                detail_zone_rejection_reason_map,
                detail_zone_summary,
                detail_zone_facts,
            ) = _author_stage4_detail_zones(
                state=state,
                detail_mask=detail_mask,
                requested_detail_layers=requested_detail_layers,
                optical_gain_map=optical_gain_map,
                detail_signal=detail_signal,
                signal_threshold=signal_threshold_for_authoring,
                enabled=True,
                recipe_boundary_support=None,
            )
        _record_debug_map(
            debug_maps,
            "stage4_detail_candidate_zone_labels",
            detail_candidate_zone_label_map,
        )
        _record_debug_map(
            debug_maps,
            "stage4_detail_zone_labels",
            detail_zone_label_map,
        )
        _record_debug_map(
            debug_maps,
            "stage4_detail_rejection_reasons",
            detail_zone_rejection_reason_map,
        )
        requested_detail_layers = _shape_stage4_detail_stack_layers(
            detail_mask=detail_mask,
            requested_detail_layers=requested_detail_layers,
            detail_signal=detail_signal,
            signal_threshold=signal_threshold_for_authoring,
            layer_height=layer_height,
        )
        _record_debug_map(
            debug_maps,
            "stage4_detail_requested_layers_post_authoring_mm",
            requested_detail_layers,
        )
        if appearance_structural_split_enabled:
            assert desired_final_cap_target is not None
            selected_detail_height = np.minimum(
                requested_detail_layers,
                np.maximum(
                    desired_final_cap_target - boundary_cap_height,
                    np.float32(0.0),
                ),
            ).astype(np.float32, copy=False)
            smooth_residual_height = np.maximum(
                desired_final_cap_target - boundary_cap_height - selected_detail_height,
                np.float32(0.0),
            ).astype(np.float32, copy=False)
            if np.any(smooth_residual_height > np.float32(1e-9)):
                boundary_cap_height = np.minimum(
                    boundary_cap_height + smooth_residual_height,
                    desired_final_cap_target,
                ).astype(np.float32, copy=False)
                _record_debug_map(
                    debug_maps,
                    "stage4_boundary_smooth_residual_mm",
                    smooth_residual_height,
                )
            final_cap_target = np.minimum(
                boundary_cap_height + selected_detail_height,
                desired_final_cap_target,
            ).astype(np.float32, copy=False)
            requested_detail_layers = selected_detail_height
        else:
            final_cap_target = np.minimum(
                boundary_cap_height + requested_detail_layers,
                remaining_cap_budget,
            ).astype(np.float32, copy=False)
        detail_height = (final_cap_target - boundary_cap_height).astype(
            np.float32,
            copy=False,
        )
        _record_debug_map(
            debug_maps,
            "stage4_detail_final_height_mm",
            detail_height,
        )
    else:
        boundary_cap_height = final_cap_target.astype(np.float32, copy=False)
        detail_height = np.zeros_like(final_cap_target, dtype=np.float32)
        detail_candidate_zone_label_map = np.full(final_cap_target.shape, -1, dtype=np.int32)
        detail_zone_label_map = np.full(final_cap_target.shape, -1, dtype=np.int32)
        detail_zone_rejection_reason_map = np.full(final_cap_target.shape, -1, dtype=np.int32)
        detail_zone_summary = Stage4DetailZoneSummary(
            enabled=False,
            min_zone_pixels=int(_stage4_detail_zone_min_pixels(state)),
            candidate_pixels=0,
            candidate_zone_count=0,
            active_pixels=0,
            rejected_pixels=0,
            zone_count=0,
            rejected_zone_count=0,
            rejected_too_small_zone_count=0,
            rejected_weak_optical_gain_zone_count=0,
            rejected_weak_signal_zone_count=0,
            largest_zone_pixels=0,
            mean_zone_pixels=0.0,
            mean_zone_optical_gain=0.0,
            mean_zone_structure_support=0.0,
            mean_zone_recipe_boundary_support=0.0,
        )
        detail_zone_facts = ()

    # Phase 5: smooth and physically enforce the authored cap layers.
    detail_cap_smoothing_summary: dict[str, object] | None = None
    detail_height, detail_cap_smoothing_summary = _apply_stage4_detail_cap_smoothing(
        detail_height_mm=detail_height,
        cfg=cfg,
        layer_height=layer_height,
        boundary_cap_height_mm=boundary_cap_height,
        remaining_cap_budget_mm=remaining_cap_budget,
        desired_final_cap_target_mm=(
            desired_final_cap_target
            if appearance_structural_split_enabled
            else None
        ),
    )
    if detail_cap_smoothing_summary is not None:
        final_cap_target = np.minimum(
            boundary_cap_height + detail_height,
            remaining_cap_budget,
        ).astype(np.float32, copy=False)
        if appearance_structural_split_enabled and desired_final_cap_target is not None:
            final_cap_target = np.minimum(
                final_cap_target,
                desired_final_cap_target,
            ).astype(np.float32, copy=False)
        _record_debug_map(
            debug_maps,
            "stage4_detail_smoothed_height_mm",
            detail_height,
        )
        diagnostics.entries.append(
            PlanningDiagnosticEntry(
                code="stage4_detail_cap_smoothing",
                severity="info",
                message=(
                    "Stage 4 detail smoothing changed "
                    f"{int(detail_cap_smoothing_summary.get('changed_px', 0))} "
                    "pixels before final printability gates."
                ),
            )
        )

    detail_authoring_printability_rejection_map: np.ndarray | None = None
    detail_authoring_printability_summary = _disabled_stage4_detail_authoring_printability_summary(
        cfg
    )
    if enforce_printability and not boundary_printability_repair_applied:
        printability_settings = resolve_blueprint_printability_settings(
            cfg,
            pitch_mm=float(cfg.solver_fine_pitch_mm),
        )
        # Boundary cap is structural.  This gate may grow boundary-cap coverage
        # or suppress optional over-cap material, but it must preserve the
        # mandatory quantized floor handed off by Stage 2.
        boundary_cap_printability_gate = _apply_stage4_boundary_cap_printability_gate(
            boundary_cap_height_mm=boundary_cap_height,
            settings=printability_settings,
            color_ceiling_mm=filler_plan.color_ceiling_mm,
            max_boundary_cap_height_mm=boundary_cap_budget,
            minimum_boundary_cap_height_mm=floor_mm,
            minimum_boundary_cap_height_map_mm=boundary_shield_floor_mm,
            apply_changes=True,
        )
        boundary_cap_height = (
            boundary_cap_printability_gate.boundary_cap_height_mm.astype(
                np.float32,
                copy=False,
            )
        )
        boundary_cap_printability_repair_map = (
            boundary_cap_printability_gate.rejection_map
        )
        boundary_cap_printability_summary = boundary_cap_printability_gate.summary
        detail_height = np.minimum(
            detail_height,
            np.maximum(remaining_cap_budget - boundary_cap_height, 0.0),
        ).astype(np.float32, copy=False)
        if _stage4_detail_authoring_printability_enabled(
            config=cfg,
            detail_enabled=detail_enabled,
            enforce_printability=enforce_printability,
        ):
            detail_authoring = _apply_stage4_luminance_detail_authoring_printability(
                detail_height_mm=detail_height,
                settings=printability_settings,
                color_ceiling_mm=filler_plan.color_ceiling_mm,
                boundary_cap_height_mm=boundary_cap_height,
                remaining_cap_budget_mm=remaining_cap_budget,
                mode=_stage4_detail_authoring_printability_mode(cfg),
            )
            detail_height = detail_authoring.detail_height_mm.astype(
                np.float32,
                copy=False,
            )
            detail_authoring_printability_rejection_map = (
                detail_authoring.rejection_map
            )
            detail_authoring_printability_summary = detail_authoring.summary
        final_cap_target = np.minimum(
            boundary_cap_height + detail_height,
            remaining_cap_budget,
        ).astype(np.float32, copy=False)
        if appearance_structural_split_enabled and desired_final_cap_target is not None:
            final_cap_target = np.minimum(
                final_cap_target,
                desired_final_cap_target,
            ).astype(np.float32, copy=False)

    detail_printability_suppression_map: np.ndarray | None = None
    detail_printability_summary = Stage4DetailPrintabilitySummary(
        enabled=False,
        suppressed_layer_pixels=0,
        suppressed_components=0,
        accepted_components=0,
        rejected_tiny_components=0,
        rejected_narrow_components=0,
        rejected_short_components=0,
    )
    if detail_enabled and enforce_printability:
        printability_settings = resolve_blueprint_printability_settings(
            cfg,
            pitch_mm=float(cfg.solver_fine_pitch_mm),
        )
        # Detail is optional material above the boundary cap, so hard-failing
        # features can be suppressed without violating the boundary-cap floor.
        detail_printability_gate = _apply_stage4_detail_printability_gate(
            detail_height_mm=detail_height,
            settings=printability_settings,
            base_top_mm=filler_plan.color_ceiling_mm + boundary_cap_height,
            color_ceiling_mm=filler_plan.color_ceiling_mm,
            boundary_cap_height_mm=boundary_cap_height,
        )
        detail_height = detail_printability_gate.detail_height_mm.astype(
            np.float32,
            copy=False,
        )
        detail_printability_suppression_map = detail_printability_gate.rejection_map
        detail_printability_summary = detail_printability_gate.summary
        final_cap_target = np.minimum(
            boundary_cap_height + detail_height,
            remaining_cap_budget,
        ).astype(np.float32, copy=False)

    if enforce_printability:
        printability_settings = resolve_blueprint_printability_settings(
            cfg,
            pitch_mm=float(cfg.solver_fine_pitch_mm),
        )
        boundary_cap_cleanup = _apply_stage4_boundary_cap_printability_gate(
            boundary_cap_height_mm=boundary_cap_height,
            settings=printability_settings,
            color_ceiling_mm=filler_plan.color_ceiling_mm,
            max_boundary_cap_height_mm=boundary_cap_budget,
            minimum_boundary_cap_height_mm=floor_mm,
            minimum_boundary_cap_height_map_mm=boundary_shield_floor_mm,
            apply_changes=True,
            repair_with_growth=False,
        )
        if int(boundary_cap_cleanup.summary.suppressed_optional_layer_pixels) > 0:
            boundary_cap_height = boundary_cap_cleanup.boundary_cap_height_mm.astype(
                np.float32,
                copy=False,
            )
            boundary_cap_printability_repair_map = (
                boundary_cap_cleanup.rejection_map
                if boundary_cap_printability_repair_map is None
                else np.bitwise_or(
                    boundary_cap_printability_repair_map.astype(
                        np.uint8,
                        copy=False,
                    ),
                    boundary_cap_cleanup.rejection_map.astype(np.uint8, copy=False),
                )
            )
            previous = boundary_cap_printability_summary
            current = boundary_cap_cleanup.summary
            boundary_cap_printability_summary = Stage4BoundaryCapPrintabilitySummary(
                enabled=True,
                flagged_layer_pixels=int(previous.flagged_layer_pixels)
                + int(current.flagged_layer_pixels),
                flagged_components=int(previous.flagged_components)
                + int(current.flagged_components),
                grown_layer_pixels=int(previous.grown_layer_pixels)
                + int(current.grown_layer_pixels),
                grown_components=int(previous.grown_components)
                + int(current.grown_components),
                suppressed_optional_layer_pixels=int(
                    previous.suppressed_optional_layer_pixels
                )
                + int(current.suppressed_optional_layer_pixels),
                suppressed_optional_components=int(
                    previous.suppressed_optional_components
                )
                + int(current.suppressed_optional_components),
                preserved_mandatory_layer_pixels=int(
                    previous.preserved_mandatory_layer_pixels
                )
                + int(current.preserved_mandatory_layer_pixels),
                preserved_mandatory_components=int(
                    previous.preserved_mandatory_components
                )
                + int(current.preserved_mandatory_components),
                accepted_components=int(previous.accepted_components)
                + int(current.accepted_components),
                rejected_tiny_components=int(previous.rejected_tiny_components)
                + int(current.rejected_tiny_components),
                rejected_narrow_components=int(previous.rejected_narrow_components)
                + int(current.rejected_narrow_components),
                rejected_short_components=int(previous.rejected_short_components)
                + int(current.rejected_short_components),
            )
        available_after_boundary = np.maximum(
            remaining_cap_budget - boundary_cap_height,
            0.0,
        ).astype(np.float32, copy=False)
        if appearance_structural_split_enabled and desired_final_cap_target is not None:
            available_after_boundary = np.minimum(
                available_after_boundary,
                np.maximum(desired_final_cap_target - boundary_cap_height, 0.0),
            ).astype(np.float32, copy=False)
        detail_height = np.minimum(
            detail_height,
            available_after_boundary,
        ).astype(np.float32, copy=False)
        if detail_enabled:
            detail_cleanup = _apply_stage4_detail_printability_gate(
                detail_height_mm=detail_height,
                settings=printability_settings,
                base_top_mm=filler_plan.color_ceiling_mm + boundary_cap_height,
                color_ceiling_mm=filler_plan.color_ceiling_mm,
                boundary_cap_height_mm=boundary_cap_height,
            )
            if int(detail_cleanup.summary.suppressed_layer_pixels) > 0:
                detail_height = detail_cleanup.detail_height_mm.astype(
                    np.float32,
                    copy=False,
                )
                detail_printability_suppression_map = (
                    detail_cleanup.rejection_map
                    if detail_printability_suppression_map is None
                    else np.bitwise_or(
                        detail_printability_suppression_map.astype(np.uint8, copy=False),
                        detail_cleanup.rejection_map.astype(np.uint8, copy=False),
                    )
                )
                previous_detail = detail_printability_summary
                current_detail = detail_cleanup.summary
                detail_printability_summary = Stage4DetailPrintabilitySummary(
                    enabled=True,
                    suppressed_layer_pixels=int(previous_detail.suppressed_layer_pixels)
                    + int(current_detail.suppressed_layer_pixels),
                    suppressed_components=int(previous_detail.suppressed_components)
                    + int(current_detail.suppressed_components),
                    accepted_components=int(previous_detail.accepted_components)
                    + int(current_detail.accepted_components),
                    rejected_tiny_components=int(
                        previous_detail.rejected_tiny_components
                    )
                    + int(current_detail.rejected_tiny_components),
                    rejected_narrow_components=int(
                        previous_detail.rejected_narrow_components
                    )
                    + int(current_detail.rejected_narrow_components),
                    rejected_short_components=int(
                        previous_detail.rejected_short_components
                    )
                    + int(current_detail.rejected_short_components),
                )
        final_cap_target = np.minimum(
            boundary_cap_height + detail_height,
            remaining_cap_budget,
        ).astype(np.float32, copy=False)
        if appearance_structural_split_enabled and desired_final_cap_target is not None:
            final_cap_target = np.minimum(
                final_cap_target,
                desired_final_cap_target,
            ).astype(np.float32, copy=False)

    if appearance_structural_split_enabled and desired_final_cap_target is not None:
        structural_layers = positive_layer_counts(boundary_cap_height, layer_height)
        residual_after_boundary = np.maximum(
            desired_final_cap_target - boundary_cap_height,
            np.float32(0.0),
        ).astype(np.float32, copy=False)
        equivalence_delta = (
            desired_final_cap_target - final_cap_target
        ).astype(np.float32, copy=False)
        _record_debug_map(
            debug_maps,
            "stage4_boundary_structural_cap_mm",
            boundary_cap_height.astype(np.float32, copy=False),
        )
        _record_debug_map(
            debug_maps,
            "stage4_detail_residual_from_appearance_target_mm",
            residual_after_boundary,
        )
        _record_debug_map(
            debug_maps,
            "stage4_final_target_equivalence_delta_mm",
            equivalence_delta,
        )
        diagnostics.entries.append(
            PlanningDiagnosticEntry(
                code="stage4_boundary_structural_split",
                severity="info",
                message=(
                    "Stage 4 structural boundary split is active; structural "
                    f"boundary active pixels {int(np.count_nonzero(structural_layers > 0))}, "
                    f"max layers {int(np.max(structural_layers, initial=0))}."
                ),
            )
        )
        delta_abs = np.abs(equivalence_delta)
        delta_max = float(np.max(delta_abs, initial=0.0))
        delta_mean = float(np.mean(delta_abs)) if delta_abs.size else 0.0
        diagnostics.entries.append(
            PlanningDiagnosticEntry(
                code="stage4_final_target_equivalence_delta",
                severity="warning" if delta_max > float(layer_height) * 0.25 else "info",
                message=(
                    "Stage 4 split final-cap delta vs appearance target "
                    f"mean/max = {delta_mean:.4f}/{delta_max:.4f}mm."
                ),
            )
        )

    # Phase 6: materialize the cap artifact and project diagnostics.
    cap_height = final_cap_target.astype(np.float32, copy=False)
    cap_boundary_top = (
        filler_plan.color_ceiling_mm + boundary_cap_height
    ).astype(np.float32, copy=False)
    final_visible_top = (
        filler_plan.color_ceiling_mm + final_cap_target
    ).astype(np.float32, copy=False)
    _record_debug_map(
        debug_maps,
        "stage4_detail_final_height_mm",
        detail_height,
    )

    cap_policy_reference = (
        desired_final_cap_target
        if appearance_structural_split_enabled and desired_final_cap_target is not None
        else requested_boundary_cap
    )
    record_stage4_diagnostics(
        diagnostics,
        cap_height=cap_height,
        cap_policy_reference=cap_policy_reference,
        final_cap_target=final_cap_target,
        detail_height=detail_height,
        detail_zone_summary=detail_zone_summary,
        boundary_cap_printability_summary=boundary_cap_printability_summary,
        detail_authoring_printability_summary=(
            detail_authoring_printability_summary
        ),
        detail_printability_summary=detail_printability_summary,
    )
    return CapSynthesisPlan(
        cap_boundary_top_mm=cap_boundary_top.astype(np.float32, copy=True),
        cap_height_mm=cap_height.astype(np.float32, copy=True),
        boundary_edge_guard_weight=boundary_edge_guard_weight.astype(np.float32, copy=True),
        detail_height_mm=detail_height.astype(np.float32, copy=True),
        detail_candidate_zone_label_map=detail_candidate_zone_label_map.astype(np.int32, copy=True),
        detail_zone_label_map=detail_zone_label_map.astype(np.int32, copy=True),
        detail_zone_rejection_reason_map=detail_zone_rejection_reason_map.astype(np.int32, copy=True),
        detail_zone_summary=detail_zone_summary,
        detail_zone_facts=detail_zone_facts,
        final_visible_top_mm=final_visible_top.astype(np.float32, copy=True),
        boundary_cap_printability_repair_map=(
            None
            if boundary_cap_printability_repair_map is None
            else boundary_cap_printability_repair_map.astype(np.uint8, copy=True)
        ),
        boundary_cap_printability_summary=boundary_cap_printability_summary,
        detail_authoring_printability_rejection_map=(
            None
            if detail_authoring_printability_rejection_map is None
            else detail_authoring_printability_rejection_map.astype(np.uint8, copy=True)
        ),
        detail_authoring_printability_summary=detail_authoring_printability_summary,
        detail_printability_suppression_map=(
            None
            if detail_printability_suppression_map is None
            else detail_printability_suppression_map.astype(np.uint8, copy=True)
        ),
        detail_printability_summary=detail_printability_summary,
        detail_cap_smoothing_summary=detail_cap_smoothing_summary,
    )

__all__ = (
    'build_cap_plan',
)
