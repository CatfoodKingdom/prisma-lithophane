"""Exact arithmetic and value objects for Generator print setup.

Product persistence uses integer micrometers so profile identity and Solve
Pitch multiplication never depend on binary floating-point equality. Existing
solver boundaries continue to receive millimeter floats after resolution.
"""
from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Mapping

from solve_grid import FRAME_DIMENSION_MAX_MM


MICROMETERS_PER_MM = 1000
MINIMUM_SOLVE_PITCH_MULTIPLIER = 1
MAX_INPUT_DECIMALS = 3
_ROUND_TRIP_TOLERANCE_MM = 5e-7


class PrintSetupValueError(ValueError):
    """Raised when a persisted or requested print-setup value is invalid."""


def mm_to_um(value: object, *, field: str, positive: bool = True) -> int:
    """Normalize a millimeter boundary value to exact integer micrometers."""

    if isinstance(value, bool):
        raise PrintSetupValueError(f"{field} must be a finite millimeter value")
    try:
        numeric = float(value)
    except (TypeError, ValueError) as exc:
        raise PrintSetupValueError(f"{field} must be a finite millimeter value") from exc
    if not math.isfinite(numeric) or (positive and numeric <= 0.0):
        qualifier = "positive " if positive else ""
        raise PrintSetupValueError(f"{field} must be a {qualifier}finite millimeter value")
    normalized = int(math.floor(abs(numeric) * MICROMETERS_PER_MM + 0.5))
    if numeric < 0:
        normalized = -normalized
    if positive and normalized <= 0:
        raise PrintSetupValueError(f"{field} must be at least 0.001 mm")
    round_trip = normalized / MICROMETERS_PER_MM
    if not math.isclose(numeric, round_trip, rel_tol=0.0, abs_tol=_ROUND_TRIP_TOLERANCE_MM):
        raise PrintSetupValueError(
            f"{field} supports at most {MAX_INPUT_DECIMALS} decimal places"
        )
    return normalized


def require_um(value: object, *, field: str, positive: bool = True) -> int:
    """Validate an integer-micrometer persisted value."""

    if isinstance(value, bool) or not isinstance(value, int):
        raise PrintSetupValueError(f"{field} must be an integer number of micrometers")
    if positive and value <= 0:
        raise PrintSetupValueError(f"{field} must be positive")
    return int(value)


def um_to_mm(value: int) -> float:
    return int(value) / MICROMETERS_PER_MM


def require_multiplier(value: object, *, field: str) -> int:
    if isinstance(value, bool):
        raise PrintSetupValueError(f"{field} must be a positive whole number")
    try:
        numeric = float(value)
    except (TypeError, ValueError) as exc:
        raise PrintSetupValueError(f"{field} must be a positive whole number") from exc
    if not math.isfinite(numeric) or not numeric.is_integer() or numeric < 1:
        raise PrintSetupValueError(f"{field} must be a positive whole number")
    return int(numeric)


def maximum_solve_pitch_multiplier(extrusion_width_um: int) -> int:
    width = require_um(extrusion_width_um, field="extrusion_width_um")
    maximum_um = int(round(FRAME_DIMENSION_MAX_MM * MICROMETERS_PER_MM))
    return max(MINIMUM_SOLVE_PITCH_MULTIPLIER, maximum_um // width)


def resolve_pitch_um(extrusion_width_um: int, multiplier: int) -> int:
    width = require_um(extrusion_width_um, field="extrusion_width_um")
    factor = require_multiplier(multiplier, field="solve_pitch_extrusion_width_multiplier")
    maximum = maximum_solve_pitch_multiplier(width)
    if factor > maximum:
        raise PrintSetupValueError(
            "solve_pitch_extrusion_width_multiplier exceeds the supported image range"
        )
    return width * factor


def nearest_supported_layer_height_um(
    requested_um: int,
    minimum_um: int,
    maximum_um: int,
) -> int:
    requested = require_um(requested_um, field="layer_height_um")
    minimum = require_um(minimum_um, field="min_layer_height_um")
    maximum = require_um(maximum_um, field="max_layer_height_um")
    if minimum > maximum:
        raise PrintSetupValueError("minimum Layer Height cannot exceed maximum Layer Height")
    return min(max(requested, minimum), maximum)


@dataclass(frozen=True)
class ResolvedPrintSetup:
    printer_id: str
    nozzle_id: str
    nozzle_diameter_um: int
    extrusion_width_um: int
    solve_pitch_extrusion_width_multiplier: int
    effective_solve_pitch_um: int
    min_layer_height_um: int
    max_layer_height_um: int
    minimum_line_length_multiplier: int
    minimum_line_length_um: int

    @property
    def minimum_component_area_mm2(self) -> float:
        return um_to_mm(self.extrusion_width_um) * um_to_mm(self.minimum_line_length_um)

    def to_dict(self) -> dict[str, Any]:
        return {
            "printer_id": self.printer_id,
            "nozzle_id": self.nozzle_id,
            "nozzle_diameter_um": self.nozzle_diameter_um,
            "nozzle_diameter_mm": um_to_mm(self.nozzle_diameter_um),
            "extrusion_width_um": self.extrusion_width_um,
            "extrusion_width_mm": um_to_mm(self.extrusion_width_um),
            "solve_pitch_extrusion_width_multiplier": (
                self.solve_pitch_extrusion_width_multiplier
            ),
            "effective_solve_pitch_um": self.effective_solve_pitch_um,
            "effective_solve_pitch_mm": um_to_mm(self.effective_solve_pitch_um),
            "max_solve_pitch_extrusion_width_multiplier": (
                maximum_solve_pitch_multiplier(self.extrusion_width_um)
            ),
            "min_layer_height_um": self.min_layer_height_um,
            "min_layer_height_mm": um_to_mm(self.min_layer_height_um),
            "max_layer_height_um": self.max_layer_height_um,
            "max_layer_height_mm": um_to_mm(self.max_layer_height_um),
            "minimum_line_length_multiplier": self.minimum_line_length_multiplier,
            "minimum_line_length_um": self.minimum_line_length_um,
            "minimum_line_length_mm": um_to_mm(self.minimum_line_length_um),
            "minimum_component_area_mm2": self.minimum_component_area_mm2,
        }


def resolved_print_setup_from_active(
    active: Mapping[str, Any],
    multiplier: object,
) -> ResolvedPrintSetup:
    printer = active.get("printer")
    nozzle = active.get("nozzle")
    width = active.get("extrusion_width")
    if not isinstance(printer, Mapping) or not isinstance(nozzle, Mapping) or not isinstance(width, Mapping):
        raise PrintSetupValueError("an active printer, nozzle, and Extrusion Width are required")
    width_um = require_um(width.get("width_um"), field="width_um")
    factor = require_multiplier(multiplier, field="solve_pitch_extrusion_width_multiplier")
    pitch_um = resolve_pitch_um(width_um, factor)
    line_multiplier = require_multiplier(
        nozzle.get("minimum_line_length_multiplier"),
        field="minimum_line_length_multiplier",
    )
    return ResolvedPrintSetup(
        printer_id=str(printer.get("id") or ""),
        nozzle_id=str(nozzle.get("id") or ""),
        nozzle_diameter_um=require_um(nozzle.get("diameter_um"), field="diameter_um"),
        extrusion_width_um=width_um,
        solve_pitch_extrusion_width_multiplier=factor,
        effective_solve_pitch_um=pitch_um,
        min_layer_height_um=require_um(nozzle.get("min_layer_height_um"), field="min_layer_height_um"),
        max_layer_height_um=require_um(nozzle.get("max_layer_height_um"), field="max_layer_height_um"),
        minimum_line_length_multiplier=line_multiplier,
        minimum_line_length_um=width_um * line_multiplier,
    )


__all__ = [
    "MAX_INPUT_DECIMALS",
    "MICROMETERS_PER_MM",
    "MINIMUM_SOLVE_PITCH_MULTIPLIER",
    "PrintSetupValueError",
    "ResolvedPrintSetup",
    "maximum_solve_pitch_multiplier",
    "mm_to_um",
    "nearest_supported_layer_height_um",
    "require_multiplier",
    "require_um",
    "resolve_pitch_um",
    "resolved_print_setup_from_active",
    "um_to_mm",
]
