"""Stage 4 boundary-cap shaping and appearance constraints."""
from __future__ import annotations


import numpy as np
from scipy.ndimage import (
    gaussian_filter,
    maximum_filter,
    uniform_filter,
)


from ...staged_artifacts import (
    FillerGeometryPlan,
    VisibleRecipeRawGeometryPlan,
)

from ..cap_prediction import (
    _stage4_recipe_cap_oklab_lookup,
    _stage4_lookup_oklab_by_count,
)
from ..cap_surface import (
    _quantize_cap_map,
    _compute_stage4_detail_signal,
    _stage4_gradient_magnitude,
    _stage4_recipe_boundary_mask,
)


_STAGE4_BOUNDARY_EDGE_SOURCE_PERCENTILE = 80.0

_STAGE4_BOUNDARY_EDGE_SOURCE_MIN_SIGNAL = 0.020

_STAGE4_BOUNDARY_EDGE_CEILING_PERCENTILE = 75.0

_STAGE4_BOUNDARY_EDGE_MAX_RADIUS_PX = 8

_STAGE4_BOUNDARY_EDGE_MAX_RESTORE_WEIGHT = 0.65

_STAGE4_BOUNDARY_EDGE_FULL_RESTORE_KERNEL = 15.0

_STAGE4_BOUNDARY_GUIDED_FILTER_EPS = 0.0025

def _build_stage4_boundary_smoothing_guide(
    *,
    visible_plan: VisibleRecipeRawGeometryPlan,
    filler_plan: FillerGeometryPlan,
) -> np.ndarray:
    """Return the structure guide used to smooth boundary cap without broad bleed."""
    shape = visible_plan.evaluation_shape
    return np.zeros(shape, dtype=np.float32)

def _stage4_boundary_edge_restore_weight(smooth_kernel: float) -> float:
    """Return how strongly Stage 4 should restore raw cap near edge guards."""
    if float(smooth_kernel) <= 0.0:
        return 0.0
    smooth_fraction = float(smooth_kernel) / max(
        float(_STAGE4_BOUNDARY_EDGE_FULL_RESTORE_KERNEL),
        1e-9,
    )
    restore_fraction = 1.0 - float(np.clip(smooth_fraction, 0.0, 1.0))
    if restore_fraction <= 0.0:
        return 0.0
    return float(_STAGE4_BOUNDARY_EDGE_MAX_RESTORE_WEIGHT) * restore_fraction

def _stage4_box_filter(values: np.ndarray, radius: int) -> np.ndarray:
    """Apply an O(1) box filter for Stage 4 guided boundary smoothing."""
    size = max(1, 2 * int(radius) + 1)
    return uniform_filter(
        np.asarray(values, dtype=np.float64),
        size=size,
        mode="nearest",
    ).astype(np.float32)

def _stage4_guided_filter(
    *,
    guide: np.ndarray,
    values: np.ndarray,
    radius: int,
    eps: float,
) -> np.ndarray:
    """Edge-preserving guided smoothing for the Stage 4 boundary cap."""
    guide_arr = np.asarray(guide, dtype=np.float32)
    src = np.asarray(values, dtype=np.float32)
    radius_i = max(1, int(radius))
    mean_i = _stage4_box_filter(guide_arr, radius_i)
    mean_p = _stage4_box_filter(src, radius_i)
    mean_ip = _stage4_box_filter(guide_arr * src, radius_i)
    mean_ii = _stage4_box_filter(guide_arr * guide_arr, radius_i)

    cov_ip = mean_ip - mean_i * mean_p
    var_i = mean_ii - mean_i * mean_i
    a = cov_ip / (var_i + np.float32(eps))
    b = mean_p - a * mean_i
    mean_a = _stage4_box_filter(a, radius_i)
    mean_b = _stage4_box_filter(b, radius_i)
    return (mean_a * guide_arr + mean_b).astype(np.float32, copy=False)

def _smooth_stage4_boundary_cap(
    *,
    raw_cap: np.ndarray,
    smoothing_guide: np.ndarray,
    smooth_kernel: float,
) -> np.ndarray:
    """Smooth the boundary cap while preserving real guide-space contours."""
    if float(smooth_kernel) <= 0.0:
        return np.asarray(raw_cap, dtype=np.float32).copy()

    radius = max(1, int(np.ceil(float(smooth_kernel))))
    return _stage4_guided_filter(
        guide=smoothing_guide,
        values=np.asarray(raw_cap, dtype=np.float32),
        radius=radius,
        eps=_STAGE4_BOUNDARY_GUIDED_FILTER_EPS,
    )

def _build_stage4_boundary_edge_guard(
    *,
    visible_plan: VisibleRecipeRawGeometryPlan,
    filler_plan: FillerGeometryPlan,
    layer_height: float,
    smooth_kernel: float,
) -> np.ndarray:
    """Return a weight map where boundary smoothing should respect strong edges."""
    shape = visible_plan.evaluation_shape
    if float(smooth_kernel) <= 0.0:
        return np.zeros(shape, dtype=np.float32)

    detail_signal = _compute_stage4_detail_signal(visible_plan)
    positive_signal = detail_signal[detail_signal > 1e-9]
    if positive_signal.size:
        source_threshold = max(
            float(np.percentile(positive_signal, _STAGE4_BOUNDARY_EDGE_SOURCE_PERCENTILE)),
            float(_STAGE4_BOUNDARY_EDGE_SOURCE_MIN_SIGNAL),
        )
        source_edges = detail_signal >= source_threshold
    else:
        source_edges = np.zeros(shape, dtype=bool)

    ceiling_gradient = _stage4_gradient_magnitude(filler_plan.color_ceiling_mm)
    positive_ceiling = ceiling_gradient[ceiling_gradient > 1e-9]
    if positive_ceiling.size:
        ceiling_threshold = max(
            float(np.percentile(positive_ceiling, _STAGE4_BOUNDARY_EDGE_CEILING_PERCENTILE)),
            0.5 * float(layer_height),
        )
        ceiling_edges = ceiling_gradient >= ceiling_threshold
    else:
        ceiling_edges = np.zeros(shape, dtype=bool)

    recipe_edges = _stage4_recipe_boundary_mask(visible_plan.recipe_label_map)
    ceiling_support = maximum_filter(
        ceiling_edges.astype(np.uint8),
        size=3,
        mode="nearest",
    ) > 0
    edge_core = ceiling_edges | (source_edges & ceiling_support) | (recipe_edges & ceiling_support)
    edge_density = gaussian_filter(
        edge_core.astype(np.float32),
        sigma=1.0,
        mode="nearest",
    )
    edge_core &= edge_density < np.float32(0.65)
    if not np.any(edge_core):
        return np.zeros(shape, dtype=np.float32)

    radius = int(np.ceil(max(1.0, min(float(_STAGE4_BOUNDARY_EDGE_MAX_RADIUS_PX), float(smooth_kernel) / 6.0))))
    dilated = maximum_filter(
        edge_core.astype(np.uint8),
        size=2 * radius + 1,
        mode="nearest",
    ) > 0
    softened = gaussian_filter(
        dilated.astype(np.float32),
        sigma=max(0.5, float(radius) / 2.0),
        mode="nearest",
    ).astype(np.float32)
    max_weight = float(np.max(softened)) if softened.size else 0.0
    if max_weight > 1e-9:
        softened = softened / np.float32(max_weight)
    weight = np.maximum(softened, edge_core.astype(np.float32))
    weight = np.clip(weight, 0.0, 1.0) * np.float32(
        _stage4_boundary_edge_restore_weight(float(smooth_kernel))
    )
    return weight.astype(np.float32, copy=False)

def _apply_stage4_edge_aware_boundary_restore(
    *,
    smoothed_cap: np.ndarray,
    raw_cap_reference: np.ndarray,
    edge_guard_weight: np.ndarray,
) -> np.ndarray:
    """Blend smoothed boundary cap back toward raw cap near strong edges."""
    smoothed = np.asarray(smoothed_cap, dtype=np.float32)
    raw = np.asarray(raw_cap_reference, dtype=np.float32)
    weight = np.asarray(edge_guard_weight, dtype=np.float32)
    if not np.any(weight > 1e-9):
        return smoothed.astype(np.float32, copy=True)
    restored = smoothed + weight * (raw - smoothed)
    return restored.astype(np.float32, copy=False)

def _stage4_summary_percentiles(values: np.ndarray) -> tuple[float, float, float]:
    """Return mean, p90, and p99 for finite values, or zeros for empty input."""
    arr = np.asarray(values, dtype=np.float32)
    arr = arr[np.isfinite(arr)]
    if arr.size == 0:
        return 0.0, 0.0, 0.0
    return (
        float(np.mean(arr)),
        float(np.percentile(arr, 90.0)),
        float(np.percentile(arr, 99.0)),
    )

def _apply_stage4_boundary_appearance_bound(
    *,
    state,
    visible_plan: VisibleRecipeRawGeometryPlan,
    raw_cap: np.ndarray,
    smooth_candidate_cap: np.ndarray,
    layer_height: float,
    d_wc_min: float,
    d_wc_max: float,
    de_budget: float,
) -> tuple[np.ndarray, dict[str, np.ndarray], dict[str, float | int]]:
    """Reject boundary smoothing where it damages predicted final appearance."""
    shape = visible_plan.evaluation_shape
    layer = max(float(layer_height), 1e-9)
    floor_count = int(np.ceil(float(d_wc_min) / layer - 1e-9))
    max_count = max(floor_count, int(np.ceil(float(d_wc_max) / layer - 1e-9)))

    raw = _quantize_cap_map(
        np.asarray(raw_cap, dtype=np.float32).reshape(shape),
        layer_height=layer,
        d_wc_min=d_wc_min,
        d_wc_max=d_wc_max,
    )
    smooth = _quantize_cap_map(
        np.asarray(smooth_candidate_cap, dtype=np.float32).reshape(shape),
        layer_height=layer,
        d_wc_min=d_wc_min,
        d_wc_max=d_wc_max,
    )
    raw_counts = np.clip(
        np.rint(raw / np.float32(layer)).astype(np.int32),
        floor_count,
        max_count,
    )
    smooth_counts = np.clip(
        np.rint(smooth / np.float32(layer)).astype(np.int32),
        floor_count,
        max_count,
    )

    accepted_counts = raw_counts.copy()
    raw_de_map = np.full(shape, np.nan, dtype=np.float32)
    candidate_de_map = np.full(shape, np.nan, dtype=np.float32)
    accepted_de_map = np.full(shape, np.nan, dtype=np.float32)
    provider_fallback_count = 0

    recipe_label_map = np.asarray(visible_plan.recipe_label_map, dtype=np.int32).reshape(shape)
    target_oklab = np.asarray(
        visible_plan.mapped_target_oklab,
        dtype=np.float32,
    ).reshape(shape + (3,))
    de_budget_f = max(0.0, float(de_budget))

    for recipe_label in np.unique(recipe_label_map).tolist():
        label = int(recipe_label)
        if label < 0 or label >= len(visible_plan.recipe_table):
            continue
        recipe_mask = recipe_label_map == label
        if not np.any(recipe_mask):
            continue

        raw_label_counts = raw_counts[recipe_mask]
        smooth_label_counts = smooth_counts[recipe_mask]
        target_label = target_oklab[recipe_mask]
        min_count = int(np.min(np.minimum(raw_label_counts, smooth_label_counts)))
        max_label_count = int(np.max(np.maximum(raw_label_counts, smooth_label_counts)))
        candidate_counts = np.arange(min_count, max_label_count + 1, dtype=np.int32)
        candidate_values = (candidate_counts.astype(np.float32) * np.float32(layer)).astype(
            np.float32,
            copy=False,
        )
        cap_oklab_lookup, provider_fallback = _stage4_recipe_cap_oklab_lookup(
            state=state,
            visible_plan=visible_plan,
            recipe_label=label,
            cap_values=candidate_values,
        )
        if provider_fallback:
            provider_fallback_count += 1
        lookup_by_count: dict[int, np.ndarray] = {}
        for count, value in zip(candidate_counts.tolist(), candidate_values.tolist(), strict=False):
            row = cap_oklab_lookup.get(float(value))
            if row is not None:
                lookup_by_count[int(count)] = row.astype(np.float32, copy=False)
        if not lookup_by_count:
            continue

        raw_de = _stage4_lookup_oklab_by_count(
            lookup_by_count,
            raw_label_counts,
            target_label,
        )
        candidate_de = _stage4_lookup_oklab_by_count(
            lookup_by_count,
            smooth_label_counts,
            target_label,
        )
        accepted_label_counts = raw_label_counts.copy()
        accepted_de = raw_de.copy()
        unresolved = np.ones(raw_label_counts.shape, dtype=bool)
        direction = np.where(smooth_label_counts >= raw_label_counts, -1, 1).astype(np.int32)
        max_delta = int(np.max(np.abs(smooth_label_counts - raw_label_counts), initial=0))

        for step in range(max_delta + 1):
            if not np.any(unresolved):
                break
            probe_counts = smooth_label_counts + direction * int(step)
            in_range = (
                (probe_counts >= np.minimum(raw_label_counts, smooth_label_counts))
                & (probe_counts <= np.maximum(raw_label_counts, smooth_label_counts))
                & unresolved
            )
            if not np.any(in_range):
                continue
            probe_de = _stage4_lookup_oklab_by_count(
                lookup_by_count,
                probe_counts[in_range],
                target_label[in_range],
            )
            accepted_here = probe_de <= (raw_de[in_range] + np.float32(de_budget_f))
            if not np.any(accepted_here):
                continue
            in_range_indices = np.flatnonzero(in_range)
            take_indices = in_range_indices[accepted_here]
            accepted_label_counts[take_indices] = probe_counts[in_range][accepted_here]
            accepted_de[take_indices] = probe_de[accepted_here]
            unresolved[take_indices] = False

        raw_de_map[recipe_mask] = raw_de.astype(np.float32, copy=False)
        candidate_de_map[recipe_mask] = candidate_de.astype(np.float32, copy=False)
        accepted_de_map[recipe_mask] = accepted_de.astype(np.float32, copy=False)
        accepted_counts[recipe_mask] = accepted_label_counts.astype(np.int32, copy=False)

    accepted = (
        accepted_counts.astype(np.float32) * np.float32(layer)
    ).astype(np.float32, copy=False)
    accepted = _quantize_cap_map(
        accepted,
        layer_height=layer,
        d_wc_min=d_wc_min,
        d_wc_max=d_wc_max,
    )

    rejected_mm = np.abs(smooth - accepted).astype(np.float32, copy=False)
    accepted_full_candidate = np.isclose(accepted, smooth, atol=max(layer * 0.25, 1e-6))
    raw_de_debug = np.nan_to_num(
        raw_de_map,
        nan=0.0,
        posinf=0.0,
        neginf=0.0,
    ).astype(np.float32, copy=False)
    candidate_de_debug = np.nan_to_num(
        candidate_de_map,
        nan=0.0,
        posinf=0.0,
        neginf=0.0,
    ).astype(np.float32, copy=False)
    accepted_de_debug = np.nan_to_num(
        accepted_de_map,
        nan=0.0,
        posinf=0.0,
        neginf=0.0,
    ).astype(np.float32, copy=False)
    extra_de = np.maximum(
        accepted_de_debug - raw_de_debug,
        np.float32(0.0),
    ).astype(np.float32, copy=False)
    accepted_extra_values = extra_de[np.isfinite(extra_de) & accepted_full_candidate]
    rejected_values = rejected_mm[rejected_mm > np.float32(1e-9)]
    extra_mean, extra_p90, extra_p99 = _stage4_summary_percentiles(accepted_extra_values)
    rejected_mean, rejected_p90, rejected_p99 = _stage4_summary_percentiles(rejected_values)

    debug_maps = {
        "stage4_boundary_smooth_candidate_cap_mm": smooth,
        "stage4_boundary_appearance_raw_de": raw_de_debug,
        "stage4_boundary_appearance_candidate_de": candidate_de_debug,
        "stage4_boundary_appearance_accepted_de": accepted_de_debug,
        "stage4_boundary_appearance_extra_de": extra_de,
        "stage4_boundary_appearance_bounded_cap_mm": accepted,
        "stage4_boundary_appearance_rejected_mm": rejected_mm,
        "stage4_boundary_appearance_accept_mask": accepted_full_candidate.astype(
            np.float32,
            copy=False,
        ),
        "stage4_boundary_candidate_minus_raw_mm": (smooth - raw).astype(
            np.float32,
            copy=False,
        ),
        "stage4_boundary_accepted_minus_raw_mm": (accepted - raw).astype(
            np.float32,
            copy=False,
        ),
    }
    summary = {
        "budget": float(de_budget_f),
        "accepted_pixels": int(np.count_nonzero(accepted_full_candidate)),
        "rejected_pixels": int(np.count_nonzero(~accepted_full_candidate)),
        "accepted_extra_de_mean": extra_mean,
        "accepted_extra_de_p90": extra_p90,
        "accepted_extra_de_p99": extra_p99,
        "rejected_cap_mm_mean": rejected_mean,
        "rejected_cap_mm_p90": rejected_p90,
        "rejected_cap_mm_p99": rejected_p99,
        "provider_fallback_count": int(provider_fallback_count),
    }
    return accepted.astype(np.float32, copy=False), debug_maps, summary

__all__ = (
    '_STAGE4_BOUNDARY_EDGE_SOURCE_PERCENTILE',
    '_STAGE4_BOUNDARY_EDGE_SOURCE_MIN_SIGNAL',
    '_STAGE4_BOUNDARY_EDGE_CEILING_PERCENTILE',
    '_STAGE4_BOUNDARY_EDGE_MAX_RADIUS_PX',
    '_STAGE4_BOUNDARY_EDGE_MAX_RESTORE_WEIGHT',
    '_STAGE4_BOUNDARY_EDGE_FULL_RESTORE_KERNEL',
    '_STAGE4_BOUNDARY_GUIDED_FILTER_EPS',
    '_build_stage4_boundary_smoothing_guide',
    '_stage4_boundary_edge_restore_weight',
    '_stage4_box_filter',
    '_stage4_guided_filter',
    '_smooth_stage4_boundary_cap',
    '_build_stage4_boundary_edge_guard',
    '_apply_stage4_edge_aware_boundary_restore',
    '_stage4_summary_percentiles',
    '_apply_stage4_boundary_appearance_bound',
)
