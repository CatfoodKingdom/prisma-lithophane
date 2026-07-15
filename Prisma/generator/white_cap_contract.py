"""Shared white-cap export contract constants."""
from __future__ import annotations

import math


WHITE_CAP_FIELD_TARGET_UPPER_SURFACE_KEY = "white_cap_field_target_upper_surface_map"
WHITE_CAP_FIELD_TARGET_METADATA_KEY = "white_cap_field_target"
PHYSICAL_GEOMETRY_METADATA_KEY = "physical_geometry"
WHITE_CAP_FIELD_TARGET_SCHEMA = "white-cap-field-target-v1"

POLICY_STANDARD_SMOOTH_VARIABLE_CANONICAL = "standard_smooth_variable_canonical"
POLICY_STANDARD_APPEARANCE_BOUNDED_STRUCTURAL_SPLIT = (
    "standard_appearance_bounded_structural_split"
)
POLICY_LUMINANCE_DETAIL_CANONICAL = "luminance_detail_canonical"


def quantized_cover_floor_mm(d_wc_min: float, layer_height_mm: float) -> float:
    """Smallest layer-quantized cap thickness at or above d_wc_min."""

    layer = float(layer_height_mm)
    if layer <= 0.0:
        raise ValueError("layer_height_mm must be positive")
    min_steps = int(math.ceil(float(d_wc_min) / layer - 1e-9))
    return float(max(1, min_steps) * layer)


__all__ = [
    "PHYSICAL_GEOMETRY_METADATA_KEY",
    "POLICY_LUMINANCE_DETAIL_CANONICAL",
    "POLICY_STANDARD_APPEARANCE_BOUNDED_STRUCTURAL_SPLIT",
    "POLICY_STANDARD_SMOOTH_VARIABLE_CANONICAL",
    "WHITE_CAP_FIELD_TARGET_METADATA_KEY",
    "WHITE_CAP_FIELD_TARGET_SCHEMA",
    "WHITE_CAP_FIELD_TARGET_UPPER_SURFACE_KEY",
    "quantized_cover_floor_mm",
]
