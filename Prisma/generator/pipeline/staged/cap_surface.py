"""Shared white-cap surface analysis kernels."""
from __future__ import annotations


import numpy as np
from scipy.ndimage import (
    binary_closing,
    gaussian_filter,
    generate_binary_structure,
    maximum_filter,
)


from ..staged_artifacts import VisibleRecipeRawGeometryPlan

_STAGE4_DETAIL_SIGNAL_SIGMA = 1.0
_STAGE4_DETAIL_SIGNAL_PERCENTILE = 85.0


def _quantize_down(field: np.ndarray, layer_height: float) -> np.ndarray:
    """Quantize a positive field downward onto the layer lattice."""
    if field.size == 0:
        return field.astype(np.float32, copy=True)
    steps = np.floor((field.astype(np.float64) / float(layer_height)) + 1e-6)
    return (steps * float(layer_height)).astype(np.float32)

def _quantized_cap_floor(d_wc_min: float, layer_height: float) -> float:
    """Return the smallest printable cap thickness at or above d_wc_min."""
    min_steps = int(np.ceil(float(d_wc_min) / float(layer_height) - 1e-9))
    floor_mm = float(min_steps) * float(layer_height)
    if floor_mm < float(d_wc_min) - 1e-9:
        floor_mm += float(layer_height)
    return floor_mm

def _quantize_cap_map(
    cap_map: np.ndarray,
    *,
    layer_height: float,
    d_wc_min: float,
    d_wc_max: float,
) -> np.ndarray:
    """Round a requested cap map onto the printable cap lattice."""
    floor_mm = _quantized_cap_floor(d_wc_min, layer_height)
    quantized = np.rint(np.asarray(cap_map, dtype=np.float64) / float(layer_height))
    quantized = (quantized * float(layer_height)).astype(np.float32)
    quantized = np.maximum(quantized, np.float32(floor_mm))
    quantized = np.minimum(quantized, np.float32(d_wc_max))
    return quantized.astype(np.float32, copy=False)

def _continuity_cleanup_cap_map(
    cap_map: np.ndarray,
    *,
    layer_height: float,
    d_wc_min: float,
    d_wc_max: float,
) -> np.ndarray:
    """Fill tiny one-pixel cap discontinuities without changing overall policy."""
    floor_mm = _quantized_cap_floor(d_wc_min, layer_height)
    min_layers = max(1, int(np.rint(floor_mm / float(layer_height))))
    layers = np.rint(np.asarray(cap_map, dtype=np.float32) / float(layer_height)).astype(np.int32)
    thicker_than_floor = layers > min_layers
    if not thicker_than_floor.any() or thicker_than_floor.all():
        return np.array(cap_map, dtype=np.float32, copy=True)

    closed = binary_closing(
        thicker_than_floor,
        structure=generate_binary_structure(2, 1),
    )
    newly_filled = closed & ~thicker_than_floor
    if not newly_filled.any():
        return np.array(cap_map, dtype=np.float32, copy=True)

    fill_value = min(float(d_wc_max), float(min_layers + 1) * float(layer_height))
    out = np.array(cap_map, dtype=np.float32, copy=True)
    out[newly_filled] = np.maximum(out[newly_filled], np.float32(fill_value))
    return _quantize_cap_map(
        out,
        layer_height=layer_height,
        d_wc_min=d_wc_min,
        d_wc_max=d_wc_max,
    )

def _compute_stage4_detail_signal(visible_plan: VisibleRecipeRawGeometryPlan) -> np.ndarray:
    """Return a local high-frequency OKLab detail signal for Stage 4 gating."""
    shape = visible_plan.evaluation_shape
    targets = np.asarray(visible_plan.mapped_target_oklab, dtype=np.float32).reshape(shape + (3,))
    sigma = float(_STAGE4_DETAIL_SIGNAL_SIGMA)
    blurred = np.empty_like(targets, dtype=np.float32)
    for channel in range(3):
        blurred[..., channel] = gaussian_filter(
            targets[..., channel].astype(np.float64, copy=False),
            sigma=sigma,
            mode="nearest",
        ).astype(np.float32)
    delta = targets - blurred
    return np.sqrt(np.sum(delta * delta, axis=2)).astype(np.float32, copy=False)

def _stage4_detail_signal_threshold(
    *,
    detail_signal: np.ndarray,
    candidate_mask: np.ndarray,
    percentile: float = _STAGE4_DETAIL_SIGNAL_PERCENTILE,
) -> float | None:
    """Return the adaptive local-detail threshold for Stage 4 detail candidates."""
    candidate_signal = np.asarray(detail_signal, dtype=np.float32)[candidate_mask]
    positive_signal = candidate_signal[candidate_signal > 1e-9]
    if positive_signal.size == 0:
        return None
    return float(np.percentile(positive_signal, float(percentile)))

def _stage4_gradient_magnitude(values: np.ndarray) -> np.ndarray:
    """Return a simple local 4-neighbor gradient magnitude map."""
    arr = np.asarray(values, dtype=np.float32)
    grad = np.zeros(arr.shape, dtype=np.float32)
    if arr.shape[0] > 1:
        dy = np.abs(np.diff(arr, axis=0))
        grad[:-1, :] = np.maximum(grad[:-1, :], dy)
        grad[1:, :] = np.maximum(grad[1:, :], dy)
    if arr.shape[1] > 1:
        dx = np.abs(np.diff(arr, axis=1))
        grad[:, :-1] = np.maximum(grad[:, :-1], dx)
        grad[:, 1:] = np.maximum(grad[:, 1:], dx)
    return grad.astype(np.float32, copy=False)

def _stage4_recipe_boundary_mask(recipe_label_map: np.ndarray) -> np.ndarray:
    """Return pixels adjacent to a Stage 2 recipe transition."""
    labels = np.asarray(recipe_label_map, dtype=np.int32)
    boundary = np.zeros(labels.shape, dtype=bool)
    if labels.shape[0] > 1:
        dy = labels[:-1, :] != labels[1:, :]
        boundary[:-1, :] |= dy
        boundary[1:, :] |= dy
    if labels.shape[1] > 1:
        dx = labels[:, :-1] != labels[:, 1:]
        boundary[:, :-1] |= dx
        boundary[:, 1:] |= dx
    return boundary

def _stage4_detail_recipe_boundary_support(
    visible_plan: VisibleRecipeRawGeometryPlan,
) -> np.ndarray:
    """Return a small support band around Stage 2 recipe transitions."""
    recipe_edges = _stage4_recipe_boundary_mask(visible_plan.recipe_label_map)
    if not np.any(recipe_edges):
        return np.zeros(visible_plan.evaluation_shape, dtype=bool)
    return maximum_filter(
        recipe_edges.astype(np.uint8),
        size=3,
        mode="nearest",
    ) > 0

__all__ = (
    '_STAGE4_DETAIL_SIGNAL_SIGMA',
    '_STAGE4_DETAIL_SIGNAL_PERCENTILE',
    '_quantize_down',
    '_quantized_cap_floor',
    '_quantize_cap_map',
    '_continuity_cleanup_cap_map',
    '_compute_stage4_detail_signal',
    '_stage4_detail_signal_threshold',
    '_stage4_gradient_magnitude',
    '_stage4_recipe_boundary_mask',
    '_stage4_detail_recipe_boundary_support',
)
