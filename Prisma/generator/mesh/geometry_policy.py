"""Resolved geometry/export policy shared by post-solve mesh builders.

The objects here intentionally do not read UI state.  Callers resolve user and
printer settings before entering mesh construction, then pass the resolved
numbers through this small policy shape.
"""

from __future__ import annotations

from dataclasses import dataclass


def _positive_float(name: str, value: float) -> float:
    number = float(value)
    if number <= 0.0:
        raise ValueError(f"{name} must be > 0, got {value!r}")
    return number


@dataclass(frozen=True)
class GeometryPrintabilityPolicy:
    """Physical printability thresholds derived from the solve grid.

    ``solve_grid_pitch_mm`` is the canonical hard printability pitch for the
    solve.  A 0.2 mm solve grid means 0.2 mm minimum positive width; a 0.4 mm
    solve grid means 0.4 mm minimum positive width.
    """

    solve_grid_pitch_mm: float
    min_printable_width_mm: float | None = None
    lateral_guard_width_mm: float | None = None
    max_internal_empty_hole_area_mm2: float | None = None
    min_positive_feature_area_mm2: float | None = None
    min_positive_feature_width_mm: float | None = None

    def __post_init__(self) -> None:
        pitch = _positive_float("solve_grid_pitch_mm", self.solve_grid_pitch_mm)
        object.__setattr__(self, "solve_grid_pitch_mm", pitch)

        min_width = (
            pitch
            if self.min_printable_width_mm is None
            else _positive_float("min_printable_width_mm", self.min_printable_width_mm)
        )
        object.__setattr__(self, "min_printable_width_mm", min_width)

        guard = (
            min_width
            if self.lateral_guard_width_mm is None
            else _positive_float("lateral_guard_width_mm", self.lateral_guard_width_mm)
        )
        object.__setattr__(self, "lateral_guard_width_mm", guard)

        max_empty = (
            0.5 * min_width * min_width
            if self.max_internal_empty_hole_area_mm2 is None
            else _positive_float(
                "max_internal_empty_hole_area_mm2",
                self.max_internal_empty_hole_area_mm2,
            )
        )
        object.__setattr__(self, "max_internal_empty_hole_area_mm2", max_empty)

        min_positive_area = (
            min_width * min_width
            if self.min_positive_feature_area_mm2 is None
            else _positive_float(
                "min_positive_feature_area_mm2",
                self.min_positive_feature_area_mm2,
            )
        )
        object.__setattr__(self, "min_positive_feature_area_mm2", min_positive_area)

        min_positive_width = (
            min_width
            if self.min_positive_feature_width_mm is None
            else _positive_float(
                "min_positive_feature_width_mm",
                self.min_positive_feature_width_mm,
            )
        )
        object.__setattr__(self, "min_positive_feature_width_mm", min_positive_width)

    def as_dict(self) -> dict[str, float]:
        return {
            "solve_grid_pitch_mm": float(self.solve_grid_pitch_mm),
            "min_printable_width_mm": float(self.min_printable_width_mm),
            "lateral_guard_width_mm": float(self.lateral_guard_width_mm),
            "max_internal_empty_hole_area_mm2": float(self.max_internal_empty_hole_area_mm2),
            "min_positive_feature_area_mm2": float(self.min_positive_feature_area_mm2),
            "min_positive_feature_width_mm": float(self.min_positive_feature_width_mm),
        }


@dataclass(frozen=True)
class GeometryExportPolicy:
    """Resolved settings needed by field-to-mesh and raster-to-mesh export."""

    solve_grid_pitch_mm: float
    geometry_xy_pitch_mm: float
    layer_height_mm: float
    white_base_thickness_mm: float
    printability: GeometryPrintabilityPolicy | None = None

    def __post_init__(self) -> None:
        solve_pitch = _positive_float("solve_grid_pitch_mm", self.solve_grid_pitch_mm)
        geometry_pitch = _positive_float("geometry_xy_pitch_mm", self.geometry_xy_pitch_mm)
        layer_height = _positive_float("layer_height_mm", self.layer_height_mm)
        white_base = _positive_float("white_base_thickness_mm", self.white_base_thickness_mm)
        object.__setattr__(self, "solve_grid_pitch_mm", solve_pitch)
        object.__setattr__(self, "geometry_xy_pitch_mm", geometry_pitch)
        object.__setattr__(self, "layer_height_mm", layer_height)
        object.__setattr__(self, "white_base_thickness_mm", white_base)
        if self.printability is None:
            object.__setattr__(
                self,
                "printability",
                GeometryPrintabilityPolicy(solve_grid_pitch_mm=solve_pitch),
            )
        elif abs(float(self.printability.solve_grid_pitch_mm) - solve_pitch) > 1e-9:
            raise ValueError(
                "printability.solve_grid_pitch_mm must match GeometryExportPolicy.solve_grid_pitch_mm"
            )

    def as_dict(self) -> dict[str, object]:
        assert self.printability is not None
        return {
            "solve_grid_pitch_mm": float(self.solve_grid_pitch_mm),
            "geometry_xy_pitch_mm": float(self.geometry_xy_pitch_mm),
            "layer_height_mm": float(self.layer_height_mm),
            "white_base_thickness_mm": float(self.white_base_thickness_mm),
            "printability": self.printability.as_dict(),
        }
