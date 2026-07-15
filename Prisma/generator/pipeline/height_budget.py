"""Helpers for enforcing the explicit total-height contract.

The current config/pipeline contract treats ``t_max`` as the total object
height, including the white base. Any remaining height above the color ceiling
is the only headroom the white cap may occupy.
"""
from __future__ import annotations

import numpy as np


def max_cap_height_for_color_thickness(
    color_thickness_mm: float,
    *,
    d_wb_mm: float,
    t_max_mm: float,
    d_wc_max_mm: float | None = None,
) -> float:
    """Return the cap headroom left by a committed color stack."""
    remaining = float(t_max_mm) - float(d_wb_mm) - float(color_thickness_mm)
    if d_wc_max_mm is not None:
        remaining = min(remaining, float(d_wc_max_mm))
    return max(0.0, float(remaining))


def max_cap_height_map_for_color_ceiling(
    color_ceiling_map: np.ndarray,
    *,
    t_max_mm: float,
    d_wc_max_mm: float | None = None,
) -> np.ndarray:
    """Return the per-pixel white-cap headroom under the total-height ceiling."""
    color_ceiling = np.asarray(color_ceiling_map, dtype=np.float32)
    remaining = float(t_max_mm) - color_ceiling
    if d_wc_max_mm is not None:
        remaining = np.minimum(remaining, float(d_wc_max_mm))
    return np.maximum(remaining, 0.0).astype(np.float32, copy=False)


def clamp_cap_height_map_to_budget(
    cap_height_map: np.ndarray,
    color_ceiling_map: np.ndarray,
    *,
    t_max_mm: float,
    d_wc_max_mm: float | None = None,
) -> np.ndarray:
    """Clamp white-cap thickness so total object height never exceeds ``t_max``."""
    cap = np.maximum(np.asarray(cap_height_map, dtype=np.float32), 0.0)
    cap_max = max_cap_height_map_for_color_ceiling(
        color_ceiling_map,
        t_max_mm=t_max_mm,
        d_wc_max_mm=d_wc_max_mm,
    )
    return np.minimum(cap, cap_max).astype(np.float32, copy=False)


__all__ = [
    "max_cap_height_for_color_thickness",
    "max_cap_height_map_for_color_ceiling",
    "clamp_cap_height_map_to_budget",
]
