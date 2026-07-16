"""Helpers for enforcing the explicit total-height contract.

The current config/pipeline contract treats ``t_max`` as the total object
height, including the white base. Any remaining height above the color ceiling
is the only headroom the white cap may occupy.
"""
from __future__ import annotations


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


__all__ = [
    "max_cap_height_for_color_thickness",
]
