"""Color-region geometry containers (phase 4 commit 1).

Each ``ColorRegion`` represents one connected same-stack downstream component
in solved image-domain millimeters. Regions are derived from
``SolvedMaterialPlan`` — they are not discovered from thickness rasters.

Coordinate convention:
    X_mm = col * solver_fine_pitch_mm   (origin at image left edge)
    Y_mm = row * solver_fine_pitch_mm   (origin at image top edge)

Rings follow the array-coordinate convention (Y-down). Downstream consumers
that need Y-up (e.g. STL export) apply the flip at consumption time.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Tuple

import numpy as np


@dataclass(frozen=True)
class ColorRegion:
    """One connected same-stack region in solved image-domain millimeters.

    Attributes
    ----------
    component_id : int
        Unique identifier within an extraction result.
    stack_index : int
        Index into ``SolvedMaterialPlan.stack_table``.
    exterior_ring_mm : (N, 2) float64
        Closed exterior boundary polygon in mm. Vertices are ``(x, y)``
        pairs. The first and last vertex are the same (explicitly closed).
    hole_rings_mm : tuple of (M, 2) float64
        Interior exclusion boundaries, each explicitly closed.
    segment_ids : tuple of int
        Source segment IDs covered by this connected component.
    area_mm2 : float
        Signed area of the exterior ring minus holes, in mm².
    bbox_mm : (x_min, y_min, x_max, y_max)
        Axis-aligned bounding box in mm.
    """

    component_id: int
    stack_index: int
    exterior_ring_mm: np.ndarray
    hole_rings_mm: Tuple[np.ndarray, ...]
    segment_ids: Tuple[int, ...]
    area_mm2: float
    bbox_mm: Tuple[float, float, float, float]

    def __post_init__(self) -> None:
        if self.exterior_ring_mm.ndim != 2 or self.exterior_ring_mm.shape[1] != 2:
            raise ValueError(
                f"exterior_ring_mm must be (N, 2), got {self.exterior_ring_mm.shape}"
            )
        for i, h in enumerate(self.hole_rings_mm):
            if h.ndim != 2 or h.shape[1] != 2:
                raise ValueError(
                    f"hole_rings_mm[{i}] must be (M, 2), got {h.shape}"
                )


@dataclass
class RegionGeometry:
    """Complete region-geometry extraction from a ``SolvedMaterialPlan``.

    Attributes
    ----------
    regions : tuple of ColorRegion
        All connected same-stack color regions. Disconnected components of the
        same stack appear as separate entries.
    image_domain_width_mm : float
        Physical width of the solved image domain.
    image_domain_height_mm : float
        Physical height of the solved image domain.
    solver_fine_pitch_mm : float
        Solve-grid cell size used for coordinate conversion.
    """

    regions: Tuple[ColorRegion, ...]
    image_domain_width_mm: float
    image_domain_height_mm: float
    solver_fine_pitch_mm: float

    @property
    def n_regions(self) -> int:
        return len(self.regions)

    def regions_for_stack(self, stack_index: int) -> Tuple[ColorRegion, ...]:
        return tuple(r for r in self.regions if r.stack_index == stack_index)


__all__ = ["ColorRegion", "RegionGeometry"]
