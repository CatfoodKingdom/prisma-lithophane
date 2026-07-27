"""Stage 4 cap-request construction."""
from __future__ import annotations


import numpy as np

from ...luminance_handler import (
    LuminanceHandler,
    luminance_handler_enabled,
)

from ...staged_artifacts import (
    FillerGeometryPlan,
    PlanningDiagnosticEntry,
    PlanningDiagnosticsStream,
    VisibleRecipeRawGeometryPlan,
)

from ..cap_surface import (
    _quantize_cap_map,
    _continuity_cleanup_cap_map,
)
from ..telemetry import (
    _debug_map_sink,
    _record_debug_map,
)

from .boundary import (
    _build_stage4_boundary_smoothing_guide,
    _smooth_stage4_boundary_cap,
    _build_stage4_boundary_edge_guard,
    _apply_stage4_edge_aware_boundary_restore,
    _apply_stage4_boundary_appearance_bound,
)

_STAGE4_SUPPORTED_CAP_MODES = frozenset(
    {"smooth_variable", "appearance_bounded_smooth"}
)

def _banded_luminance_cap_limit_mm(state) -> float | None:
    cfg = state.config
    if not luminance_handler_enabled(cfg):
        return None
    swap_grouping = getattr(state, "swap_grouping", None)
    if swap_grouping is None:
        return None
    cap_limit = float(swap_grouping["cap_limit_mm"])
    if not np.isfinite(cap_limit):
        raise ValueError("Banded luminance cap limit must be finite")
    return cap_limit

def _stage4_banded_white_fill_mm(
    state,
    visible_plan: VisibleRecipeRawGeometryPlan,
) -> np.ndarray:
    """Return the authoritative total solver-priced white fill at Stage 4."""
    from grouping.band_plan import band_fill_maps

    shape = visible_plan.recipe_label_map.shape
    thickness_maps = {
        str(fid): np.zeros(shape, dtype=np.float32)
        for fid in state.config.palette
    }
    recipe_label_map = np.asarray(visible_plan.recipe_label_map, dtype=np.int32)
    for recipe_label, recipe in enumerate(visible_plan.recipe_table):
        mask = recipe_label_map == int(recipe_label)
        if not np.any(mask):
            continue
        for fid, thickness in recipe.thickness_by_filament:
            if str(fid) in thickness_maps:
                thickness_maps[str(fid)][mask] = np.float32(thickness)

    swap_grouping = state.swap_grouping
    fill_maps = band_fill_maps(
        thickness_maps,
        swap_grouping["groups"],
        swap_grouping["band_layers"],
        layer_height=float(state.config.layer_height),
    )
    if not fill_maps:
        return np.zeros(shape, dtype=np.float32)
    return np.add.reduce(fill_maps).astype(np.float32, copy=False)

def _requested_stage4_cap_maps(
    state,
    visible_plan: VisibleRecipeRawGeometryPlan,
    filler_plan: FillerGeometryPlan,
    diagnostics: PlanningDiagnosticsStream,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Build the Stage 4 boundary request plus unsmoothed detail reference."""
    cfg = state.config
    shape = filler_plan.color_ceiling_mm.shape
    debug_maps = _debug_map_sink(state)
    layer_height = float(cfg.layer_height)
    d_wc_min = float(cfg.d_wc_min)
    d_wc_max = float(cfg.effective_d_wc_max())
    boundary_d_wc_max = float(
        cfg.effective_boundary_d_wc_max()
        if hasattr(cfg, "effective_boundary_d_wc_max")
        else d_wc_max
    )
    luminance_enabled = luminance_handler_enabled(cfg)
    cap_limit_mm = _banded_luminance_cap_limit_mm(state)
    white_fill_mm: np.ndarray | None = None
    if cap_limit_mm is not None:
        d_wc_max = min(d_wc_max, cap_limit_mm)
        boundary_d_wc_max = min(boundary_d_wc_max, cap_limit_mm)
        white_fill_mm = _stage4_banded_white_fill_mm(state, visible_plan)
        _record_debug_map(
            debug_maps,
            "luminance_handler_stage4_white_fill_mm",
            white_fill_mm,
        )
    cap_mode = str(cfg.cap_mode or "smooth_variable")
    if cap_mode not in _STAGE4_SUPPORTED_CAP_MODES:
        raise ValueError(f"Unsupported Stage 4 cap_mode: {cap_mode!r}")

    raw_requested = np.asarray(
        visible_plan.implied_cap_height_mm,
        dtype=np.float32,
    ).reshape(shape)
    color_ceiling = np.asarray(filler_plan.color_ceiling_mm, dtype=np.float32)
    if luminance_enabled:
        handler = LuminanceHandler(
            cfg,
            state.profiles,
            appearance_provider=getattr(state, "appearance_provider", None),
        )
        build_kwargs = {
            "target_oklab": visible_plan.mapped_target_oklab,
            "shape": shape,
            "raw_implied_cap_mm": raw_requested,
            "color_ceiling_mm": color_ceiling,
        }
        if cap_limit_mm is not None:
            build_kwargs.update(
                {
                    "white_fill_mm": white_fill_mm,
                    "cap_limit_mm": cap_limit_mm,
                }
            )
        guidance = handler.build(**build_kwargs)
        raw_reference = guidance.boundary_cap_request_mm.astype(np.float32, copy=False)
        detail_reference = guidance.detail_cap_reference_mm.astype(np.float32, copy=False)
        _record_debug_map(
            debug_maps,
            "luminance_handler_stage4_boundary_request",
            raw_reference,
        )
        _record_debug_map(
            debug_maps,
            "luminance_handler_stage4_detail_reference",
            detail_reference,
        )
        diagnostics.entries.append(
            PlanningDiagnosticEntry(
                code="stage4_luminance_handler",
                severity="info",
                message=(
                    "Stage 4 luminance handler requested boundary authority "
                    f"{guidance.reference.boundary_authority_mm:.3f}mm, "
                    f"boundary mean {guidance.diagnostics['boundary_request_mean_mm']:.3f}mm, "
                    f"detail reference mean {guidance.diagnostics['detail_reference_mean_mm']:.3f}mm."
                ),
            )
        )
    else:
        raw_reference = _quantize_cap_map(
            raw_requested,
            layer_height=layer_height,
            d_wc_min=d_wc_min,
            d_wc_max=boundary_d_wc_max,
        ).astype(np.float32, copy=False)
        detail_reference = _quantize_cap_map(
            raw_requested,
            layer_height=layer_height,
            d_wc_min=d_wc_min,
            d_wc_max=d_wc_max,
        ).astype(np.float32, copy=False)
    raw_top_reference = (color_ceiling + raw_reference).astype(np.float32, copy=False)
    requested_top = raw_top_reference.astype(np.float32, copy=False)
    _record_debug_map(
        debug_maps,
        "stage4_boundary_raw_requested_cap_mm",
        raw_reference,
    )
    _record_debug_map(debug_maps, "stage4_color_ceiling_mm", color_ceiling)
    _record_debug_map(
        debug_maps,
        "stage4_boundary_raw_top_reference_mm",
        raw_top_reference,
    )

    smooth_kernel = float(cfg.smooth_kernel or 0.0)
    edge_guard_weight = np.zeros(shape, dtype=np.float32)
    smoothed_top_pre_restore = requested_top
    if smooth_kernel > 0.0:
        smoothing_guide = _build_stage4_boundary_smoothing_guide(
            visible_plan=visible_plan,
            filler_plan=filler_plan,
        )
        requested_top = _smooth_stage4_boundary_cap(
            raw_cap=requested_top,
            smoothing_guide=smoothing_guide,
            smooth_kernel=smooth_kernel,
        )
        smoothed_top_pre_restore = requested_top
        edge_guard_weight = _build_stage4_boundary_edge_guard(
            visible_plan=visible_plan,
            filler_plan=filler_plan,
            layer_height=layer_height,
            smooth_kernel=smooth_kernel,
        )
        requested_top = _apply_stage4_edge_aware_boundary_restore(
            smoothed_cap=requested_top,
            raw_cap_reference=raw_top_reference,
            edge_guard_weight=edge_guard_weight,
        )
    _record_debug_map(
        debug_maps,
        "stage4_boundary_smoothed_top_pre_restore_mm",
        smoothed_top_pre_restore,
    )
    _record_debug_map(
        debug_maps,
        "stage4_boundary_smoothed_top_post_restore_mm",
        requested_top,
    )
    _record_debug_map(
        debug_maps,
        "stage4_boundary_edge_guard_weight",
        edge_guard_weight,
    )

    unquantized_requested = np.maximum(
        requested_top - color_ceiling,
        np.float32(d_wc_min),
    ).astype(np.float32, copy=False)
    _record_debug_map(
        debug_maps,
        "stage4_boundary_unquantized_requested_cap_mm",
        unquantized_requested,
    )
    requested = unquantized_requested
    requested = _quantize_cap_map(
        requested,
        layer_height=layer_height,
        d_wc_min=d_wc_min,
        d_wc_max=boundary_d_wc_max,
    )
    if bool(cfg.cap_continuity_cleanup):
        requested = _continuity_cleanup_cap_map(
            requested,
            layer_height=layer_height,
            d_wc_min=d_wc_min,
            d_wc_max=boundary_d_wc_max,
        )
    smooth_candidate_requested = requested.astype(np.float32, copy=True)
    if (
        luminance_enabled
        and str(cfg.luminance_handler_mode or "").strip().lower()
        == "boundary_ceiling"
    ):
        requested = np.minimum(requested, raw_reference).astype(np.float32, copy=False)
        requested = _quantize_cap_map(
            requested,
            layer_height=layer_height,
            d_wc_min=d_wc_min,
            d_wc_max=boundary_d_wc_max,
        )
        _record_debug_map(
            debug_maps,
            "luminance_handler_stage4_boundary_after_hard_ceiling",
            requested,
        )
    if cap_mode == "appearance_bounded_smooth":
        if luminance_enabled:
            diagnostics.entries.append(
                PlanningDiagnosticEntry(
                    code="stage4_boundary_appearance_bound_skipped_luminance",
                    severity="info",
                    message=(
                        "Stage 4 appearance-bounded boundary smoothing is "
                        "standard-color-mode only; using the existing luminance "
                        "boundary cap path."
                    ),
                )
            )
        else:
            bounded, appearance_debug_maps, appearance_summary = (
                _apply_stage4_boundary_appearance_bound(
                    state=state,
                    visible_plan=visible_plan,
                    raw_cap=raw_reference,
                    smooth_candidate_cap=smooth_candidate_requested,
                    layer_height=layer_height,
                    d_wc_min=d_wc_min,
                    d_wc_max=boundary_d_wc_max,
                    de_budget=float(getattr(cfg, "boundary_cap_de_budget", 0.004)),
                )
            )
            for key, value in appearance_debug_maps.items():
                _record_debug_map(debug_maps, key, value)
            requested = bounded.astype(np.float32, copy=False)
            diagnostics.entries.append(
                PlanningDiagnosticEntry(
                    code="stage4_boundary_appearance_bound",
                    severity="info",
                    message=(
                        "Stage 4 appearance-bounded boundary smoothing used "
                        f"budget {appearance_summary['budget']:.4f} dE; "
                        f"accepted {appearance_summary['accepted_pixels']} pixels, "
                        f"rejected {appearance_summary['rejected_pixels']} pixels; "
                        "accepted extra dE mean/p90/p99 "
                        f"{appearance_summary['accepted_extra_de_mean']:.4f}/"
                        f"{appearance_summary['accepted_extra_de_p90']:.4f}/"
                        f"{appearance_summary['accepted_extra_de_p99']:.4f}; "
                        "rejected cap mm mean/p90/p99 "
                        f"{appearance_summary['rejected_cap_mm_mean']:.4f}/"
                        f"{appearance_summary['rejected_cap_mm_p90']:.4f}/"
                        f"{appearance_summary['rejected_cap_mm_p99']:.4f}; "
                        "provider fallbacks "
                        f"{appearance_summary['provider_fallback_count']}."
                    ),
                )
            )
    _record_debug_map(
        debug_maps,
        "stage4_boundary_quantized_requested_cap_mm",
        requested,
    )
    edge_guard_pixels = int(np.count_nonzero(edge_guard_weight > 0.25))
    if edge_guard_pixels:
        diagnostics.entries.append(
            PlanningDiagnosticEntry(
                code="stage4_boundary_edge_guard_pixels",
                severity="info",
                message=(
                    "Stage 4 edge-aware boundary guard restored raw cap influence at "
                    f"{edge_guard_pixels} pixels."
                ),
            )
        )
    return requested.astype(np.float32, copy=False), detail_reference, edge_guard_weight

__all__ = (
    '_STAGE4_SUPPORTED_CAP_MODES',
    '_banded_luminance_cap_limit_mm',
    '_stage4_banded_white_fill_mm',
    '_requested_stage4_cap_maps',
)
