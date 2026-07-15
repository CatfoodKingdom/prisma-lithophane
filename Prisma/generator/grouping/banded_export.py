"""Shared immutable-plan helpers for swap-banded export.

The solver persists ``swap_grouping`` as JSON.  This module is the narrow
boundary that turns that JSON back into validated geometry and swap-plan input;
neither mesh export nor the instruction writer may infer band boundaries from
the solved thickness maps.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

import numpy as np

from grouping.band_plan import band_fill_maps


_EPS = 1e-6


@dataclass(frozen=True)
class BandedExportPlan:
    """Validated immutable geometry contract for a solved banded run."""

    groups: tuple[tuple[str, ...], ...]
    band_layers: tuple[int, ...]
    layer_height_mm: float
    d_wb_mm: float
    pause_z_mm: tuple[float, ...]

    @property
    def band_heights_mm(self) -> tuple[float, ...]:
        return tuple(float(layers) * self.layer_height_mm for layers in self.band_layers)

    @property
    def final_color_ceiling_mm(self) -> float:
        return self.d_wb_mm + sum(self.band_heights_mm)

    @property
    def color_filaments(self) -> tuple[str, ...]:
        return tuple(fid for group in self.groups for fid in group)

    def band_floor_mm(self, index: int) -> float:
        return self.d_wb_mm + sum(self.band_heights_mm[:index])

    def band_ceiling_mm(self, index: int) -> float:
        return self.band_floor_mm(index) + self.band_heights_mm[index]


def banded_export_plan_from_metadata(
    metadata: Mapping[str, Any] | None,
    *,
    d_wb_mm: float,
    layer_height_mm: float,
    expected_palette: Sequence[str] | None = None,
) -> BandedExportPlan | None:
    """Parse a persisted ``swap_grouping`` block without recomputing it.

    Returns ``None`` for an unbanded run.  A partially populated block is a
    hard export error: guessing here could put a color below the wrong pause.
    """

    if metadata is None:
        return None
    if not isinstance(metadata, Mapping):
        raise ValueError("swap_grouping must be an object")

    raw_groups = metadata.get("groups")
    raw_layers = metadata.get("band_layers")
    if not isinstance(raw_groups, Sequence) or isinstance(raw_groups, (str, bytes)):
        raise ValueError("swap_grouping.groups must be a non-empty list")
    if not isinstance(raw_layers, Sequence) or isinstance(raw_layers, (str, bytes)):
        raise ValueError("swap_grouping.band_layers must be a non-empty list")
    if len(raw_groups) == 0 or len(raw_groups) != len(raw_layers):
        raise ValueError("swap_grouping groups and band_layers must be matching non-empty lists")

    groups: list[tuple[str, ...]] = []
    for raw_group in raw_groups:
        if not isinstance(raw_group, Sequence) or isinstance(raw_group, (str, bytes)):
            raise ValueError("every swap_grouping group must be a non-empty list")
        group = tuple(str(fid) for fid in raw_group)
        if not group or any(not fid for fid in group):
            raise ValueError("every swap_grouping group must contain non-empty filament ids")
        groups.append(group)
    layers = tuple(int(value) for value in raw_layers)
    if any(value < 1 for value in layers):
        raise ValueError("swap_grouping band_layers must all be at least one")

    flat = tuple(fid for group in groups for fid in group)
    if len(set(flat)) != len(flat):
        raise ValueError("swap_grouping assigns a filament to more than one band")
    if expected_palette is not None:
        expected = tuple(str(fid) for fid in expected_palette)
        if len(flat) != len(expected) or set(flat) != set(expected):
            raise ValueError("swap_grouping colors do not match the solved canonical palette")
        declared_palette = metadata.get("canonical_palette")
        if declared_palette is not None:
            if not isinstance(declared_palette, Sequence) or isinstance(
                declared_palette, (str, bytes)
            ):
                raise ValueError("swap_grouping.canonical_palette must be a list")
            if tuple(str(fid) for fid in declared_palette) != expected:
                raise ValueError(
                    "swap_grouping canonical_palette disagrees with the solved canonical palette"
                )

    resolved_layer_height = float(metadata.get("layer_height_mm", layer_height_mm))
    resolved_d_wb = float(metadata.get("d_wb_mm", d_wb_mm))
    if resolved_layer_height <= 0.0:
        raise ValueError("swap_grouping layer_height_mm must be positive")
    if abs(resolved_layer_height - float(layer_height_mm)) > _EPS:
        raise ValueError("swap_grouping layer height disagrees with export settings")
    if abs(resolved_d_wb - float(d_wb_mm)) > _EPS:
        raise ValueError("swap_grouping white base thickness disagrees with export settings")

    heights = tuple(float(layer) * resolved_layer_height for layer in layers)
    pauses = tuple(
        resolved_d_wb + sum(heights[: index + 1])
        for index in range(len(heights) - 1)
    )
    declared_pauses = metadata.get("pause_z_mm")
    if declared_pauses is not None:
        if not isinstance(declared_pauses, Sequence) or isinstance(declared_pauses, (str, bytes)):
            raise ValueError("swap_grouping.pause_z_mm must be a list")
        actual = tuple(float(value) for value in declared_pauses)
        if len(actual) != len(pauses) or any(abs(left - right) > _EPS for left, right in zip(actual, pauses)):
            raise ValueError("swap_grouping pause_z_mm disagrees with its band boundaries")

    return BandedExportPlan(
        groups=tuple(groups),
        band_layers=layers,
        layer_height_mm=resolved_layer_height,
        d_wb_mm=resolved_d_wb,
        pause_z_mm=pauses,
    )


def banded_fill_maps(
    thickness_maps: Mapping[str, np.ndarray],
    plan: BandedExportPlan,
) -> tuple[np.ndarray, ...]:
    """Return each mandatory white-fill map, rejecting budget violations."""

    return band_fill_maps(
        thickness_maps,
        plan.groups,
        plan.band_layers,
        layer_height=plan.layer_height_mm,
    )


def banded_color_ceiling_map(shape: tuple[int, int], plan: BandedExportPlan) -> np.ndarray:
    """Return the flat post-band color/fill ceiling used by cap geometry."""

    return np.full(shape, plan.final_color_ceiling_mm, dtype=np.float32)


def band_slot_tables(plan: BandedExportPlan, *, color_slots: int) -> tuple[dict[int, str | None], ...]:
    """Assign each band to fixed color slots 1..N without swapping white."""

    if color_slots < 1:
        raise ValueError("at least one color slot is required for a banded swap plan")
    tables: list[dict[int, str | None]] = []
    for group in plan.groups:
        if len(group) > color_slots:
            raise ValueError("swap_grouping group exceeds the active printer color-slot capacity")
        table = {slot: None for slot in range(1, color_slots + 1)}
        for slot, fid in enumerate(group, start=1):
            table[slot] = fid
        tables.append(table)
    return tuple(tables)


__all__ = [
    "BandedExportPlan",
    "band_slot_tables",
    "banded_color_ceiling_map",
    "banded_export_plan_from_metadata",
    "banded_fill_maps",
]
