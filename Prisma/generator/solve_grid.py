"""Canonical physical-size to whole-cell solve-grid resolution."""
from __future__ import annotations

import math
from typing import Any


ROUNDING_MODE = "half_up"
ALIGNMENT_TOLERANCE_MM = 1e-6
FRAME_DIMENSION_MIN_MM = 10.0
FRAME_DIMENSION_MAX_MM = 300.0
_STABLE_DECIMALS = 6


class SolveGridResolutionError(ValueError):
    """Raised when positive whole solve cells cannot represent the frame."""


def _stable(value: float) -> float:
    numeric = float(value)
    scale = 10**_STABLE_DECIMALS
    magnitude = math.floor(abs(numeric) * scale + 0.5) / scale
    return math.copysign(magnitude, numeric) if magnitude else 0.0


def round_half_up_positive(value: float) -> int:
    """Round a finite non-negative number to the nearest integer, ties upward."""

    numeric = float(value)
    if not math.isfinite(numeric) or numeric < 0.0:
        raise SolveGridResolutionError("solve-grid rounding requires a finite non-negative value")
    return int(math.floor(numeric + 0.5))


def _resolve_axis_cells(
    requested_mm: float,
    pitch_mm: float,
    *,
    minimum_mm: float,
    maximum_mm: float,
) -> int:
    minimum_cells = max(1, int(math.ceil((minimum_mm / pitch_mm) - 1e-12)))
    maximum_cells = int(math.floor((maximum_mm / pitch_mm) + 1e-12))
    if maximum_cells < minimum_cells:
        raise SolveGridResolutionError(
            f"Solve Pitch ({pitch_mm:g} mm) is too large for the supported "
            f"image dimension range ({minimum_mm:g}-{maximum_mm:g} mm)"
        )
    rounded = round_half_up_positive(requested_mm / pitch_mm)
    return max(minimum_cells, min(maximum_cells, rounded))


def resolve_solve_grid(
    width_mm: float,
    height_mm: float,
    pitch_mm: float,
    *,
    minimum_mm: float = FRAME_DIMENSION_MIN_MM,
    maximum_mm: float = FRAME_DIMENSION_MAX_MM,
) -> dict[str, Any]:
    """Resolve requested physical dimensions onto the canonical whole-cell grid."""

    width = float(width_mm)
    height = float(height_mm)
    pitch = float(pitch_mm)
    minimum = float(minimum_mm)
    maximum = float(maximum_mm)
    if not all(math.isfinite(value) for value in (width, height, pitch, minimum, maximum)):
        raise SolveGridResolutionError("solve-grid dimensions and pitch must be finite")
    if width <= 0.0 or height <= 0.0 or pitch <= 0.0:
        raise SolveGridResolutionError("solve-grid dimensions and pitch must be positive")
    if minimum <= 0.0 or maximum < minimum:
        raise SolveGridResolutionError("invalid solve-grid dimension range")

    width_cells = _resolve_axis_cells(width, pitch, minimum_mm=minimum, maximum_mm=maximum)
    height_cells = _resolve_axis_cells(height, pitch, minimum_mm=minimum, maximum_mm=maximum)
    requested_width = _stable(width)
    requested_height = _stable(height)
    resolved_width = _stable(width_cells * pitch)
    resolved_height = _stable(height_cells * pitch)
    width_delta = _stable(resolved_width - requested_width)
    height_delta = _stable(resolved_height - requested_height)
    width_aligned = math.isclose(
        requested_width,
        resolved_width,
        rel_tol=0.0,
        abs_tol=ALIGNMENT_TOLERANCE_MM,
    )
    height_aligned = math.isclose(
        requested_height,
        resolved_height,
        rel_tol=0.0,
        abs_tol=ALIGNMENT_TOLERANCE_MM,
    )
    return {
        "rounding_mode": ROUNDING_MODE,
        "pitch_mm": _stable(pitch),
        "requested": {
            "width_mm": requested_width,
            "height_mm": requested_height,
        },
        "cells": {
            "width": width_cells,
            "height": height_cells,
        },
        "resolved": {
            "width_mm": resolved_width,
            "height_mm": resolved_height,
        },
        "delta": {
            "width_mm": width_delta,
            "height_mm": height_delta,
        },
        "aligned": {
            "width": width_aligned,
            "height": height_aligned,
            "all": width_aligned and height_aligned,
        },
    }


__all__ = [
    "ALIGNMENT_TOLERANCE_MM",
    "FRAME_DIMENSION_MAX_MM",
    "FRAME_DIMENSION_MIN_MM",
    "ROUNDING_MODE",
    "SolveGridResolutionError",
    "resolve_solve_grid",
    "round_half_up_positive",
]
