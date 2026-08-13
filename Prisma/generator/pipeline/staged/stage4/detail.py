"""Stage 4 optical detail authoring and smoothing."""
from __future__ import annotations


import numpy as np
from scipy.ndimage import (
    generate_binary_structure,
    label as nd_label,
)

from model import (
    compose_stack,
    predict_transmission,
    to_oklab,
)
from ...luminance_handler import luminance_handler_enabled
from ...detail_cap_smoothing import (
    DetailCapSmoothingSettings,
    detail_height_to_layers,
    detail_layers_to_height,
    smooth_detail_cap_layers,
)

from ...staged_artifacts import (
    Stage4DetailZoneFacts,
    Stage4DetailZoneSummary,
    VisibleRecipeRawGeometryPlan,
)
from ...staged_printability import resolve_blueprint_printability_settings

from ..cap_prediction import (
    _stage4_provider_enabled,
    _increment_diagnostic_counter,
    _stage4_provider_cap_oklab_lookup,
    _stage4_precomputed_cap_oklab_lookup,
)
from ..cap_surface import _quantize_down


_STAGE4_DETAIL_MIN_OPTICAL_GAIN = 0.005

_STAGE4_DETAIL_ZONE_MIN_PIXELS = 2

_STAGE4_DETAIL_ZONE_MIN_POSITIVE_GAIN_FRACTION = 0.50

_STAGE4_DETAIL_ZONE_MIN_SIGNAL_SUPPORT_FRACTION = 0.25

_STAGE4_DETAIL_RECIPE_BOUNDARY_SIGNAL_FRACTION = 0.75

_STAGE4_DEFAULT_DETAIL_MAX_LAYERS = 2

_STAGE4_OPTICAL_DETAIL_LAYER_SEARCH_CAP = 64

_STAGE4_DETAIL_REJECT_NONE = 0

_STAGE4_DETAIL_REJECT_TOO_SMALL = 1

_STAGE4_DETAIL_REJECT_WEAK_OPTICAL_GAIN = 2

_STAGE4_DETAIL_REJECT_WEAK_SIGNAL = 3

def _shape_stage4_detail_stack_layers(
    *,
    detail_mask: np.ndarray,
    requested_detail_layers: np.ndarray,
    detail_signal: np.ndarray,
    signal_threshold: float | None,
    layer_height: float,
) -> np.ndarray:
    """Reserve extra detail-cap layers for full-strength local structure."""
    authored = np.where(
        np.asarray(detail_mask, dtype=bool),
        np.asarray(requested_detail_layers, dtype=np.float32),
        np.float32(0.0),
    ).astype(np.float32, copy=False)
    if signal_threshold is None or not np.any(authored > np.float32(layer_height + 1e-9)):
        return authored.astype(np.float32, copy=False)

    strong_stack_support = np.asarray(detail_signal, dtype=np.float32) >= np.float32(signal_threshold)
    layer_counts = np.rint(authored / np.float32(layer_height)).astype(np.int32)
    trim_to_one_layer = (layer_counts >= 2) & ~strong_stack_support
    if not np.any(trim_to_one_layer):
        return authored.astype(np.float32, copy=False)
    shaped = authored.copy()
    shaped[trim_to_one_layer] = np.float32(layer_height)
    return shaped.astype(np.float32, copy=False)

def _limit_stage4_independent_detail_layers(
    requested_detail_layers: np.ndarray,
    *,
    available_detail_mm: np.ndarray,
    layer_height: float,
    max_layers: int | None = None,
) -> np.ndarray:
    """Quantize and cap additive detail above the smoothed boundary tier."""
    limited = _quantize_down(requested_detail_layers, layer_height)
    layer_cap = max(0, int(max_layers if max_layers is not None else _STAGE4_DEFAULT_DETAIL_MAX_LAYERS))
    max_detail = np.float32(float(layer_cap) * float(layer_height))
    limited = np.minimum(limited, np.full_like(limited, max_detail, dtype=np.float32))
    limited = np.minimum(
        limited,
        np.maximum(np.asarray(available_detail_mm, dtype=np.float32), np.float32(0.0)),
    ).astype(np.float32, copy=False)
    return np.maximum(limited, np.float32(0.0)).astype(np.float32, copy=False)

def _compute_stage4_detail_optical_gain_map(
    *,
    state,
    visible_plan: VisibleRecipeRawGeometryPlan,
    boundary_cap_height: np.ndarray,
    final_cap_target: np.ndarray,
    detail_mask: np.ndarray,
) -> np.ndarray:
    """Return per-pixel OKLab gain from adding the proposed Stage 4 detail."""
    detail_mask = np.asarray(detail_mask, dtype=bool)
    gain_map = np.full(detail_mask.shape, np.nan, dtype=np.float32)
    if not np.any(detail_mask):
        return gain_map

    recipe_label_map = np.asarray(visible_plan.recipe_label_map, dtype=np.int32)
    target_oklab = np.asarray(
        visible_plan.mapped_target_oklab,
        dtype=np.float32,
    ).reshape(visible_plan.evaluation_shape + (3,))

    unique_recipe_labels = np.unique(recipe_label_map[detail_mask])
    for recipe_label in unique_recipe_labels.tolist():
        recipe_mask = detail_mask & (recipe_label_map == int(recipe_label))
        if not np.any(recipe_mask):
            continue

        recipe = visible_plan.recipe_table[int(recipe_label)].to_mapping()

        boundary_values = np.asarray(boundary_cap_height[recipe_mask], dtype=np.float32)
        final_values = np.asarray(final_cap_target[recipe_mask], dtype=np.float32)
        unique_caps = np.unique(np.concatenate([boundary_values, final_values], axis=0))
        if unique_caps.size == 0:
            continue

        if _stage4_provider_enabled(state):
            cap_oklab_lookup = _stage4_precomputed_cap_oklab_lookup(
                state=state,
                visible_plan=visible_plan,
                recipe_label=int(recipe_label),
                cap_values=unique_caps,
            )
            if cap_oklab_lookup is None:
                _increment_diagnostic_counter(state, "__stage4_provider_optical_gain_fallbacks__")
                cap_oklab_lookup = _stage4_provider_cap_oklab_lookup(
                    state=state,
                    recipe=recipe,
                    cap_values=unique_caps,
                )
        else:
            wb_profile = state.profiles.wb_profile
            wc_profile = state.profiles.wc_profile
            color_profiles = state.profiles.color_profiles
            d_wb = float(state.config.d_wb)
            layers = [(wb_profile, d_wb)]
            for fid, thickness in recipe.items():
                if float(thickness) > 1e-9:
                    layers.append((color_profiles[fid], float(thickness)))
            base_t = compose_stack(layers).astype(np.float32)
            cap_oklab_lookup: dict[float, np.ndarray] = {}
            for cap_value in unique_caps.tolist():
                t_cap = np.asarray(
                    predict_transmission(wc_profile, float(cap_value)),
                    dtype=np.float32,
                )
                cap_oklab_lookup[float(cap_value)] = to_oklab(
                    (base_t * t_cap).reshape(1, 3)
                )[0].astype(np.float32, copy=False)

        boundary_oklab = np.stack(
            [cap_oklab_lookup[float(value)] for value in boundary_values.tolist()],
            axis=0,
        ).astype(np.float32, copy=False)
        final_oklab = np.stack(
            [cap_oklab_lookup[float(value)] for value in final_values.tolist()],
            axis=0,
        ).astype(np.float32, copy=False)
        target_subset = target_oklab[recipe_mask].astype(np.float32, copy=False)
        boundary_de = np.sqrt(np.sum((boundary_oklab - target_subset) ** 2, axis=1))
        final_de = np.sqrt(np.sum((final_oklab - target_subset) ** 2, axis=1))
        gains = boundary_de - final_de

        flat_indices = np.flatnonzero(recipe_mask.reshape(-1))
        gain_map.reshape(-1)[flat_indices] = gains.astype(np.float32, copy=False)

    return gain_map

def _build_stage4_optical_detail_surface(
    *,
    state,
    visible_plan: VisibleRecipeRawGeometryPlan,
    boundary_cap_height: np.ndarray,
    remaining_cap_budget: np.ndarray,
    max_layers: int | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    """Choose additive detail layers by direct local optical gain."""
    layer_height = float(state.config.layer_height)
    boundary = np.asarray(boundary_cap_height, dtype=np.float32)
    available_mm = np.maximum(
        np.asarray(remaining_cap_budget, dtype=np.float32) - boundary,
        np.float32(0.0),
    )
    available_layers = np.floor(
        available_mm / np.float32(layer_height) + np.float32(1e-6)
    ).astype(np.int32)
    if max_layers is None:
        max_layer_count = int(np.max(available_layers)) if available_layers.size else 0
        max_layer_count = min(max_layer_count, int(_STAGE4_OPTICAL_DETAIL_LAYER_SEARCH_CAP))
    else:
        max_layer_count = max(0, int(max_layers))
    if max_layer_count <= 0 or not np.any(available_layers > 0):
        return (
            np.zeros_like(boundary, dtype=np.float32),
            np.full(boundary.shape, np.nan, dtype=np.float32),
        )

    best_layers = np.zeros_like(boundary, dtype=np.float32)
    best_gain = np.full(boundary.shape, -np.inf, dtype=np.float32)
    for layer_count in range(1, max_layer_count + 1):
        candidate_mask = available_layers >= int(layer_count)
        if not np.any(candidate_mask):
            continue
        detail_height = np.float32(float(layer_count) * layer_height)
        candidate_final_cap = boundary.copy()
        candidate_final_cap[candidate_mask] = (
            candidate_final_cap[candidate_mask] + detail_height
        ).astype(np.float32, copy=False)
        gain_map = _compute_stage4_detail_optical_gain_map(
            state=state,
            visible_plan=visible_plan,
            boundary_cap_height=boundary,
            final_cap_target=candidate_final_cap,
            detail_mask=candidate_mask,
        )
        finite_gain = np.isfinite(gain_map)
        better = finite_gain & (gain_map > best_gain)
        if np.any(better):
            best_gain[better] = gain_map[better].astype(np.float32, copy=False)
            best_layers[better] = detail_height

    requested = np.where(
        best_gain > np.float32(_STAGE4_DETAIL_MIN_OPTICAL_GAIN),
        best_layers,
        np.float32(0.0),
    ).astype(np.float32, copy=False)
    best_gain = np.where(np.isfinite(best_gain), best_gain, np.nan).astype(
        np.float32,
        copy=False,
    )
    return requested, best_gain

def _stage4_detail_zone_min_pixels(state) -> int:
    """Return the minimum connected pixel count for one detail-cap zone."""
    cfg = state.config
    pitch = max(float(cfg.solver_fine_pitch_mm or 0.20), 1e-9)
    printability_settings = resolve_blueprint_printability_settings(cfg, pitch_mm=pitch)
    min_width = max(float(printability_settings.extrusion_width_mm), pitch)
    min_width_px = max(1, int(np.ceil(min_width / pitch - 1e-9)))
    return max(int(_STAGE4_DETAIL_ZONE_MIN_PIXELS), min_width_px)

def _author_stage4_detail_zones(
    *,
    state,
    detail_mask: np.ndarray,
    requested_detail_layers: np.ndarray,
    optical_gain_map: np.ndarray,
    detail_signal: np.ndarray,
    signal_threshold: float | None,
    enabled: bool,
    recipe_boundary_support: np.ndarray | None = None,
) -> tuple[
    np.ndarray,
    np.ndarray,
    np.ndarray,
    np.ndarray,
    Stage4DetailZoneSummary,
    tuple[Stage4DetailZoneFacts, ...],
]:
    """Score connected Stage 4 detail candidates and accept whole authored zones."""
    candidate_mask = np.asarray(detail_mask, dtype=bool)
    label_map = np.full(candidate_mask.shape, -1, dtype=np.int32)
    candidate_label_map = np.full(candidate_mask.shape, -1, dtype=np.int32)
    rejection_reason_map = np.full(candidate_mask.shape, -1, dtype=np.int32)
    candidate_pixels = int(np.count_nonzero(candidate_mask))
    min_zone_pixels = _stage4_detail_zone_min_pixels(state)
    if not enabled or candidate_pixels == 0:
        summary = Stage4DetailZoneSummary(
            enabled=bool(enabled),
            min_zone_pixels=int(min_zone_pixels),
            candidate_pixels=int(candidate_pixels),
            candidate_zone_count=0,
            active_pixels=0,
            rejected_pixels=int(candidate_pixels),
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
        return (
            np.zeros_like(candidate_mask, dtype=bool),
            label_map,
            candidate_label_map,
            rejection_reason_map,
            summary,
            (),
        )

    component_labels, component_count = nd_label(
        candidate_mask,
        structure=generate_binary_structure(2, 2),
    )
    accepted_sizes: list[int] = []
    accepted_gains: list[float] = []
    accepted_structure_support: list[float] = []
    accepted_recipe_support: list[float] = []
    facts: list[Stage4DetailZoneFacts] = []
    next_zone_label = 0
    rejected_too_small = 0
    rejected_weak_gain = 0
    rejected_weak_signal = 0
    signal_cutoff = float(signal_threshold) if signal_threshold is not None else 0.0
    recipe_support_map = (
        np.asarray(recipe_boundary_support, dtype=bool)
        if recipe_boundary_support is not None
        else np.zeros_like(candidate_mask, dtype=bool)
    )
    boundary_signal_cutoff = (
        signal_cutoff * float(_STAGE4_DETAIL_RECIPE_BOUNDARY_SIGNAL_FRACTION)
        if signal_cutoff > 0.0
        else 0.0
    )
    min_gain = float(_STAGE4_DETAIL_MIN_OPTICAL_GAIN)
    for component_id in range(1, int(component_count) + 1):
        component_mask = component_labels == component_id
        candidate_label_map[component_mask] = np.int32(component_id - 1)
        pixel_count = int(np.count_nonzero(component_mask))
        ys, xs = np.nonzero(component_mask)
        detail_values = np.asarray(requested_detail_layers[component_mask], dtype=np.float32)
        gain_values = np.asarray(optical_gain_map[component_mask], dtype=np.float32)
        finite_gain_values = gain_values[np.isfinite(gain_values)]
        signal_values = np.asarray(detail_signal[component_mask], dtype=np.float32)
        mean_gain = float(np.mean(finite_gain_values)) if finite_gain_values.size else 0.0
        min_zone_gain = float(np.min(finite_gain_values)) if finite_gain_values.size else 0.0
        positive_gain_fraction = (
            float(np.count_nonzero(finite_gain_values > min_gain)) / float(finite_gain_values.size)
            if finite_gain_values.size
            else 0.0
        )
        mean_signal = float(np.mean(signal_values)) if signal_values.size else 0.0
        signal_support_fraction = (
            float(np.count_nonzero(signal_values >= signal_cutoff)) / float(signal_values.size)
            if signal_values.size and signal_cutoff > 0.0
            else 1.0
        )
        recipe_support_values = recipe_support_map[component_mask]
        recipe_boundary_support_fraction = (
            float(np.count_nonzero(recipe_support_values)) / float(recipe_support_values.size)
            if recipe_support_values.size
            else 0.0
        )
        structure_values = (
            (signal_values >= signal_cutoff)
            | (recipe_support_values & (signal_values >= np.float32(boundary_signal_cutoff)))
            if signal_cutoff > 0.0
            else np.ones(signal_values.shape, dtype=bool)
        )
        structure_support_fraction = (
            float(np.count_nonzero(structure_values)) / float(structure_values.size)
            if structure_values.size
            else 0.0
        )

        accepted = True
        rejection_reason = ""
        rejection_code = _STAGE4_DETAIL_REJECT_NONE
        if pixel_count < min_zone_pixels:
            accepted = False
            rejection_reason = "too_small"
            rejection_code = _STAGE4_DETAIL_REJECT_TOO_SMALL
            rejected_too_small += 1
        elif structure_support_fraction < float(_STAGE4_DETAIL_ZONE_MIN_SIGNAL_SUPPORT_FRACTION):
            accepted = False
            rejection_reason = "weak_detail_signal"
            rejection_code = _STAGE4_DETAIL_REJECT_WEAK_SIGNAL
            rejected_weak_signal += 1
        elif (
            mean_gain <= min_gain
            or positive_gain_fraction < float(_STAGE4_DETAIL_ZONE_MIN_POSITIVE_GAIN_FRACTION)
        ):
            accepted = False
            rejection_reason = "weak_optical_gain"
            rejection_code = _STAGE4_DETAIL_REJECT_WEAK_OPTICAL_GAIN
            rejected_weak_gain += 1

        rejection_reason_map[component_mask] = np.int32(rejection_code)
        zone_label = -1
        if accepted:
            zone_label = int(next_zone_label)
            label_map[component_mask] = np.int32(zone_label)
            accepted_sizes.append(pixel_count)
            accepted_gains.append(mean_gain)
            accepted_structure_support.append(structure_support_fraction)
            accepted_recipe_support.append(recipe_boundary_support_fraction)
            next_zone_label += 1

        facts.append(
            Stage4DetailZoneFacts(
                component_id=int(component_id),
                zone_label=int(zone_label),
                accepted=bool(accepted),
                rejection_reason=rejection_reason,
                pixel_count=int(pixel_count),
                y_min=int(np.min(ys)) if ys.size else -1,
                x_min=int(np.min(xs)) if xs.size else -1,
                y_max=int(np.max(ys)) if ys.size else -1,
                x_max=int(np.max(xs)) if xs.size else -1,
                mean_detail_height_mm=float(np.mean(detail_values)) if detail_values.size else 0.0,
                max_detail_height_mm=float(np.max(detail_values)) if detail_values.size else 0.0,
                mean_optical_gain=mean_gain,
                min_optical_gain=min_zone_gain,
                positive_gain_fraction=positive_gain_fraction,
                mean_detail_signal=mean_signal,
                signal_support_fraction=signal_support_fraction,
                structure_support_fraction=structure_support_fraction,
                recipe_boundary_support_fraction=recipe_boundary_support_fraction,
            )
        )

    active_mask = label_map >= 0
    active_pixels = int(np.count_nonzero(active_mask))
    rejected_pixels = int(candidate_pixels - active_pixels)
    rejected_zone_count = (
        int(rejected_too_small)
        + int(rejected_weak_gain)
        + int(rejected_weak_signal)
    )
    summary = Stage4DetailZoneSummary(
        enabled=True,
        min_zone_pixels=int(min_zone_pixels),
        candidate_pixels=int(candidate_pixels),
        candidate_zone_count=int(component_count),
        active_pixels=int(active_pixels),
        rejected_pixels=int(rejected_pixels),
        zone_count=int(len(accepted_sizes)),
        rejected_zone_count=int(rejected_zone_count),
        rejected_too_small_zone_count=int(rejected_too_small),
        rejected_weak_optical_gain_zone_count=int(rejected_weak_gain),
        rejected_weak_signal_zone_count=int(rejected_weak_signal),
        largest_zone_pixels=int(max(accepted_sizes, default=0)),
        mean_zone_pixels=float(np.mean(accepted_sizes)) if accepted_sizes else 0.0,
        mean_zone_optical_gain=float(np.mean(accepted_gains)) if accepted_gains else 0.0,
        mean_zone_structure_support=(
            float(np.mean(accepted_structure_support)) if accepted_structure_support else 0.0
        ),
        mean_zone_recipe_boundary_support=(
            float(np.mean(accepted_recipe_support)) if accepted_recipe_support else 0.0
        ),
    )
    return active_mask, label_map, candidate_label_map, rejection_reason_map, summary, tuple(facts)

def _author_stage4_direct_residual_detail_zones(
    *,
    state,
    detail_mask: np.ndarray,
    requested_detail_layers: np.ndarray,
    optical_gain_map: np.ndarray,
    detail_signal: np.ndarray,
) -> tuple[
    np.ndarray,
    np.ndarray,
    np.ndarray,
    np.ndarray,
    Stage4DetailZoneSummary,
    tuple[Stage4DetailZoneFacts, ...],
]:
    """Accept direct residual detail zones; printability owns suppression."""
    candidate_mask = np.asarray(detail_mask, dtype=bool)
    label_map = np.full(candidate_mask.shape, -1, dtype=np.int32)
    candidate_label_map = np.full(candidate_mask.shape, -1, dtype=np.int32)
    rejection_reason_map = np.full(candidate_mask.shape, -1, dtype=np.int32)
    candidate_pixels = int(np.count_nonzero(candidate_mask))
    min_zone_pixels = _stage4_detail_zone_min_pixels(state)
    if candidate_pixels == 0:
        summary = Stage4DetailZoneSummary(
            enabled=True,
            min_zone_pixels=int(min_zone_pixels),
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
        return (
            np.zeros_like(candidate_mask, dtype=bool),
            label_map,
            candidate_label_map,
            rejection_reason_map,
            summary,
            (),
        )

    component_labels, component_count = nd_label(
        candidate_mask,
        structure=generate_binary_structure(2, 2),
    )
    requested = np.asarray(requested_detail_layers, dtype=np.float32)
    gains = np.asarray(optical_gain_map, dtype=np.float32)
    signals = np.asarray(detail_signal, dtype=np.float32)
    accepted_sizes: list[int] = []
    accepted_gains: list[float] = []
    facts: list[Stage4DetailZoneFacts] = []
    for component_id in range(1, int(component_count) + 1):
        component_mask = component_labels == component_id
        candidate_label = int(component_id - 1)
        candidate_label_map[component_mask] = np.int32(candidate_label)
        label_map[component_mask] = np.int32(candidate_label)
        rejection_reason_map[component_mask] = np.int32(_STAGE4_DETAIL_REJECT_NONE)
        pixel_count = int(np.count_nonzero(component_mask))
        ys, xs = np.nonzero(component_mask)
        detail_values = requested[component_mask]
        gain_values = gains[component_mask]
        finite_gain_values = gain_values[np.isfinite(gain_values)]
        signal_values = signals[component_mask]
        mean_gain = float(np.mean(finite_gain_values)) if finite_gain_values.size else 0.0
        min_zone_gain = float(np.min(finite_gain_values)) if finite_gain_values.size else 0.0
        positive_gain_fraction = (
            float(np.count_nonzero(finite_gain_values > float(_STAGE4_DETAIL_MIN_OPTICAL_GAIN)))
            / float(finite_gain_values.size)
            if finite_gain_values.size
            else 0.0
        )
        accepted_sizes.append(pixel_count)
        accepted_gains.append(mean_gain)
        facts.append(
            Stage4DetailZoneFacts(
                component_id=int(component_id),
                zone_label=candidate_label,
                accepted=True,
                rejection_reason="",
                pixel_count=pixel_count,
                y_min=int(np.min(ys)) if ys.size else -1,
                x_min=int(np.min(xs)) if xs.size else -1,
                y_max=int(np.max(ys)) if ys.size else -1,
                x_max=int(np.max(xs)) if xs.size else -1,
                mean_detail_height_mm=float(np.mean(detail_values)) if detail_values.size else 0.0,
                max_detail_height_mm=float(np.max(detail_values)) if detail_values.size else 0.0,
                mean_optical_gain=mean_gain,
                min_optical_gain=min_zone_gain,
                positive_gain_fraction=positive_gain_fraction,
                mean_detail_signal=float(np.mean(signal_values)) if signal_values.size else 0.0,
                signal_support_fraction=1.0,
                structure_support_fraction=1.0,
                recipe_boundary_support_fraction=0.0,
            )
        )

    summary = Stage4DetailZoneSummary(
        enabled=True,
        min_zone_pixels=int(min_zone_pixels),
        candidate_pixels=int(candidate_pixels),
        candidate_zone_count=int(component_count),
        active_pixels=int(candidate_pixels),
        rejected_pixels=0,
        zone_count=int(component_count),
        rejected_zone_count=0,
        rejected_too_small_zone_count=0,
        rejected_weak_optical_gain_zone_count=0,
        rejected_weak_signal_zone_count=0,
        largest_zone_pixels=int(max(accepted_sizes, default=0)),
        mean_zone_pixels=float(np.mean(accepted_sizes)) if accepted_sizes else 0.0,
        mean_zone_optical_gain=float(np.mean(accepted_gains)) if accepted_gains else 0.0,
        mean_zone_structure_support=1.0 if accepted_sizes else 0.0,
        mean_zone_recipe_boundary_support=0.0,
    )
    active_mask = label_map >= 0
    return active_mask, label_map, candidate_label_map, rejection_reason_map, summary, tuple(facts)

def _stage4_detail_cap_smoothing_settings(cfg) -> DetailCapSmoothingSettings:
    max_layer = getattr(cfg, "detail_cap_max_layers", None)
    if max_layer is None:
        max_layer = DetailCapSmoothingSettings().max_layer
    return DetailCapSmoothingSettings(
        max_layer=max(0, int(max_layer)),
        exact_speckle_max_px=max(
            0,
            int(getattr(cfg, "detail_cap_smoothing_exact_speckle_max_px", 1) or 0),
        ),
        cumulative_component_max_px=max(
            0,
            int(
                getattr(
                    cfg,
                    "detail_cap_smoothing_cumulative_component_max_px",
                    2,
                )
                or 0
            ),
        ),
        cumulative_hole_max_px=max(
            0,
            int(getattr(cfg, "detail_cap_smoothing_cumulative_hole_max_px", 2) or 0),
        ),
    )

def _apply_stage4_detail_cap_smoothing(
    *,
    detail_height_mm: np.ndarray,
    cfg,
    layer_height: float,
    boundary_cap_height_mm: np.ndarray,
    remaining_cap_budget_mm: np.ndarray,
    desired_final_cap_target_mm: np.ndarray | None,
) -> tuple[np.ndarray, dict[str, object] | None]:
    """Smooth luminance detail before final printability gates run."""

    if not luminance_handler_enabled(cfg):
        return detail_height_mm, None
    if not bool(getattr(cfg, "detail_cap_enabled", True)):
        return detail_height_mm, None
    if not bool(getattr(cfg, "detail_cap_smoothing_enabled", True)):
        return detail_height_mm, None

    detail = np.asarray(detail_height_mm, dtype=np.float32)
    if not np.any(detail > np.float32(1e-9)):
        return detail.astype(np.float32, copy=False), None

    settings = _stage4_detail_cap_smoothing_settings(cfg)
    layers = detail_height_to_layers(detail, layer_height)
    smoothing = smooth_detail_cap_layers(layers, settings)
    smoothed_detail = detail_layers_to_height(
        smoothing.smoothed_layers,
        layer_height,
    ).astype(np.float32, copy=False)

    available_detail = np.maximum(
        np.asarray(remaining_cap_budget_mm, dtype=np.float32)
        - np.asarray(boundary_cap_height_mm, dtype=np.float32),
        np.float32(0.0),
    ).astype(np.float32, copy=False)
    if desired_final_cap_target_mm is not None:
        available_detail = np.minimum(
            available_detail,
            np.maximum(
                np.asarray(desired_final_cap_target_mm, dtype=np.float32)
                - np.asarray(boundary_cap_height_mm, dtype=np.float32),
                np.float32(0.0),
            ),
        ).astype(np.float32, copy=False)

    clamped_detail = np.minimum(smoothed_detail, available_detail).astype(
        np.float32,
        copy=False,
    )
    budget_clamped_px = int(
        np.count_nonzero(smoothed_detail > clamped_detail + np.float32(1e-9))
    )

    summary = smoothing.summary_dict()
    summary.update(
        {
            "applied": True,
            "layer_height_mm": float(layer_height),
            "changed_px": int(smoothing.after.changed_px),
            "raised_px": int(smoothing.after.raised_px),
            "lowered_px": int(smoothing.after.lowered_px),
            "mean_abs_layer_delta": float(smoothing.after.mean_abs_layer_delta),
            "p95_abs_layer_delta": float(smoothing.after.p95_abs_layer_delta),
            "max_abs_layer_delta": int(smoothing.after.max_abs_layer_delta),
            "post_budget_clamped_px": budget_clamped_px,
            "printability_regated": True,
        }
    )
    return clamped_detail.astype(np.float32, copy=False), summary

__all__ = (
    '_STAGE4_DETAIL_MIN_OPTICAL_GAIN',
    '_STAGE4_DETAIL_ZONE_MIN_PIXELS',
    '_STAGE4_DETAIL_ZONE_MIN_POSITIVE_GAIN_FRACTION',
    '_STAGE4_DETAIL_ZONE_MIN_SIGNAL_SUPPORT_FRACTION',
    '_STAGE4_DETAIL_RECIPE_BOUNDARY_SIGNAL_FRACTION',
    '_STAGE4_DEFAULT_DETAIL_MAX_LAYERS',
    '_STAGE4_OPTICAL_DETAIL_LAYER_SEARCH_CAP',
    '_STAGE4_DETAIL_REJECT_NONE',
    '_STAGE4_DETAIL_REJECT_TOO_SMALL',
    '_STAGE4_DETAIL_REJECT_WEAK_OPTICAL_GAIN',
    '_STAGE4_DETAIL_REJECT_WEAK_SIGNAL',
    '_shape_stage4_detail_stack_layers',
    '_limit_stage4_independent_detail_layers',
    '_compute_stage4_detail_optical_gain_map',
    '_build_stage4_optical_detail_surface',
    '_stage4_detail_zone_min_pixels',
    '_author_stage4_detail_zones',
    '_author_stage4_direct_residual_detail_zones',
    '_stage4_detail_cap_smoothing_settings',
    '_apply_stage4_detail_cap_smoothing',
)
