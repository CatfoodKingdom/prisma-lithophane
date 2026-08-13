"""Resolve user-meaningful settings into internal solver units."""
from __future__ import annotations

import math


def minimum_cap_thickness_mm(min_cap_layers: object, layer_height_mm: object) -> float:
    """Return the physical minimum-cap thickness requested by the user."""
    layers = max(1, int(min_cap_layers))
    layer_height = float(layer_height_mm)
    if not math.isfinite(layer_height) or layer_height <= 0:
        raise ValueError("layer_height_mm must be a positive finite value")
    return round(layers * layer_height, 6)


def boundary_cap_smoothing_cells(radius_mm: object, solve_pitch_mm: object) -> float:
    """Convert a physical boundary-smoothing radius to solve-grid cells."""
    radius = float(radius_mm)
    pitch = float(solve_pitch_mm)
    if not math.isfinite(radius) or radius < 0:
        raise ValueError("radius_mm must be a non-negative finite value")
    if not math.isfinite(pitch) or pitch <= 0:
        raise ValueError("solve_pitch_mm must be a positive finite value")
    return radius / pitch


__all__ = ["boundary_cap_smoothing_cells", "minimum_cap_thickness_mm"]
