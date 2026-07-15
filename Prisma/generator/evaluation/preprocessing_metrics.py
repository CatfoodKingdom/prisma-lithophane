"""A4 shared metric primitives for preprocessing evaluation.

Definitions pinned by Wing B consensus § J (foundation amendment Q3 assigns
these A4 placeholders to Wing B as canonical):

- ``count_small_components`` — 8-connectivity CCL on per-filament thickness
  maps, thresholded at 5% of per-map max thickness, counting components whose
  physical area is below ``min_area_mm2``.
- ``cap_total_variation`` — anisotropic L1 total variation over a cap
  thickness map at solve-grid resolution.

The harness in ``evaluation/preprocessing_harness.py`` composes both metrics
into ``EvaluationMetrics``; tests in
``tests/generator/test_preprocessing_evaluation_metrics.py`` cover the pinned
semantics (pitch scaling, 8-connectivity, 5%-of-max threshold,
constant/structured TV values).
"""
from __future__ import annotations

from typing import Mapping

import numpy as np


__all__ = ["count_small_components", "cap_total_variation"]


# Fixed per Wing B consensus § J: threshold is 5% of per-map max thickness.
_SMALL_COMPONENT_REL_THRESHOLD = 0.05


def count_small_components(
    thickness_maps: Mapping[str, np.ndarray],
    *,
    min_area_mm2: float,
    pixel_pitch_mm: float,
) -> int:
    """Count small connected components across per-filament thickness maps.

    For each per-filament thickness map in ``thickness_maps`` (keys starting
    with ``__`` are treated as diagnostics and skipped), pixels above 5% of
    the per-map max thickness are marked as occupied. 8-connectivity
    connected components are extracted, their physical areas computed from
    pixel count × ``pixel_pitch_mm²``, and the count of components below
    ``min_area_mm2`` is accumulated.
    """
    from scipy import ndimage

    min_area = float(min_area_mm2)
    pitch = float(pixel_pitch_mm)
    pixel_area = pitch * pitch
    structure = np.ones((3, 3), dtype=np.int8)  # 8-connectivity

    total = 0
    for key, tmap in thickness_maps.items():
        if key.startswith("__"):
            continue
        arr = np.asarray(tmap)
        if arr.ndim != 2 or arr.size == 0:
            continue
        vmax = float(arr.max(initial=0.0))
        if vmax <= 0.0:
            continue
        mask = arr > (_SMALL_COMPONENT_REL_THRESHOLD * vmax)
        if not mask.any():
            continue
        labels, n = ndimage.label(mask, structure=structure)
        if n <= 0:
            continue
        sizes = np.bincount(labels.ravel(), minlength=n + 1)
        # labels are 1..n; sizes[0] is background
        areas = sizes[1 : n + 1].astype(np.float64) * pixel_area
        total += int(np.count_nonzero(areas < min_area))
    return total


def cap_total_variation(cap_map: np.ndarray) -> float:
    """Anisotropic L1 total variation over a 2-D cap thickness map.

    Returns ``Σ(|Δx| + |Δy|)`` summed across every adjacent-pixel pair on the
    grid. The caller is expected to pass the cap at solve-grid resolution;
    the value is a grid-space sum, not a per-mm integral.
    """
    arr = np.asarray(cap_map, dtype=np.float32)
    if arr.ndim != 2 or arr.size == 0:
        return 0.0
    dx = np.abs(np.diff(arr, axis=1))
    dy = np.abs(np.diff(arr, axis=0))
    return float(dx.sum() + dy.sum())
