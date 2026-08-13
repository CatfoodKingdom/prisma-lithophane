"""Exact decimal-to-layer resolution for solver height budgets.

Physical settings arrive as decimal millimeter values but are carried by
Python floats.  Converting a quotient such as ``2.60 / 0.10`` with ``int`` can
therefore discard a mathematically valid final layer.  Resolve scalar settings
in decimal space once, then keep budget arithmetic in integer layer counts.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP
import math


_FALLBACK_SNAP_ULPS = 8


def _decimal(value: object) -> Decimal:
    """Return the user-facing decimal representation of a numeric setting."""

    return Decimal(str(value))


def floor_layer_steps(height_mm: object, layer_height_mm: object) -> int:
    """Compatibility fallback for callers that only have a computed height.

    Production resolves original physical settings with ``resolve_layer_budget``.
    A lower-level caller may already have introduced binary subtraction error,
    so snap only quotients within a conservative float-operation error bound of
    an integer.  ``int`` retains the legacy treatment of invalid negatives.
    """

    ratio = float(height_mm) / float(layer_height_mm)
    if math.isfinite(ratio):
        nearest = round(ratio)
        tolerance = _FALLBACK_SNAP_ULPS * max(
            math.ulp(ratio),
            math.ulp(float(nearest)),
        )
        if abs(ratio - nearest) <= tolerance:
            ratio = float(nearest)
    return int(ratio)


def _truncate_decimal_layers(height_mm: Decimal, layer_height_mm: Decimal) -> int:
    """Match legacy ``int`` semantics while avoiding binary representation error."""

    ratio = height_mm / layer_height_mm
    rounding = ROUND_FLOOR if ratio >= 0 else ROUND_CEILING
    return int(ratio.to_integral_value(rounding=rounding))


@dataclass(frozen=True)
class ResolvedLayerBudget:
    """Immutable whole-layer budget shared by one production solve."""

    layer_height_mm: float
    post_base_steps: int
    effective_max_layers: int
    minimum_cap_steps: int
    color_budget_mm: float
    used_color_budget_mm: float
    remainder_mm: float
    lower_total_mm: float
    upper_total_mm: float


@dataclass(frozen=True)
class ResolvedBorderHeight:
    """A border height resolved onto the print's post-base layer grid."""

    below_base: bool
    aligned: bool
    layer_steps: int
    remainder_mm: float
    lower_height_mm: float
    upper_height_mm: float


def resolve_border_height(
    *,
    border_height_mm: object,
    base_thickness_mm: object,
    layer_height_mm: object,
) -> ResolvedBorderHeight:
    """Resolve a border height relative to the top of the white base.

    The border must reach at least the base thickness. Any height above the
    base must be a whole number of print layers so its top lands on a printable
    Z boundary.
    """

    border_height = _decimal(border_height_mm)
    base_thickness = _decimal(base_thickness_mm)
    layer_height = _decimal(layer_height_mm)
    if layer_height <= 0:
        raise ValueError("layer_height_mm must be positive")

    excess = border_height - base_thickness
    if excess < 0:
        return ResolvedBorderHeight(
            below_base=True,
            aligned=False,
            layer_steps=0,
            remainder_mm=float(excess),
            lower_height_mm=float(base_thickness),
            upper_height_mm=float(base_thickness),
        )

    ratio = excess / layer_height
    nearest_steps = int(ratio.to_integral_value(rounding=ROUND_HALF_UP))
    nearest_height = Decimal(nearest_steps) * layer_height
    tolerance = Decimal("0.000001")
    if abs(excess - nearest_height) <= tolerance:
        resolved = base_thickness + nearest_height
        return ResolvedBorderHeight(
            below_base=False,
            aligned=True,
            layer_steps=nearest_steps,
            remainder_mm=0.0,
            lower_height_mm=float(resolved),
            upper_height_mm=float(resolved),
        )

    lower_steps = int(ratio.to_integral_value(rounding=ROUND_FLOOR))
    lower = base_thickness + Decimal(lower_steps) * layer_height
    upper = lower + layer_height
    return ResolvedBorderHeight(
        below_base=False,
        aligned=False,
        layer_steps=lower_steps,
        remainder_mm=float(excess - Decimal(lower_steps) * layer_height),
        lower_height_mm=float(lower),
        upper_height_mm=float(upper),
    )


def resolve_layer_budget(
    *,
    t_max_mm: object,
    d_wb_mm: object,
    d_wc_min_mm: object,
    layer_height_mm: object,
    max_layers: int | None = None,
) -> ResolvedLayerBudget:
    """Resolve configured physical limits without binary-float truncation."""

    layer_height = _decimal(layer_height_mm)
    post_base = _decimal(t_max_mm) - _decimal(d_wb_mm)
    color_at_minimum_cap = post_base - _decimal(d_wc_min_mm)
    post_base_steps = _truncate_decimal_layers(post_base, layer_height)
    derived_max_layers = _truncate_decimal_layers(
        color_at_minimum_cap,
        layer_height,
    )
    effective_max_layers = (
        int(max_layers) if max_layers is not None else derived_max_layers
    )
    # Preserve the established cap-grid convention.  Cap settings are
    # canonicalized onto this grid before production solve materialization.
    minimum_cap_steps = max(
        1,
        round(float(d_wc_min_mm) / float(layer_height_mm)),
    )
    used_color_budget = Decimal(effective_max_layers) * layer_height
    remainder = color_at_minimum_cap - used_color_budget
    lower_total = _decimal(d_wb_mm) + _decimal(d_wc_min_mm) + used_color_budget
    upper_total = lower_total + layer_height
    return ResolvedLayerBudget(
        layer_height_mm=float(layer_height_mm),
        post_base_steps=post_base_steps,
        effective_max_layers=effective_max_layers,
        minimum_cap_steps=minimum_cap_steps,
        color_budget_mm=float(color_at_minimum_cap),
        used_color_budget_mm=float(used_color_budget),
        remainder_mm=float(remainder),
        lower_total_mm=float(lower_total),
        upper_total_mm=float(upper_total),
    )


def minimum_total_thickness_mm(
    *,
    d_wb_mm: object,
    d_wc_min_mm: object,
    layer_height_mm: object,
) -> float:
    """Return the minimum total thickness that leaves one color layer."""

    return float(
        _decimal(d_wb_mm)
        + _decimal(d_wc_min_mm)
        + _decimal(layer_height_mm)
    )


__all__ = [
    "ResolvedBorderHeight",
    "ResolvedLayerBudget",
    "floor_layer_steps",
    "minimum_total_thickness_mm",
    "resolve_border_height",
    "resolve_layer_budget",
]
