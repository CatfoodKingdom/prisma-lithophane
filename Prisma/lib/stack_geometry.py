"""Utilities for model-facing filament stack geometry.

Calibration records sometimes preserve how a strip was authored or sliced
rather than the physical material regions a light path sees. For prediction,
adjacent runs of the same filament should be treated as one contiguous region.
Non-adjacent repeats are intentionally kept separate because interfaces and
order still matter.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any


Layer = tuple[str, float]


def _clean_filament(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _clean_thickness(value: Any, *, precision: int) -> float:
    try:
        thickness = round(float(value), precision)
    except (TypeError, ValueError):
        thickness = 0.0
    return 0.0 if abs(thickness) < 10 ** (-(precision + 1)) else thickness


def collapse_adjacent_layers(
    layers: Iterable[tuple[Any, Any]],
    *,
    precision: int = 5,
    drop_zero: bool = True,
) -> tuple[Layer, ...]:
    """Collapse adjacent same-filament layers while preserving stack order.

    Examples:
        A 0.40 + A 0.20 + W 0.20 -> A 0.60 + W 0.20
        A 0.20 + B 0.20 + A 0.20 -> unchanged
    """

    collapsed: list[Layer] = []
    for raw_filament, raw_thickness in layers:
        filament = _clean_filament(raw_filament)
        if not filament:
            continue
        thickness = _clean_thickness(raw_thickness, precision=precision)
        if drop_zero and abs(thickness) <= 1e-12:
            continue
        if collapsed and collapsed[-1][0] == filament:
            collapsed[-1] = (
                filament,
                _clean_thickness(collapsed[-1][1] + thickness, precision=precision),
            )
        else:
            collapsed.append((filament, thickness))
    return tuple(collapsed)


def canonicalize_filament_stack(
    filaments: Sequence[Any],
    thicknesses_mm: Sequence[Any],
    *,
    precision: int = 5,
    default_thickness_mm: float = 0.0,
    drop_zero: bool = True,
) -> tuple[Layer, ...]:
    """Build a canonical ordered stack from parallel filament/thickness lists."""

    pairs = [
        (
            filament,
            thicknesses_mm[index] if index < len(thicknesses_mm) else default_thickness_mm,
        )
        for index, filament in enumerate(filaments)
    ]
    return collapse_adjacent_layers(pairs, precision=precision, drop_zero=drop_zero)


def unzip_layers(layers: Iterable[tuple[Any, Any]]) -> tuple[tuple[str, ...], tuple[float, ...]]:
    """Return parallel filament and thickness tuples from a layer sequence."""

    normalized = tuple((str(filament).strip(), float(thickness)) for filament, thickness in layers)
    return (
        tuple(filament for filament, _thickness in normalized),
        tuple(thickness for _filament, thickness in normalized),
    )
